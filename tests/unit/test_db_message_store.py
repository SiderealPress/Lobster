"""
tests/unit/test_db_message_store.py — Unit tests for src/db/message_store.py (BIS-167).

Tests cover:
  - classify(): pure routing logic
  - build_message_row(): pure dict transform
  - build_bisque_event_row(): pure dict transform
  - build_agent_event_row(): pure dict transform
  - _coerce(): type coercion helper
  - persist_message() / persist_inbound() / persist_outbound() no-op when disabled
  - DB round-trip when LOBSTER_USE_DB=1 (in-memory SQLite)

All tests are pure-functional: no real filesystem access for the core helpers.
The integration-style test (DB round-trip) uses a tmp_path fixture with an
in-memory SQLite connection to stay hermetic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ is on sys.path
_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from db.message_store import (
    _coerce,
    _split,
    _AGENT_FIELDS,
    _BISQUE_FIELDS,
    _MESSAGE_FIELDS,
    _SKIP_INTERNAL,
    build_agent_event_row,
    build_bisque_event_row,
    build_message_row,
    classify,
)


# ---------------------------------------------------------------------------
# _coerce — pure type coercion
# ---------------------------------------------------------------------------


class TestCoerce:
    def test_bool_true_becomes_one(self):
        assert _coerce(True) == 1

    def test_bool_false_becomes_zero(self):
        assert _coerce(False) == 0

    def test_list_becomes_json_string(self):
        result = _coerce(["a", "b"])
        assert result == '["a", "b"]'

    def test_dict_becomes_json_string(self):
        result = _coerce({"key": "val"})
        parsed = json.loads(result)
        assert parsed == {"key": "val"}

    def test_string_passthrough(self):
        assert _coerce("hello") == "hello"

    def test_int_passthrough(self):
        assert _coerce(42) == 42

    def test_none_passthrough(self):
        assert _coerce(None) is None


# ---------------------------------------------------------------------------
# _split — field partitioning (pure, non-mutating)
# ---------------------------------------------------------------------------


class TestSplit:
    def test_known_fields_go_to_row(self):
        msg = {"id": "abc", "text": "hello"}
        row, overflow = _split(msg, frozenset({"id", "text"}))
        assert row == {"id": "abc", "text": "hello"}
        assert overflow == {}

    def test_unknown_fields_go_to_overflow(self):
        msg = {"id": "abc", "custom_field": "x"}
        row, overflow = _split(msg, frozenset({"id"}))
        assert row == {"id": "abc"}
        assert overflow == {"custom_field": "x"}

    def test_internal_fields_excluded_entirely(self):
        msg = {"id": "abc", "_processing_started_at": "2024-01-01"}
        row, overflow = _split(msg, frozenset({"id"}))
        assert "_processing_started_at" not in row
        assert "_processing_started_at" not in overflow

    def test_does_not_mutate_input(self):
        original = {"id": "abc", "text": "hi", "_retry_count": 3}
        original_copy = dict(original)
        _split(original, _MESSAGE_FIELDS)
        assert original == original_copy

    def test_bool_coerced_in_row(self):
        msg = {"id": "abc", "sent_reply_to_user": True}
        row, _ = _split(msg, frozenset({"id", "sent_reply_to_user"}))
        assert row["sent_reply_to_user"] == 1  # bool -> int


# ---------------------------------------------------------------------------
# classify — routing logic (pure)
# ---------------------------------------------------------------------------


class TestClassify:
    def test_agent_event_types_route_to_agent_events(self):
        for t in ("subagent_result", "subagent_notification", "subagent_error",
                  "agent_failed", "task-notification"):
            assert classify({"type": t}, "in") == "agent_events"

    def test_bisque_source_routes_to_bisque_events(self):
        assert classify({"source": "bisque"}, "in") == "bisque_events"

    def test_plain_message_routes_to_messages(self):
        assert classify({"source": "telegram", "type": "text"}, "in") == "messages"

    def test_agent_event_type_takes_priority_over_bisque_source(self):
        # A bisque message that is also a subagent_result should go to agent_events
        record = {"source": "bisque", "type": "subagent_result"}
        assert classify(record, "in") == "agent_events"

    def test_empty_record_routes_to_messages(self):
        assert classify({}, "in") == "messages"


# ---------------------------------------------------------------------------
# build_message_row — pure dict transform
# ---------------------------------------------------------------------------


class TestBuildMessageRow:
    def test_direction_is_injected(self):
        row = build_message_row({"id": "x", "text": "hi"}, "out")
        assert row["direction"] == "out"

    def test_known_fields_preserved(self):
        msg = {
            "id": "msg-1",
            "source": "telegram",
            "chat_id": 12345,
            "text": "Hello world",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        row = build_message_row(msg, "in")
        assert row["id"] == "msg-1"
        assert row["source"] == "telegram"
        assert row["chat_id"] == 12345
        assert row["text"] == "Hello world"

    def test_overflow_fields_serialised_as_extra(self):
        msg = {"id": "x", "custom_flag": "foo"}
        row = build_message_row(msg, "in")
        assert "extra" in row
        extra = json.loads(row["extra"])
        assert extra["custom_flag"] == "foo"

    def test_no_overflow_no_extra_key(self):
        msg = {"id": "x"}
        row = build_message_row(msg, "in")
        assert "extra" not in row

    def test_internal_skip_fields_excluded(self):
        msg = {"id": "x", "_permanently_failed": True, "_retry_count": 2}
        row = build_message_row(msg, "in")
        assert "_permanently_failed" not in row
        assert "_retry_count" not in row

    def test_does_not_mutate_input(self):
        msg = {"id": "x", "text": "hi"}
        original = dict(msg)
        build_message_row(msg, "in")
        assert msg == original


# ---------------------------------------------------------------------------
# build_bisque_event_row — pure dict transform
# ---------------------------------------------------------------------------


class TestBuildBisqueEventRow:
    def test_id_preserved(self):
        row = build_bisque_event_row({"id": "b-1", "chat_id": 9})
        assert row["id"] == "b-1"

    def test_timestamp_defaulted_when_absent(self):
        row = build_bisque_event_row({"id": "b-1"})
        # setdefault("timestamp", "") means timestamp key exists, value may be ""
        assert "timestamp" in row


# ---------------------------------------------------------------------------
# build_agent_event_row — pure dict transform
# ---------------------------------------------------------------------------


class TestBuildAgentEventRow:
    def test_type_defaulted_when_absent(self):
        row = build_agent_event_row({"id": "ae-1"})
        assert row["type"] == "unknown"

    def test_artifacts_list_becomes_json_string(self):
        msg = {"id": "ae-1", "type": "subagent_result", "artifacts": ["url1", "url2"]}
        row = build_agent_event_row(msg)
        # _coerce converts list to JSON string
        assert isinstance(row.get("artifacts"), str)
        parsed = json.loads(row["artifacts"])
        assert parsed == ["url1", "url2"]

    def test_artifacts_already_json_string_preserved(self):
        msg = {"id": "ae-1", "type": "subagent_result", "artifacts": '["url1"]'}
        row = build_agent_event_row(msg)
        # Already a valid JSON string — should remain as-is
        assert row["artifacts"] == '["url1"]'

    def test_sent_reply_to_user_coerced_to_int(self):
        msg = {"id": "ae-1", "type": "x", "sent_reply_to_user": True}
        row = build_agent_event_row(msg)
        assert row["sent_reply_to_user"] == 1


# ---------------------------------------------------------------------------
# Feature flag: persist_* functions are no-ops when LOBSTER_USE_DB != "1"
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    def test_persist_message_is_noop_when_disabled(self):
        """When LOBSTER_USE_DB is not set, persist_message must not call _write_to_db."""
        # Re-import the module with env var unset so _DB_ENABLED=False
        import importlib
        env_backup = os.environ.pop("LOBSTER_USE_DB", None)
        try:
            import db.message_store as ms
            # Force _DB_ENABLED to False (flag evaluated at import time)
            with patch.object(ms, "_DB_ENABLED", False):
                with patch.object(ms, "_write_to_db") as mock_write:
                    ms.persist_message({"id": "x", "text": "hi"}, "in")
                    mock_write.assert_not_called()
        finally:
            if env_backup is not None:
                os.environ["LOBSTER_USE_DB"] = env_backup

    def test_persist_inbound_delegates_to_persist_message(self):
        import db.message_store as ms
        with patch.object(ms, "persist_message") as mock_pm:
            ms.persist_inbound({"id": "x"})
            mock_pm.assert_called_once_with({"id": "x"}, "in")

    def test_persist_outbound_delegates_to_persist_message(self):
        import db.message_store as ms
        with patch.object(ms, "persist_message") as mock_pm:
            ms.persist_outbound({"id": "x"})
            mock_pm.assert_called_once_with({"id": "x"}, "out")

    def test_persist_agent_event_is_noop_when_disabled(self):
        import db.message_store as ms
        with patch.object(ms, "_DB_ENABLED", False):
            with patch.object(ms, "_write_to_db") as mock_write:
                ms.persist_agent_event({"id": "x", "type": "subagent_result"})
                mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# DB round-trip (in-memory SQLite — no real filesystem writes)
# ---------------------------------------------------------------------------


class TestDBRoundTrip:
    """Verify the full persist pipeline writes correct rows to SQLite.

    Uses a real temp-file SQLite database (not in-memory) because _write_to_db
    opens a fresh connection per call and closes it on exit.  The temp DB file
    persists across individual _write_to_db calls so we can read back the rows
    after the fact.
    """

    @pytest.fixture
    def temp_db(self, tmp_path):
        """Return a path to a fresh SQLite DB with the schema applied."""
        schema_path = _SRC / "db" / "schema.sql"
        if not schema_path.exists():
            pytest.skip("schema.sql not found")

        db_path = tmp_path / "messages.db"
        schema_sql = schema_path.read_text(encoding="utf-8")
        conn = sqlite3.connect(str(db_path))
        conn.executescript(schema_sql)
        conn.close()
        return db_path

    def _read_row(self, db_path: Path, table: str, msg_id: str):
        """Open a fresh read-only connection and fetch a row by id."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (msg_id,)
        ).fetchone()
        conn.close()
        return row

    def test_message_row_inserted(self, temp_db):
        import db.message_store as ms

        record = {
            "id": "test-msg-1",
            "source": "telegram",
            "chat_id": 123,
            "text": "Hello",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }

        with patch.object(ms, "_DB_ENABLED", True), \
             patch.object(ms, "_db_path", return_value=temp_db), \
             patch.object(ms, "_schema_applied", True):
            ms.persist_inbound(record)

        row = self._read_row(temp_db, "messages", "test-msg-1")
        assert row is not None
        assert row["id"] == "test-msg-1"
        assert row["direction"] == "in"
        assert row["source"] == "telegram"
        assert row["text"] == "Hello"

    def test_insert_or_ignore_idempotent(self, temp_db):
        """Double-persist of the same id must not raise or duplicate."""
        import db.message_store as ms

        record = {"id": "dupe-msg", "source": "telegram", "chat_id": 1, "text": "hi"}

        with patch.object(ms, "_DB_ENABLED", True), \
             patch.object(ms, "_db_path", return_value=temp_db), \
             patch.object(ms, "_schema_applied", True):
            ms.persist_inbound(record)
            ms.persist_inbound(record)  # Second call must be a no-op

        conn = sqlite3.connect(str(temp_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE id = ?", ("dupe-msg",)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_agent_event_row_inserted(self, temp_db):
        import db.message_store as ms

        record = {
            "id": "ae-test-1",
            "type": "subagent_result",
            "source": "telegram",
            "chat_id": 1234567890,
            "task_id": "task-abc",
            "text": "Done!",
            "status": "success",
            "sent_reply_to_user": False,
            "timestamp": "2024-01-01T00:00:00+00:00",
        }

        with patch.object(ms, "_DB_ENABLED", True), \
             patch.object(ms, "_db_path", return_value=temp_db), \
             patch.object(ms, "_schema_applied", True):
            ms.persist_agent_event(record)

        row = self._read_row(temp_db, "agent_events", "ae-test-1")
        assert row is not None
        assert row["id"] == "ae-test-1"
        assert row["type"] == "subagent_result"
        assert row["task_id"] == "task-abc"
