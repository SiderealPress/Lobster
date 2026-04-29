"""
Tests for since_ts parameter in get_conversation_history (issue #1825).

Verifies that:
- get_conversation_history accepts a since_ts parameter
- Messages older than since_ts are excluded from DB query results
- Messages at or after since_ts are included
- count_conversation_history also respects since_ts
- handle_get_conversation_history propagates since_ts to the DB reader
- _apply_filters_and_paginate respects since_ts in the filesystem fallback path

The since_ts parameter closes the secondary failure path where high-volume
sessions can push replies off the page when get_conversation_history is called
without a time bound (fixed limit=20 default).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ and src/db/ and src/mcp/ are importable.
_SRC_DIR = Path(__file__).parent.parent.parent.parent / "src"
_MCP_DIR = _SRC_DIR / "mcp"
_DB_DIR = _SRC_DIR / "db"
for _d in (_SRC_DIR, _MCP_DIR, _DB_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# Pre-load inbox_server so patch.multiple resolves it.
import src.mcp.inbox_server  # noqa: F401
from src.mcp.inbox_server import (
    handle_get_conversation_history,
    _apply_filters_and_paginate,
)
from src.db.reader import get_conversation_history, count_conversation_history


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_BEFORE_TS = _BASE_TS - timedelta(hours=2)   # 10:00 — old reply
_WINDOW_START = _BASE_TS                      # 12:00 — since_ts cutoff
_AFTER_TS = _BASE_TS + timedelta(hours=1)    # 13:00 — new message

_WINDOW_START_ISO = "2026-01-01T12:00:00Z"
_BEFORE_TS_ISO = "2026-01-01T10:00:00.000000"
_AFTER_TS_ISO = "2026-01-01T13:00:00.000000"


# ---------------------------------------------------------------------------
# In-memory DB helpers
# ---------------------------------------------------------------------------

def _make_in_memory_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the messages schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            direction TEXT DEFAULT 'in',
            source TEXT,
            type TEXT,
            chat_id TEXT,
            user_id TEXT,
            username TEXT,
            user_name TEXT,
            text TEXT,
            reply_to TEXT,
            reply_to_message_id TEXT,
            image_file TEXT,
            image_width TEXT,
            image_height TEXT,
            audio_file TEXT,
            audio_duration TEXT,
            audio_mime_type TEXT,
            transcription TEXT,
            transcribed_at TEXT,
            transcription_model TEXT,
            file_path TEXT,
            file_name TEXT,
            mime_type TEXT,
            file_size TEXT,
            telegram_message_id TEXT,
            callback_data TEXT,
            callback_query_id TEXT,
            original_message_id TEXT,
            original_message_text TEXT,
            media_group_id TEXT,
            timestamp TEXT,
            extra TEXT
        )
    """)
    return conn


def _insert_msg(
    conn: sqlite3.Connection,
    msg_id: str,
    ts_iso: str,
    direction: str = "in",
    text: str = "hello",
    chat_id: str = "111",
    source: str = "telegram",
) -> None:
    conn.execute(
        "INSERT INTO messages (id, direction, source, chat_id, text, timestamp, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, direction, source, chat_id, text, ts_iso, "text"),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: get_conversation_history with since_ts (DB layer)
# ---------------------------------------------------------------------------


class TestGetConversationHistoryWithSinceTs:
    """Tests for since_ts filtering in the DB reader layer."""

    def test_since_ts_excludes_messages_before_window(self):
        """Messages with timestamp before since_ts are not returned."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "old-reply", _BEFORE_TS_ISO)
        _insert_msg(conn, "new-msg", _AFTER_TS_ISO)

        rows = get_conversation_history(conn, since_ts=_WINDOW_START_ISO)
        ids = {r["id"] for r in rows}

        assert "new-msg" in ids
        assert "old-reply" not in ids

    def test_since_ts_includes_messages_at_boundary(self):
        """A message exactly at since_ts is included (>= boundary)."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "boundary-msg", "2026-01-01T12:00:00.000000")

        rows = get_conversation_history(conn, since_ts=_WINDOW_START_ISO)
        ids = {r["id"] for r in rows}

        assert "boundary-msg" in ids

    def test_since_ts_returns_all_within_window(self):
        """Multiple messages within the window are all returned."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "m1", _AFTER_TS_ISO)
        _insert_msg(conn, "m2", _AFTER_TS_ISO)
        _insert_msg(conn, "old", _BEFORE_TS_ISO)

        rows = get_conversation_history(conn, since_ts=_WINDOW_START_ISO)
        ids = {r["id"] for r in rows}

        assert "m1" in ids
        assert "m2" in ids
        assert "old" not in ids

    def test_since_ts_compatible_with_direction_filter(self):
        """since_ts and direction='sent' can be combined."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "old-in", _BEFORE_TS_ISO, direction="in")
        _insert_msg(conn, "new-in", _AFTER_TS_ISO, direction="in")
        _insert_msg(conn, "new-out", _AFTER_TS_ISO, direction="out")

        rows = get_conversation_history(conn, since_ts=_WINDOW_START_ISO, direction="sent")
        ids = {r["id"] for r in rows}

        assert "new-out" in ids
        assert "new-in" not in ids
        assert "old-in" not in ids

    def test_since_ts_compatible_with_chat_id_filter(self):
        """since_ts and chat_id can be combined."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "right-chat", _AFTER_TS_ISO, chat_id="111")
        _insert_msg(conn, "wrong-chat", _AFTER_TS_ISO, chat_id="222")
        _insert_msg(conn, "old-right", _BEFORE_TS_ISO, chat_id="111")

        rows = get_conversation_history(conn, since_ts=_WINDOW_START_ISO, chat_id="111")
        ids = {r["id"] for r in rows}

        assert "right-chat" in ids
        assert "wrong-chat" not in ids
        assert "old-right" not in ids

    def test_without_since_ts_returns_latest_n_regardless_of_age(self):
        """Without since_ts, old messages are still returned (existing behavior preserved)."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "very-old", _BEFORE_TS_ISO)

        rows = get_conversation_history(conn)  # no since_ts
        ids = {r["id"] for r in rows}

        assert "very-old" in ids


# ---------------------------------------------------------------------------
# Tests: count_conversation_history with since_ts (DB layer)
# ---------------------------------------------------------------------------


class TestCountConversationHistoryWithSinceTs:
    """Tests for since_ts filtering in count_conversation_history."""

    def test_count_excludes_messages_before_window(self):
        """count_conversation_history with since_ts counts only in-window messages."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "old", _BEFORE_TS_ISO)
        _insert_msg(conn, "new1", _AFTER_TS_ISO)
        _insert_msg(conn, "new2", _AFTER_TS_ISO)

        count = count_conversation_history(conn, since_ts=_WINDOW_START_ISO)
        assert count == 2

    def test_count_without_since_ts_counts_all(self):
        """Without since_ts, count_conversation_history counts all messages."""
        conn = _make_in_memory_db()
        _insert_msg(conn, "old", _BEFORE_TS_ISO)
        _insert_msg(conn, "new", _AFTER_TS_ISO)

        count = count_conversation_history(conn)
        assert count == 2


# ---------------------------------------------------------------------------
# Tests: _apply_filters_and_paginate with since_ts (filesystem fallback)
# ---------------------------------------------------------------------------


def _make_fs_msg(
    msg_id: str,
    ts: str,
    direction: str = "received",
    chat_id: str = "111",
) -> dict:
    return {
        "id": msg_id,
        "_direction": direction,
        "source": "telegram",
        "chat_id": chat_id,
        "timestamp": ts,
        "text": f"text for {msg_id}",
    }


class TestApplyFiltersWithSinceTs:
    """Tests for since_ts in the filesystem-fallback filter helper."""

    def test_since_ts_filters_old_messages_in_filesystem_path(self):
        """Messages older than since_ts are excluded by _apply_filters_and_paginate."""
        msgs = [
            _make_fs_msg("old-reply", _BEFORE_TS_ISO, direction="sent"),
            _make_fs_msg("new-user", _AFTER_TS_ISO, direction="received"),
        ]
        result, total = _apply_filters_and_paginate(
            msgs,
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=10,
            offset=0,
            since_ts=_WINDOW_START_ISO,
        )
        ids = {m["id"] for m in result}
        assert "new-user" in ids
        assert "old-reply" not in ids
        assert total == 1

    def test_since_ts_boundary_included_in_filesystem_path(self):
        """Message at exact since_ts boundary is included."""
        msgs = [_make_fs_msg("exact", "2026-01-01T12:00:00.000000")]
        result, total = _apply_filters_and_paginate(
            msgs,
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=10,
            offset=0,
            since_ts=_WINDOW_START_ISO,
        )
        assert total == 1
        assert result[0]["id"] == "exact"

    def test_without_since_ts_no_time_filtering(self):
        """Without since_ts, _apply_filters_and_paginate returns all messages."""
        msgs = [
            _make_fs_msg("old", _BEFORE_TS_ISO),
            _make_fs_msg("new", _AFTER_TS_ISO),
        ]
        result, total = _apply_filters_and_paginate(
            msgs,
            chat_id_filter=None,
            source_filter="",
            search_text="",
            limit=10,
            offset=0,
        )
        assert total == 2


# ---------------------------------------------------------------------------
# Tests: handle_get_conversation_history propagates since_ts
# ---------------------------------------------------------------------------


class TestHandleGetConversationHistoryWithSinceTs:
    """Tests that handle_get_conversation_history propagates since_ts correctly."""

    def test_since_ts_propagated_to_db_reader(self):
        """since_ts arg is forwarded to _db_get_conversation_history."""
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
            asyncio.run(handle_get_conversation_history({"since_ts": _WINDOW_START_ISO}))

        _, kwargs = mock_get.call_args
        assert kwargs.get("since_ts") == _WINDOW_START_ISO

    def test_since_ts_propagated_to_count_reader(self):
        """since_ts arg is forwarded to _db_count_conversation_history."""
        mock_get = MagicMock(return_value=[{"id": "m1", "_direction": "received",
                                            "text": "hi", "timestamp": _AFTER_TS_ISO,
                                            "source": "telegram", "chat_id": "111",
                                            "user_name": "u", "username": "u"}])
        mock_count = MagicMock(return_value=1)
        mock_conn = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=mock_get,
            _db_count_conversation_history=mock_count,
            _open_messages_db_conn=MagicMock(return_value=mock_conn),
        ):
            asyncio.run(handle_get_conversation_history({"since_ts": _WINDOW_START_ISO}))

        _, kwargs = mock_count.call_args
        assert kwargs.get("since_ts") == _WINDOW_START_ISO

    def test_since_ts_in_filesystem_fallback(self, tmp_path: Path):
        """since_ts filters filesystem messages when DB is unavailable."""
        processed = tmp_path / "processed"
        processed.mkdir()
        sent = tmp_path / "sent"
        sent.mkdir()

        old_msg = {"id": "old-reply", "text": "old message text", "timestamp": _BEFORE_TS_ISO,
                   "source": "telegram", "chat_id": "111"}
        new_msg = {"id": "new-msg", "text": "new message text", "timestamp": _AFTER_TS_ISO,
                   "source": "telegram", "chat_id": "111"}
        (processed / "old-reply.json").write_text(json.dumps(old_msg))
        (processed / "new-msg.json").write_text(json.dumps(new_msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            _db_get_conversation_history=None,
            _db_count_conversation_history=None,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            result = asyncio.run(handle_get_conversation_history(
                {"since_ts": _WINDOW_START_ISO}
            ))

        text = result[0].text
        # The formatted output includes message text content; assert on unique text strings.
        assert "new message text" in text
        # Only 1 of 2 messages passes the since_ts filter — "old message text" is excluded.
        assert "old message text" not in text
        assert "showing 1-1 of 1" in text
