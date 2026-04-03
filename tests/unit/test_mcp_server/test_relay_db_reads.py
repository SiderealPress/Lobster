"""
Tests for BIS-164 Slice 3 — Relay DB reads.

Verifies that:
  - src/db/reader.py pure functions query correctly against an in-memory SQLite DB.
  - inbox_server.py handlers use the DB-first path when messages.db is available.
  - inbox_server.py handlers fall back to the filesystem when the DB is unavailable
    or returns zero results.

All tests use an in-memory SQLite database so that no on-disk state is required.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ is on sys.path (root conftest does this, but be explicit here).
_SRC_DIR = Path(__file__).parent.parent.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Ensure src/mcp/ is on sys.path for sibling imports inside inbox_server.
_MCP_DIR = _SRC_DIR / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

# Pre-load inbox_server so patch.multiple can resolve it.
import src.mcp.inbox_server  # noqa: F401

# Import reader module directly for unit tests.
from db import reader as db_reader
from db.connection import apply_schema, open_messages_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent / "src" / "db" / "schema.sql"


def _make_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the messages schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    schema_sql = _SCHEMA_PATH.read_text()
    apply_schema(conn, schema_sql)
    return conn


def _insert_message(conn: sqlite3.Connection, **kwargs) -> None:
    """Insert a row into messages.  Caller supplies field values as kwargs."""
    defaults: dict[str, Any] = {
        "id": "msg-001",
        "direction": "in",
        "source": "telegram",
        "type": "text",
        "chat_id": "1234",
        "user_id": "u1",
        "username": "alice",
        "user_name": "Alice",
        "text": "Hello, world!",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "telegram_message_id": None,
        "extra": None,
    }
    row = {**defaults, **kwargs}
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO messages ({cols}) VALUES ({placeholders})", list(row.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: db/reader.py internal helpers
# ---------------------------------------------------------------------------


class TestReaderInternals:
    """Unit tests for private helper functions in db/reader.py."""

    def test_row_to_dict_converts_sqlite_row(self):
        conn = _make_conn()
        row = conn.execute("SELECT 1 AS foo, 'bar' AS baz").fetchone()
        result = db_reader._row_to_dict(row)
        assert result == {"foo": 1, "baz": "bar"}
        conn.close()

    def test_merge_extra_expands_json_column(self):
        row = {"text": "hi", "extra": json.dumps({"image_file": "/tmp/img.jpg"})}
        result = db_reader._merge_extra(row)
        assert result["image_file"] == "/tmp/img.jpg"
        assert "extra" not in result

    def test_merge_extra_handles_missing_extra(self):
        row = {"text": "hi"}
        result = db_reader._merge_extra(row)
        assert result == {"text": "hi"}

    def test_merge_extra_handles_invalid_json(self):
        row = {"text": "hi", "extra": "not-json"}
        result = db_reader._merge_extra(row)
        # extra key consumed but nothing merged
        assert "extra" not in result
        assert result == {"text": "hi"}

    def test_normalize_direction_in_maps_to_received(self):
        row = {"direction": "in", "text": "hello"}
        result = db_reader._normalize_direction(row)
        assert result["_direction"] == "received"

    def test_normalize_direction_out_maps_to_sent(self):
        row = {"direction": "out", "text": "reply"}
        result = db_reader._normalize_direction(row)
        assert result["_direction"] == "sent"

    def test_parse_timestamp_valid_utc(self):
        from datetime import timezone
        ts = db_reader._parse_timestamp("2024-01-15T10:00:00+00:00")
        assert ts.tzinfo is not None
        assert ts.year == 2024

    def test_parse_timestamp_z_suffix(self):
        ts = db_reader._parse_timestamp("2024-03-01T12:00:00Z")
        assert ts.year == 2024

    def test_parse_timestamp_none_returns_min(self):
        from datetime import datetime, timezone
        ts = db_reader._parse_timestamp(None)
        assert ts == datetime.min.replace(tzinfo=timezone.utc)

    def test_table_exists_returns_true_for_existing_table(self):
        conn = _make_conn()
        assert db_reader._table_exists(conn, "messages") is True
        conn.close()

    def test_table_exists_returns_false_for_missing_table(self):
        conn = _make_conn()
        assert db_reader._table_exists(conn, "nonexistent_table") is False
        conn.close()


# ---------------------------------------------------------------------------
# Tests: get_conversation_history
# ---------------------------------------------------------------------------


class TestGetConversationHistory:
    """Tests for db_reader.get_conversation_history."""

    def test_returns_empty_list_when_no_messages(self):
        conn = _make_conn()
        result = db_reader.get_conversation_history(conn)
        assert result == []
        conn.close()

    def test_returns_inserted_message(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", text="Hello DB!")
        result = db_reader.get_conversation_history(conn)
        assert len(result) == 1
        assert result[0]["text"] == "Hello DB!"
        conn.close()

    def test_direction_key_is_set_on_results(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="in")
        result = db_reader.get_conversation_history(conn)
        assert result[0]["_direction"] == "received"
        conn.close()

    def test_filters_by_chat_id(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", chat_id="111", text="chat 111")
        _insert_message(conn, id="m2", chat_id="222", text="chat 222")
        result = db_reader.get_conversation_history(conn, chat_id="111")
        assert len(result) == 1
        assert result[0]["text"] == "chat 111"
        conn.close()

    def test_filters_by_source_case_insensitive(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", source="telegram", text="telegram msg")
        _insert_message(conn, id="m2", source="bisque", text="bisque msg")
        result = db_reader.get_conversation_history(conn, source="TELEGRAM")
        assert len(result) == 1
        assert result[0]["source"] == "telegram"
        conn.close()

    def test_filters_by_direction_received(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="in", text="inbound")
        _insert_message(conn, id="m2", direction="out", text="outbound")
        result = db_reader.get_conversation_history(conn, direction="received")
        assert len(result) == 1
        assert result[0]["text"] == "inbound"
        conn.close()

    def test_filters_by_direction_sent(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="in", text="inbound")
        _insert_message(conn, id="m2", direction="out", text="outbound")
        result = db_reader.get_conversation_history(conn, direction="sent")
        assert len(result) == 1
        assert result[0]["text"] == "outbound"
        conn.close()

    def test_pagination_limit(self):
        conn = _make_conn()
        for i in range(5):
            _insert_message(conn, id=f"m{i}", text=f"msg {i}",
                            timestamp=f"2024-01-{i+10:02d}T10:00:00+00:00")
        result = db_reader.get_conversation_history(conn, limit=2)
        assert len(result) == 2
        conn.close()

    def test_pagination_offset(self):
        conn = _make_conn()
        for i in range(4):
            _insert_message(conn, id=f"m{i}", text=f"msg {i}",
                            timestamp=f"2024-01-{i+10:02d}T10:00:00+00:00")
        page1 = db_reader.get_conversation_history(conn, limit=2, offset=0)
        page2 = db_reader.get_conversation_history(conn, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # Pages should not overlap
        ids1 = {r["id"] for r in page1}
        ids2 = {r["id"] for r in page2}
        assert ids1.isdisjoint(ids2)
        conn.close()

    def test_results_ordered_newest_first(self):
        conn = _make_conn()
        _insert_message(conn, id="old", timestamp="2024-01-01T10:00:00+00:00", text="old")
        _insert_message(conn, id="new", timestamp="2024-06-01T10:00:00+00:00", text="new")
        result = db_reader.get_conversation_history(conn)
        assert result[0]["id"] == "new"
        assert result[1]["id"] == "old"
        conn.close()

    def test_search_via_like_fallback(self):
        """Keyword search falls back to LIKE when FTS5 is unavailable."""
        conn = _make_conn()
        _insert_message(conn, id="m1", text="find the needle here")
        _insert_message(conn, id="m2", text="nothing relevant")
        # Force the LIKE path by pretending messages_fts doesn't exist.
        with patch.object(db_reader, "_table_exists", return_value=False):
            result = db_reader.get_conversation_history(conn, search="needle")
        assert len(result) == 1
        assert result[0]["id"] == "m1"
        conn.close()

    def test_search_via_fts5_path(self):
        """FTS5 search works when messages_fts table is present (regression: BIS-164)."""
        conn = _make_conn()
        _insert_message(conn, id="m1", text="find the needle here")
        _insert_message(conn, id="m2", text="nothing relevant")
        # The in-memory DB created by _make_conn() includes messages_fts,
        # so this exercises the real FTS5 JOIN path — no mocking.
        result = db_reader.get_conversation_history(conn, search="needle")
        assert len(result) == 1
        assert result[0]["id"] == "m1"
        conn.close()

    def test_search_fts5_with_source_filter_no_ambiguous_column(self):
        """
        FTS5 search combined with source filter must not raise
        'ambiguous column name: source' (regression: GitHub issue #849 / BIS-164).

        The bug: SELECT used bare m.{_HISTORY_COLUMNS} which only prefixed the
        first column, and conditions were passed unqualified into the FTS JOIN
        WHERE clause — SQLite raised OperationalError for shared column names
        (source, type, text, transcription, user_name).
        """
        conn = _make_conn()
        _insert_message(conn, id="m1", source="telegram", text="needle in telegram")
        _insert_message(conn, id="m2", source="bisque", text="needle in bisque")
        # Must not raise OperationalError: ambiguous column name: source
        result = db_reader.get_conversation_history(conn, source="telegram", search="needle")
        assert len(result) == 1
        assert result[0]["id"] == "m1"
        conn.close()

    def test_search_fts5_with_chat_id_and_source_filter(self):
        """FTS5 search with both chat_id and source filters does not raise ambiguous column."""
        conn = _make_conn()
        _insert_message(conn, id="m1", chat_id="111", source="telegram", text="needle here")
        _insert_message(conn, id="m2", chat_id="222", source="telegram", text="needle there")
        result = db_reader.get_conversation_history(
            conn, chat_id="111", source="telegram", search="needle"
        )
        assert len(result) == 1
        assert result[0]["id"] == "m1"
        conn.close()

    def test_count_search_fts5_with_source_filter_no_ambiguous_column(self):
        """count_conversation_history FTS5 path also handles source filter without error."""
        conn = _make_conn()
        _insert_message(conn, id="m1", source="telegram", text="needle in telegram")
        _insert_message(conn, id="m2", source="bisque", text="needle in bisque")
        count = db_reader.count_conversation_history(conn, source="telegram", search="needle")
        assert count == 1

    def test_extra_json_column_merged_into_result(self):
        conn = _make_conn()
        extra = json.dumps({"image_file": "/tmp/foo.jpg", "image_width": 800})
        _insert_message(conn, id="m1", extra=extra)
        result = db_reader.get_conversation_history(conn)
        assert result[0]["image_file"] == "/tmp/foo.jpg"
        assert result[0]["image_width"] == 800
        assert "extra" not in result[0]
        conn.close()


# ---------------------------------------------------------------------------
# Tests: count_conversation_history
# ---------------------------------------------------------------------------


class TestCountConversationHistory:
    """Tests for db_reader.count_conversation_history."""

    def test_returns_zero_for_empty_table(self):
        conn = _make_conn()
        assert db_reader.count_conversation_history(conn) == 0
        conn.close()

    def test_counts_all_messages(self):
        conn = _make_conn()
        for i in range(3):
            _insert_message(conn, id=f"m{i}")
        assert db_reader.count_conversation_history(conn) == 3
        conn.close()

    def test_count_respects_chat_id_filter(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", chat_id="111")
        _insert_message(conn, id="m2", chat_id="222")
        assert db_reader.count_conversation_history(conn, chat_id="111") == 1
        conn.close()

    def test_count_respects_direction_filter(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="in")
        _insert_message(conn, id="m2", direction="out")
        assert db_reader.count_conversation_history(conn, direction="received") == 1
        assert db_reader.count_conversation_history(conn, direction="sent") == 1
        conn.close()


# ---------------------------------------------------------------------------
# Tests: get_message_by_id
# ---------------------------------------------------------------------------


class TestGetMessageById:
    """Tests for db_reader.get_message_by_id."""

    def test_returns_none_for_missing_id(self):
        conn = _make_conn()
        assert db_reader.get_message_by_id(conn, "nonexistent") is None
        conn.close()

    def test_returns_message_for_existing_id(self):
        conn = _make_conn()
        _insert_message(conn, id="msg-xyz", text="found it")
        result = db_reader.get_message_by_id(conn, "msg-xyz")
        assert result is not None
        assert result["text"] == "found it"
        conn.close()

    def test_returned_row_has_direction_key(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="out")
        result = db_reader.get_message_by_id(conn, "m1")
        assert result["_direction"] == "sent"
        conn.close()


# ---------------------------------------------------------------------------
# Tests: get_message_by_telegram_id
# ---------------------------------------------------------------------------


class TestGetMessageByTelegramId:
    """Tests for db_reader.get_message_by_telegram_id."""

    def test_returns_none_for_missing_tg_id(self):
        conn = _make_conn()
        assert db_reader.get_message_by_telegram_id(conn, 99999) is None
        conn.close()

    def test_returns_message_for_matching_tg_id(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", telegram_message_id=42, text="tg message")
        result = db_reader.get_message_by_telegram_id(conn, 42)
        assert result is not None
        assert result["text"] == "tg message"
        conn.close()

    def test_narrows_by_chat_id(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", telegram_message_id=10, chat_id="111")
        _insert_message(conn, id="m2", telegram_message_id=10, chat_id="222")
        result = db_reader.get_message_by_telegram_id(conn, 10, chat_id="111")
        assert result is not None
        assert result["id"] == "m1"
        conn.close()


# ---------------------------------------------------------------------------
# Tests: get_message_stats
# ---------------------------------------------------------------------------


class TestGetMessageStats:
    """Tests for db_reader.get_message_stats."""

    def test_returns_zeros_for_empty_db(self):
        conn = _make_conn()
        stats = db_reader.get_message_stats(conn)
        assert stats["total_messages"] == 0
        assert stats["inbound_count"] == 0
        assert stats["outbound_count"] == 0
        assert stats["by_source"] == {}
        conn.close()

    def test_counts_inbound_and_outbound(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="in")
        _insert_message(conn, id="m2", direction="out")
        stats = db_reader.get_message_stats(conn)
        assert stats["total_messages"] == 2
        assert stats["inbound_count"] == 1
        assert stats["outbound_count"] == 1
        conn.close()

    def test_groups_by_source(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", source="telegram")
        _insert_message(conn, id="m2", source="telegram")
        _insert_message(conn, id="m3", source="bisque")
        stats = db_reader.get_message_stats(conn)
        assert stats["by_source"]["telegram"] == 2
        assert stats["by_source"]["bisque"] == 1
        conn.close()

    def test_agent_events_count_zero_when_empty(self):
        conn = _make_conn()
        stats = db_reader.get_message_stats(conn)
        assert stats["agent_events_count"] == 0
        conn.close()

    def test_bisque_events_count_zero_when_empty(self):
        conn = _make_conn()
        stats = db_reader.get_message_stats(conn)
        assert stats["bisque_events_count"] == 0
        conn.close()


# ---------------------------------------------------------------------------
# Tests: get_recent_messages
# ---------------------------------------------------------------------------


class TestGetRecentMessages:
    """Tests for db_reader.get_recent_messages."""

    def test_returns_only_inbound(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="in", text="inbound")
        _insert_message(conn, id="m2", direction="out", text="outbound")
        result = db_reader.get_recent_messages(conn)
        assert len(result) == 1
        assert result[0]["text"] == "inbound"
        conn.close()

    def test_respects_since_ts_filter(self):
        conn = _make_conn()
        _insert_message(conn, id="old", direction="in",
                        timestamp="2024-01-01T00:00:00+00:00", text="old")
        _insert_message(conn, id="new", direction="in",
                        timestamp="2024-06-01T00:00:00+00:00", text="new")
        result = db_reader.get_recent_messages(conn, since_ts="2024-03-01T00:00:00+00:00")
        assert len(result) == 1
        assert result[0]["id"] == "new"
        conn.close()

    def test_respects_source_filter(self):
        conn = _make_conn()
        _insert_message(conn, id="m1", direction="in", source="telegram")
        _insert_message(conn, id="m2", direction="in", source="bisque")
        result = db_reader.get_recent_messages(conn, source="telegram")
        assert len(result) == 1
        assert result[0]["source"] == "telegram"
        conn.close()

    def test_respects_limit(self):
        conn = _make_conn()
        for i in range(5):
            _insert_message(conn, id=f"m{i}", direction="in",
                            timestamp=f"2024-01-{i+10:02d}T00:00:00+00:00")
        result = db_reader.get_recent_messages(conn, limit=3)
        assert len(result) == 3
        conn.close()


# ---------------------------------------------------------------------------
# Tests: inbox_server.py handler integration (DB-first paths)
# ---------------------------------------------------------------------------


class TestHandleGetConversationHistoryDbFirst:
    """Integration tests for handle_get_conversation_history using DB path."""

    def _make_mock_conn(self, rows: list[dict], count: int):
        """Create a minimal mock connection context."""
        mock_conn = MagicMock()
        return mock_conn

    def test_uses_db_when_available(self, tmp_path: Path):
        """When DB reader is available and returns rows, results come from DB."""
        from src.mcp.inbox_server import handle_get_conversation_history

        db_rows = [
            {"id": "db1", "_direction": "received", "source": "telegram",
             "chat_id": "111", "timestamp": "2024-01-15T10:00:00+00:00",
             "text": "From DB", "user_name": "Alice", "username": "alice"},
        ]
        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=MagicMock(return_value=db_rows),
            _db_count_conversation_history=MagicMock(return_value=1),
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
        ):
            result = asyncio.run(handle_get_conversation_history({}))

        assert len(result) == 1
        assert "From DB" in result[0].text

    def test_falls_back_to_filesystem_when_db_unavailable(self, tmp_path: Path):
        """When _open_messages_db_conn returns None, filesystem fallback is used."""
        from src.mcp.inbox_server import handle_get_conversation_history

        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        msg = {
            "id": "fs1",
            "text": "From filesystem",
            "source": "telegram",
            "chat_id": "111",
            "timestamp": "2024-01-15T10:00:00+00:00",
            "user_name": "Bob",
        }
        (processed_dir / "fs1.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=MagicMock(return_value=[]),
            _db_count_conversation_history=MagicMock(return_value=0),
            _open_messages_db_conn=MagicMock(return_value=None),
            PROCESSED_DIR=processed_dir,
            SENT_DIR=tmp_path / "sent",
        ):
            result = asyncio.run(handle_get_conversation_history({}))

        assert len(result) == 1
        assert "From filesystem" in result[0].text

    def test_falls_back_when_db_reader_is_none(self, tmp_path: Path):
        """When the DB reader module failed to import, filesystem is used."""
        from src.mcp.inbox_server import handle_get_conversation_history

        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        msg = {
            "id": "fs2", "text": "fallback message", "source": "telegram",
            "chat_id": "555", "timestamp": "2024-01-15T10:00:00+00:00",
        }
        (processed_dir / "fs2.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=None,
            _db_count_conversation_history=None,
            PROCESSED_DIR=processed_dir,
            SENT_DIR=tmp_path / "sent",
        ):
            result = asyncio.run(handle_get_conversation_history({}))

        assert "fallback message" in result[0].text


class TestHandleGetStatsDbAugmentation:
    """Tests for handle_get_stats DB stats augmentation."""

    def test_includes_db_totals_when_db_available(self, tmp_path: Path):
        """Stats output contains messages.db section when DB is available."""
        from src.mcp.inbox_server import handle_get_stats

        mock_conn = MagicMock()
        db_stats = {
            "total_messages": 42,
            "inbound_count": 30,
            "outbound_count": 12,
            "by_source": {"telegram": 40, "bisque": 2},
            "agent_events_count": 5,
            "bisque_events_count": 3,
        }

        # Create minimal empty dirs so glob doesn't error
        for d in ["inbox", "outbox", "processed", "processing", "failed"]:
            (tmp_path / d).mkdir()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_message_stats=MagicMock(return_value=db_stats),
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
            INBOX_DIR=tmp_path / "inbox",
            OUTBOX_DIR=tmp_path / "outbox",
            PROCESSED_DIR=tmp_path / "processed",
            PROCESSING_DIR=tmp_path / "processing",
            FAILED_DIR=tmp_path / "failed",
        ):
            result = asyncio.run(handle_get_stats({}))

        text = result[0].text
        assert "messages.db Totals" in text
        assert "42" in text  # total_messages

    def test_omits_db_section_when_db_unavailable(self, tmp_path: Path):
        """When DB is unavailable, stats output does not mention messages.db."""
        from src.mcp.inbox_server import handle_get_stats

        for d in ["inbox", "outbox", "processed", "processing", "failed"]:
            (tmp_path / d).mkdir()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_message_stats=None,
            _open_messages_db_conn=MagicMock(return_value=None),
            INBOX_DIR=tmp_path / "inbox",
            OUTBOX_DIR=tmp_path / "outbox",
            PROCESSED_DIR=tmp_path / "processed",
            PROCESSING_DIR=tmp_path / "processing",
            FAILED_DIR=tmp_path / "failed",
        ):
            result = asyncio.run(handle_get_stats({}))

        text = result[0].text
        assert "messages.db" not in text


class TestHandleGetMessageByTelegramIdDbFirst:
    """Tests for handle_get_message_by_telegram_id DB-first lookup."""

    def test_returns_db_result_when_available(self, tmp_path: Path):
        """Message found in DB is returned without hitting filesystem."""
        from src.mcp.inbox_server import handle_get_message_by_telegram_id

        db_msg = {
            "id": "db-msg-1",
            "telegram_message_id": 777,
            "source": "telegram",
            "chat_id": "111",
            "timestamp": "2024-01-15T10:00:00+00:00",
            "text": "Found in DB",
            "type": "text",
            "user_name": "Alice",
            "username": "alice",
            "reply_to": None,
            "reply_to_message_id": None,
            "image_file": None,
            "file_path": None,
            "audio_file": None,
        }
        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_message_by_telegram_id_fn=MagicMock(return_value=db_msg),
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
        ):
            result = asyncio.run(
                handle_get_message_by_telegram_id({"telegram_message_id": 777})
            )

        text = result[0].text
        assert "Found in DB" in text
        assert "messages.db" in text

    def test_falls_back_to_filesystem_when_db_returns_none(self, tmp_path: Path):
        """When DB returns None, filesystem scan is used."""
        from src.mcp.inbox_server import handle_get_message_by_telegram_id

        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        msg = {
            "id": "fs-msg-1",
            "telegram_message_id": 888,
            "source": "telegram",
            "chat_id": "222",
            "timestamp": "2024-01-15T10:00:00+00:00",
            "text": "Found in filesystem",
            "type": "text",
            "user_name": "Bob",
        }
        (processed_dir / "fs-msg-1.json").write_text(json.dumps(msg))

        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_message_by_telegram_id_fn=MagicMock(return_value=None),
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
            PROCESSED_DIR=processed_dir,
            INBOX_DIR=tmp_path / "inbox",
            PROCESSING_DIR=tmp_path / "processing",
            FAILED_DIR=tmp_path / "failed",
        ):
            result = asyncio.run(
                handle_get_message_by_telegram_id({"telegram_message_id": 888})
            )

        text = result[0].text
        assert "Found in filesystem" in text

    def test_returns_error_for_missing_tg_id_param(self):
        """Returns error message when telegram_message_id is not provided."""
        from src.mcp.inbox_server import handle_get_message_by_telegram_id

        result = asyncio.run(handle_get_message_by_telegram_id({}))
        assert "required" in result[0].text.lower()
