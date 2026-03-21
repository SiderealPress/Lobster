"""
Tests for BIS-165 Slice 4 — conversation_history SQL read path.

Verifies that:
  - _open_messages_db_conn() returns None when the DB does not exist.
  - _scan_json_dirs_for_history() is a pure function that scans JSON files.
  - _apply_filters_and_paginate() correctly filters, sorts, and paginates.
  - _format_history_output() renders the expected markdown.
  - handle_get_conversation_history() uses the DB when available, falls back
    to the filesystem when the DB returns zero results, and emits the correct
    text output in each branch.

All tests use in-memory SQLite or tmp_path so no on-disk production state is
required.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ and src/mcp/ are importable.
_SRC_DIR = Path(__file__).parent.parent.parent.parent / "src"
_MCP_DIR = _SRC_DIR / "mcp"
for _d in (_SRC_DIR, _MCP_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# Pre-load inbox_server so patch.multiple resolves it.
import src.mcp.inbox_server  # noqa: F401

from src.mcp.inbox_server import (
    _scan_json_dirs_for_history,
    _apply_filters_and_paginate,
    _format_history_output,
    _open_messages_db_conn,
    handle_get_conversation_history,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(
    id: str = "m1",
    direction: str = "received",
    source: str = "telegram",
    chat_id: str = "111",
    timestamp: str = "2024-01-15T10:00:00+00:00",
    text: str = "Hello",
    user_name: str = "Alice",
    username: str = "alice",
) -> dict:
    return {
        "id": id,
        "_direction": direction,
        "source": source,
        "chat_id": chat_id,
        "timestamp": timestamp,
        "text": text,
        "user_name": user_name,
        "username": username,
    }


# ---------------------------------------------------------------------------
# Tests: _scan_json_dirs_for_history (pure filesystem scan)
# ---------------------------------------------------------------------------


class TestScanJsonDirsForHistory:
    """Unit tests for the pure-function JSON directory scan."""

    def test_returns_empty_list_when_dirs_empty(self, tmp_path: Path):
        processed = tmp_path / "processed"
        sent = tmp_path / "sent"
        processed.mkdir()
        sent.mkdir()

        with patch.multiple(
            "src.mcp.inbox_server",
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            result = _scan_json_dirs_for_history("all")

        assert result == []

    def test_loads_received_messages_from_processed(self, tmp_path: Path):
        processed = tmp_path / "processed"
        processed.mkdir()
        (tmp_path / "sent").mkdir()
        msg = {"id": "r1", "text": "hello", "chat_id": "111", "source": "telegram"}
        (processed / "r1.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            PROCESSED_DIR=processed,
            SENT_DIR=tmp_path / "sent",
        ):
            result = _scan_json_dirs_for_history("all")

        assert len(result) == 1
        assert result[0]["_direction"] == "received"
        assert result[0]["_filename"] == "r1.json"

    def test_loads_sent_messages_from_sent_dir(self, tmp_path: Path):
        (tmp_path / "processed").mkdir()
        sent = tmp_path / "sent"
        sent.mkdir()
        msg = {"id": "s1", "text": "reply", "chat_id": "111", "source": "telegram"}
        (sent / "s1.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            PROCESSED_DIR=tmp_path / "processed",
            SENT_DIR=sent,
        ):
            result = _scan_json_dirs_for_history("all")

        assert len(result) == 1
        assert result[0]["_direction"] == "sent"

    def test_direction_received_skips_sent(self, tmp_path: Path):
        processed = tmp_path / "processed"
        processed.mkdir()
        sent = tmp_path / "sent"
        sent.mkdir()
        (processed / "r1.json").write_text(json.dumps({"id": "r1", "text": "r"}))
        (sent / "s1.json").write_text(json.dumps({"id": "s1", "text": "s"}))

        with patch.multiple(
            "src.mcp.inbox_server",
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            result = _scan_json_dirs_for_history("received")

        assert len(result) == 1
        assert result[0]["_direction"] == "received"

    def test_direction_sent_skips_received(self, tmp_path: Path):
        processed = tmp_path / "processed"
        processed.mkdir()
        sent = tmp_path / "sent"
        sent.mkdir()
        (processed / "r1.json").write_text(json.dumps({"id": "r1", "text": "r"}))
        (sent / "s1.json").write_text(json.dumps({"id": "s1", "text": "s"}))

        with patch.multiple(
            "src.mcp.inbox_server",
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            result = _scan_json_dirs_for_history("sent")

        assert len(result) == 1
        assert result[0]["_direction"] == "sent"

    def test_skips_malformed_json_files(self, tmp_path: Path):
        processed = tmp_path / "processed"
        processed.mkdir()
        (tmp_path / "sent").mkdir()
        (processed / "bad.json").write_text("this is not json {")
        (processed / "good.json").write_text(json.dumps({"id": "ok", "text": "fine"}))

        with patch.multiple(
            "src.mcp.inbox_server",
            PROCESSED_DIR=processed,
            SENT_DIR=tmp_path / "sent",
        ):
            result = _scan_json_dirs_for_history("all")

        assert len(result) == 1
        assert result[0]["id"] == "ok"


# ---------------------------------------------------------------------------
# Tests: _apply_filters_and_paginate (pure filtering / sorting / pagination)
# ---------------------------------------------------------------------------


class TestApplyFiltersAndPaginate:
    """Unit tests for the in-memory filter / sort / paginate helper."""

    def _msgs(self):
        return [
            _make_msg("m1", chat_id="111", timestamp="2024-01-10T00:00:00+00:00", text="first"),
            _make_msg("m2", chat_id="222", timestamp="2024-01-12T00:00:00+00:00", text="second"),
            _make_msg("m3", chat_id="111", timestamp="2024-01-14T00:00:00+00:00", text="third"),
        ]

    def test_returns_all_when_no_filters(self):
        msgs = self._msgs()
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="", limit=10, offset=0
        )
        assert total == 3
        assert len(result) == 3

    def test_filters_by_chat_id(self):
        msgs = self._msgs()
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter="111", source_filter="", search_text="", limit=10, offset=0
        )
        assert total == 2
        assert all(m["chat_id"] == "111" for m in result)

    def test_filters_by_chat_id_as_int(self):
        """chat_id_filter as int should still match string chat_ids."""
        msgs = self._msgs()
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter=111, source_filter="", search_text="", limit=10, offset=0
        )
        assert total == 2

    def test_filters_by_source(self):
        msgs = [
            _make_msg("m1", source="telegram"),
            _make_msg("m2", source="slack"),
        ]
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="telegram", search_text="", limit=10, offset=0
        )
        assert total == 1
        assert result[0]["source"] == "telegram"

    def test_full_text_search(self):
        msgs = [
            _make_msg("m1", text="the quick brown fox"),
            _make_msg("m2", text="lazy dog"),
        ]
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="quick", limit=10, offset=0
        )
        assert total == 1
        assert "quick" in result[0]["text"]

    def test_search_is_case_insensitive(self):
        msgs = [_make_msg("m1", text="Hello World")]
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="HELLO", limit=10, offset=0
        )
        assert total == 1

    def test_results_sorted_newest_first(self):
        msgs = self._msgs()
        result, _ = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="", limit=10, offset=0
        )
        timestamps = [m["timestamp"] for m in result]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_pagination_limit(self):
        msgs = self._msgs()
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="", limit=2, offset=0
        )
        assert total == 3
        assert len(result) == 2

    def test_pagination_offset(self):
        msgs = self._msgs()
        page1, _ = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="", limit=2, offset=0
        )
        page2, _ = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="", limit=2, offset=2
        )
        ids_p1 = {m["id"] for m in page1}
        ids_p2 = {m["id"] for m in page2}
        assert ids_p1.isdisjoint(ids_p2), "pages must not overlap"

    def test_returns_empty_when_no_match(self):
        msgs = self._msgs()
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter="999", source_filter="", search_text="", limit=10, offset=0
        )
        assert total == 0
        assert result == []

    def test_handles_messages_without_timestamp(self):
        """Messages without a timestamp should sort to the end, not raise."""
        msgs = [
            {"id": "no-ts", "_direction": "received", "text": "no ts"},
            _make_msg("m1", timestamp="2024-01-15T00:00:00+00:00"),
        ]
        result, total = _apply_filters_and_paginate(
            msgs, chat_id_filter=None, source_filter="", search_text="", limit=10, offset=0
        )
        assert total == 2
        # The message with a timestamp comes first (newest-first, missing treated as min)
        assert result[0]["id"] == "m1"


# ---------------------------------------------------------------------------
# Tests: _format_history_output (pure string rendering)
# ---------------------------------------------------------------------------


class TestFormatHistoryOutput:
    """Unit tests for the markdown rendering helper."""

    def test_renders_header_with_count(self):
        msgs = [_make_msg("m1")]
        output = _format_history_output(msgs, total_count=1, offset=0, limit=20)
        assert "Conversation History" in output
        assert "1-1 of 1" in output

    def test_received_message_format(self):
        msgs = [_make_msg("m1", direction="received", user_name="Alice")]
        output = _format_history_output(msgs, total_count=1, offset=0, limit=20)
        assert "RECEIVED" in output
        assert "Alice" in output

    def test_sent_message_format(self):
        msgs = [_make_msg("m1", direction="sent")]
        output = _format_history_output(msgs, total_count=1, offset=0, limit=20)
        assert "SENT" in output

    def test_truncates_long_text(self):
        long_text = "x" * 600
        msgs = [_make_msg("m1", text=long_text)]
        output = _format_history_output(msgs, total_count=1, offset=0, limit=20)
        assert "..." in output
        # The rendered text block should not include all 600 chars
        assert long_text not in output

    def test_short_text_not_truncated(self):
        short_text = "short message"
        msgs = [_make_msg("m1", text=short_text)]
        output = _format_history_output(msgs, total_count=1, offset=0, limit=20)
        assert short_text in output
        assert "..." not in output

    def test_pagination_hint_shown_when_more(self):
        msgs = [_make_msg(f"m{i}") for i in range(5)]
        output = _format_history_output(msgs, total_count=10, offset=0, limit=5)
        assert "More messages available" in output
        assert "offset=5" in output

    def test_no_pagination_hint_on_last_page(self):
        msgs = [_make_msg("m1")]
        output = _format_history_output(msgs, total_count=1, offset=0, limit=20)
        assert "More messages available" not in output

    def test_offset_reflected_in_header(self):
        msgs = [_make_msg("m1")]
        output = _format_history_output(msgs, total_count=5, offset=2, limit=1)
        assert "3-3 of 5" in output


# ---------------------------------------------------------------------------
# Tests: _open_messages_db_conn
# ---------------------------------------------------------------------------


class TestOpenMessagesDbConn:
    """Unit tests for the DB connection factory helper."""

    def test_returns_none_when_db_module_unavailable(self):
        with patch("src.mcp.inbox_server._db_open_messages_db", None):
            result = _open_messages_db_conn()
        assert result is None

    def test_returns_none_when_db_file_missing(self, tmp_path: Path):
        missing_path = tmp_path / "nonexistent.db"
        with patch.multiple(
            "src.mcp.inbox_server",
            _db_open_messages_db=MagicMock(),
            MESSAGES_DB_PATH=missing_path,
        ):
            result = _open_messages_db_conn()
        assert result is None

    def test_returns_connection_when_db_exists(self, tmp_path: Path):
        db_path = tmp_path / "messages.db"
        # Create a real SQLite DB file
        conn = sqlite3.connect(str(db_path))
        conn.close()

        real_conn = sqlite3.connect(str(db_path))

        def _fake_open(path):
            return real_conn

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_open_messages_db=_fake_open,
            MESSAGES_DB_PATH=db_path,
        ):
            result = _open_messages_db_conn()

        assert result is real_conn
        real_conn.close()

    def test_returns_none_on_open_exception(self, tmp_path: Path):
        db_path = tmp_path / "messages.db"
        db_path.touch()  # exists but open raises

        def _bad_open(path):
            raise OSError("disk error")

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_open_messages_db=_bad_open,
            MESSAGES_DB_PATH=db_path,
        ):
            result = _open_messages_db_conn()

        assert result is None


# ---------------------------------------------------------------------------
# Tests: handle_get_conversation_history (end-to-end handler integration)
# ---------------------------------------------------------------------------


class TestHandleGetConversationHistorySql:
    """Integration tests for the BIS-165 Slice 4 handler."""

    def test_db_path_is_primary_source(self, tmp_path: Path):
        """When the DB returns rows, output reflects DB data not filesystem."""
        db_rows = [
            _make_msg("db1", text="From DB"),
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
        mock_conn.close.assert_called_once()

    def test_filesystem_fallback_when_db_conn_is_none(self, tmp_path: Path):
        """When _open_messages_db_conn returns None, filesystem JSON is used."""
        processed = tmp_path / "processed"
        processed.mkdir()
        (tmp_path / "sent").mkdir()
        msg = _make_msg("fs1", text="Filesystem message")
        (processed / "fs1.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=MagicMock(return_value=[]),
            _db_count_conversation_history=MagicMock(return_value=0),
            _open_messages_db_conn=MagicMock(return_value=None),
            PROCESSED_DIR=processed,
            SENT_DIR=tmp_path / "sent",
        ):
            result = asyncio.run(handle_get_conversation_history({}))

        assert "Filesystem message" in result[0].text

    def test_filesystem_fallback_when_db_reader_not_imported(self, tmp_path: Path):
        """When db reader module not imported (_db_get... is None), filesystem used."""
        processed = tmp_path / "processed"
        processed.mkdir()
        (tmp_path / "sent").mkdir()
        msg = _make_msg("fs2", text="no db reader")
        (processed / "fs2.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=None,
            _db_count_conversation_history=None,
            PROCESSED_DIR=processed,
            SENT_DIR=tmp_path / "sent",
        ):
            result = asyncio.run(handle_get_conversation_history({}))

        assert "no db reader" in result[0].text

    def test_no_messages_found_message(self, tmp_path: Path):
        """When no messages exist in DB or filesystem, returns a 'no messages' response."""
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
            result = asyncio.run(handle_get_conversation_history({}))

        assert "No messages found" in result[0].text

    def test_chat_id_filter_propagated_to_db(self):
        """The chat_id arg is passed to _db_get_conversation_history."""
        mock_get = MagicMock(return_value=[_make_msg("m1")])
        mock_count = MagicMock(return_value=1)
        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=mock_get,
            _db_count_conversation_history=mock_count,
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
        ):
            asyncio.run(handle_get_conversation_history({"chat_id": "999"}))

        _, kwargs = mock_get.call_args
        assert kwargs["chat_id"] == "999"

    def test_limit_capped_at_100(self):
        """Requests for limit > 100 are silently capped."""
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
            asyncio.run(handle_get_conversation_history({"limit": 500}))

        _, kwargs = mock_get.call_args
        assert kwargs["limit"] <= 100

    def test_db_exception_triggers_filesystem_fallback(self, tmp_path: Path):
        """If DB query raises, filesystem fallback is used (no exception propagated)."""
        processed = tmp_path / "processed"
        processed.mkdir()
        (tmp_path / "sent").mkdir()
        msg = _make_msg("fallback", text="after db error")
        (processed / "fallback.json").write_text(json.dumps(msg))

        def _raise(*args, **kwargs):
            raise RuntimeError("DB exploded")

        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=_raise,
            _db_count_conversation_history=MagicMock(return_value=0),
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
            PROCESSED_DIR=processed,
            SENT_DIR=tmp_path / "sent",
        ):
            result = asyncio.run(handle_get_conversation_history({}))

        # Should not raise; should have fallen back to filesystem
        assert "after db error" in result[0].text
        # Connection must be closed even after exception
        mock_conn.close.assert_called_once()

    def test_filter_info_in_no_messages_response(self, tmp_path: Path):
        """'No messages found' response includes filter description."""
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
            result = asyncio.run(handle_get_conversation_history({
                "chat_id": "777",
                "search": "needle",
            }))

        text = result[0].text
        assert "chat_id=777" in text
        assert "needle" in text
