"""
Tests for sender_type filter on get_conversation_history (issue #848).

Covers:
  - _apply_sender_type_filter pure helper in inbox_server.py
  - _apply_filters_and_paginate with sender_type argument (filesystem fallback path)
  - db/reader.py get_conversation_history / count_conversation_history with sender_type
    against an in-memory SQLite DB (SQL path)
  - handle_get_conversation_history end-to-end: sender_type propagated to DB call
  - No regression: omitting sender_type still returns all messages

All DB tests use an in-memory SQLite connection — no on-disk state required.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ and src/mcp/ are importable.
_SRC_DIR = Path(__file__).parent.parent.parent.parent / "src"
_MCP_DIR = _SRC_DIR / "mcp"
for _d in (_SRC_DIR, _MCP_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# Pre-load inbox_server so patch.multiple can resolve it.
import src.mcp.inbox_server  # noqa: F401

from src.mcp.inbox_server import (
    _apply_filters_and_paginate,
    _apply_sender_type_filter,
    handle_get_conversation_history,
)
from db import reader as db_reader
from db.connection import apply_schema

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent / "src" / "db" / "schema.sql"


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn, _SCHEMA_PATH.read_text())
    return conn


def _insert(conn: sqlite3.Connection, **kwargs: Any) -> None:
    defaults: dict[str, Any] = {
        "id": "m1",
        "direction": "in",
        "source": "telegram",
        "type": "text",
        "chat_id": "111",
        "user_id": "u1",
        "username": "alice",
        "user_name": "Alice",
        "text": "hello",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "telegram_message_id": None,
        "extra": None,
    }
    row = {**defaults, **kwargs}
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO messages ({cols}) VALUES ({placeholders})", list(row.values()))
    conn.commit()


def _msg(
    id: str = "m1",
    direction: str = "received",
    type_: str = "text",
    chat_id: str = "111",
    timestamp: str = "2024-01-15T10:00:00+00:00",
    text: str = "Hello",
) -> dict:
    return {
        "id": id,
        "_direction": direction,
        "type": type_,
        "source": "telegram",
        "chat_id": chat_id,
        "timestamp": timestamp,
        "text": text,
        "user_name": "Alice",
        "username": "alice",
    }


# ---------------------------------------------------------------------------
# Tests: _apply_sender_type_filter (pure in-memory helper)
# ---------------------------------------------------------------------------


class TestApplySenderTypeFilter:
    """Unit tests for the pure in-memory sender_type filtering helper."""

    def _sample_messages(self):
        return [
            _msg("user-text", direction="received", type_="text"),
            _msg("user-photo", direction="received", type_="photo"),
            _msg("user-voice", direction="received", type_="voice"),
            _msg("sys-result", direction="received", type_="subagent_result"),
            _msg("sys-cron", direction="received", type_="scheduled_reminder"),
            _msg("sys-health", direction="received", type_="health_check"),
            _msg("lobster-out", direction="sent", type_="text"),
        ]

    def test_none_returns_all_unchanged(self):
        msgs = self._sample_messages()
        result = _apply_sender_type_filter(msgs, None)
        assert len(result) == len(msgs)

    def test_all_returns_all_unchanged(self):
        msgs = self._sample_messages()
        result = _apply_sender_type_filter(msgs, "all")
        assert len(result) == len(msgs)

    def test_user_returns_only_inbound_user_types(self):
        msgs = self._sample_messages()
        result = _apply_sender_type_filter(msgs, "user")
        ids = {m["id"] for m in result}
        # user-initiated inbound types included
        assert "user-text" in ids
        assert "user-photo" in ids
        assert "user-voice" in ids
        # system inbound types excluded
        assert "sys-result" not in ids
        assert "sys-cron" not in ids
        assert "sys-health" not in ids
        # outbound excluded
        assert "lobster-out" not in ids

    def test_lobster_returns_only_outbound(self):
        msgs = self._sample_messages()
        result = _apply_sender_type_filter(msgs, "lobster")
        assert all(m["_direction"] == "sent" for m in result)
        assert len(result) == 1
        assert result[0]["id"] == "lobster-out"

    def test_conversation_excludes_system_noise(self):
        msgs = self._sample_messages()
        result = _apply_sender_type_filter(msgs, "conversation")
        ids = {m["id"] for m in result}
        # real conversation messages included
        assert "user-text" in ids
        assert "user-photo" in ids
        assert "user-voice" in ids
        assert "lobster-out" in ids
        # system noise excluded
        assert "sys-result" not in ids
        assert "sys-cron" not in ids
        assert "sys-health" not in ids

    def test_unknown_value_returns_all(self):
        msgs = self._sample_messages()
        result = _apply_sender_type_filter(msgs, "bogus_value")
        assert len(result) == len(msgs)

    def test_does_not_mutate_input_list(self):
        msgs = self._sample_messages()
        original_len = len(msgs)
        _apply_sender_type_filter(msgs, "user")
        assert len(msgs) == original_len  # original untouched


# ---------------------------------------------------------------------------
# Tests: _apply_filters_and_paginate with sender_type (filesystem fallback)
# ---------------------------------------------------------------------------


class TestApplyFiltersAndPaginateWithSenderType:
    """Verify sender_type is applied in the in-memory filter/paginate helper."""

    def _messages(self):
        return [
            _msg("u1", direction="received", type_="text"),
            _msg("u2", direction="received", type_="voice"),
            _msg("s1", direction="received", type_="subagent_result"),
            _msg("s2", direction="received", type_="health_check"),
            _msg("l1", direction="sent", type_="text"),
        ]

    def test_sender_type_user(self):
        result, total = _apply_filters_and_paginate(
            self._messages(),
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=10,
            offset=0,
            sender_type="user",
        )
        ids = {m["id"] for m in result}
        assert "u1" in ids
        assert "u2" in ids
        assert "s1" not in ids
        assert "s2" not in ids
        assert "l1" not in ids
        assert total == 2

    def test_sender_type_lobster(self):
        result, total = _apply_filters_and_paginate(
            self._messages(),
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=10,
            offset=0,
            sender_type="lobster",
        )
        assert total == 1
        assert result[0]["id"] == "l1"

    def test_sender_type_conversation(self):
        result, total = _apply_filters_and_paginate(
            self._messages(),
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=10,
            offset=0,
            sender_type="conversation",
        )
        ids = {m["id"] for m in result}
        assert "u1" in ids
        assert "u2" in ids
        assert "l1" in ids
        assert "s1" not in ids
        assert "s2" not in ids
        assert total == 3

    def test_sender_type_none_returns_all(self):
        result, total = _apply_filters_and_paginate(
            self._messages(),
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=10,
            offset=0,
            sender_type=None,
        )
        assert total == 5

    def test_pagination_count_is_post_filter(self):
        """total count must reflect sender_type-filtered count, not raw count."""
        result, total = _apply_filters_and_paginate(
            self._messages(),
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=1,
            offset=0,
            sender_type="conversation",
        )
        # 3 conversation messages total, only 1 returned due to limit
        assert total == 3
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Tests: db/reader.py SQL path with sender_type
# ---------------------------------------------------------------------------


class TestDbReaderSenderType:
    """Verify sender_type filter generates correct SQL against in-memory SQLite."""

    def _setup_db(self):
        conn = _make_conn()
        _insert(conn, id="u1", direction="in", type="text", text="user text")
        _insert(conn, id="u2", direction="in", type="photo", text="user photo")
        _insert(conn, id="s1", direction="in", type="subagent_result", text="result noise")
        _insert(conn, id="s2", direction="in", type="scheduled_reminder", text="cron noise")
        _insert(conn, id="l1", direction="out", type="text", text="lobster reply")
        return conn

    def test_sender_type_user_sql(self):
        conn = self._setup_db()
        rows = db_reader.get_conversation_history(conn, sender_type="user")
        ids = {r["id"] for r in rows}
        assert "u1" in ids
        assert "u2" in ids
        assert "s1" not in ids
        assert "s2" not in ids
        assert "l1" not in ids
        conn.close()

    def test_sender_type_lobster_sql(self):
        conn = self._setup_db()
        rows = db_reader.get_conversation_history(conn, sender_type="lobster")
        assert len(rows) == 1
        assert rows[0]["id"] == "l1"
        conn.close()

    def test_sender_type_conversation_sql(self):
        conn = self._setup_db()
        rows = db_reader.get_conversation_history(conn, sender_type="conversation")
        ids = {r["id"] for r in rows}
        assert "u1" in ids
        assert "u2" in ids
        assert "l1" in ids
        assert "s1" not in ids
        assert "s2" not in ids
        conn.close()

    def test_sender_type_none_returns_all(self):
        conn = self._setup_db()
        rows = db_reader.get_conversation_history(conn, sender_type=None)
        assert len(rows) == 5
        conn.close()

    def test_count_matches_get_for_user(self):
        conn = self._setup_db()
        rows = db_reader.get_conversation_history(conn, sender_type="user")
        count = db_reader.count_conversation_history(conn, sender_type="user")
        assert count == len(rows)
        conn.close()

    def test_count_matches_get_for_conversation(self):
        conn = self._setup_db()
        rows = db_reader.get_conversation_history(conn, sender_type="conversation")
        count = db_reader.count_conversation_history(conn, sender_type="conversation")
        assert count == len(rows)
        conn.close()

    def test_sender_type_combined_with_chat_id(self):
        """sender_type and chat_id filters compose correctly."""
        conn = _make_conn()
        _insert(conn, id="a1", direction="in", type="text", chat_id="111", text="chat 111 user")
        _insert(conn, id="a2", direction="in", type="text", chat_id="222", text="chat 222 user")
        _insert(conn, id="a3", direction="in", type="subagent_result", chat_id="111", text="noise")
        rows = db_reader.get_conversation_history(conn, chat_id="111", sender_type="user")
        ids = {r["id"] for r in rows}
        assert "a1" in ids
        assert "a2" not in ids
        assert "a3" not in ids
        conn.close()


# ---------------------------------------------------------------------------
# Tests: handle_get_conversation_history propagates sender_type to DB layer
# ---------------------------------------------------------------------------


class TestHandleGetConversationHistorySenderType:
    """Verify sender_type is threaded through to _db_get_conversation_history."""

    def test_sender_type_propagated_to_db_call(self):
        mock_get = MagicMock(return_value=[])
        mock_count = MagicMock(return_value=0)
        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=mock_get,
            _db_count_conversation_history=mock_count,
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
            PROCESSED_DIR=Path("/tmp"),
            SENT_DIR=Path("/tmp"),
        ):
            asyncio.run(handle_get_conversation_history({"sender_type": "conversation"}))

        _, kwargs = mock_get.call_args
        assert kwargs.get("sender_type") == "conversation"

    def test_sender_type_user_propagated(self):
        mock_get = MagicMock(return_value=[])
        mock_count = MagicMock(return_value=0)
        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=mock_get,
            _db_count_conversation_history=mock_count,
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
            PROCESSED_DIR=Path("/tmp"),
            SENT_DIR=Path("/tmp"),
        ):
            asyncio.run(handle_get_conversation_history({"sender_type": "user"}))

        _, kwargs = mock_get.call_args
        assert kwargs.get("sender_type") == "user"

    def test_omitted_sender_type_is_none(self):
        """When sender_type is not in args, None is passed to DB layer."""
        mock_get = MagicMock(return_value=[])
        mock_count = MagicMock(return_value=0)
        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=mock_get,
            _db_count_conversation_history=mock_count,
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
            PROCESSED_DIR=Path("/tmp"),
            SENT_DIR=Path("/tmp"),
        ):
            asyncio.run(handle_get_conversation_history({}))

        _, kwargs = mock_get.call_args
        assert kwargs.get("sender_type") is None

    def test_filter_info_includes_sender_type_on_no_results(self, tmp_path: Path):
        """When no results found, the 'no messages' string mentions sender_type."""
        processed = tmp_path / "processed"
        processed.mkdir()
        (tmp_path / "sent").mkdir()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=None,
            _db_count_conversation_history=None,
            PROCESSED_DIR=processed,
            SENT_DIR=tmp_path / "sent",
        ):
            result = asyncio.run(
                handle_get_conversation_history({"sender_type": "user", "chat_id": "999"})
            )

        text = result[0].text
        assert "sender_type=user" in text
        assert "chat_id=999" in text
