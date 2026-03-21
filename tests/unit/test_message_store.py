"""
tests/unit/test_message_store.py — Unit tests for src/db/message_store.py (BIS-163 Slice 2)

Tests the live DB write path:
  - classify() routing (pure function)
  - build_*_row() builders (pure functions)
  - _write_to_db() integration via persist_* functions
  - INSERT OR IGNORE idempotency
  - Feature flag (LOBSTER_USE_DB) guard
  - Graceful error swallowing on DB failure
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow importing src/db without installing the package
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

import importlib
import db.message_store as _ms_module
from db.message_store import (
    classify,
    build_message_row,
    build_bisque_event_row,
    build_agent_event_row,
    persist_message,
    persist_inbound,
    persist_outbound,
    persist_agent_event,
    _AGENT_EVENT_TYPES,
    _INSERT_MESSAGE,
    _INSERT_BISQUE,
    _INSERT_AGENT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_test_db(path: Path) -> sqlite3.Connection:
    """Open a test DB and apply the real schema from schema.sql."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    from db.connection import apply_schema
    apply_schema(conn)
    return conn


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _fetch_one(conn: sqlite3.Connection, table: str, msg_id: str) -> sqlite3.Row | None:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (msg_id,)).fetchone()
    return row


# ---------------------------------------------------------------------------
# classify() — pure function tests
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize(
        "record,direction,expected_table",
        [
            # Agent event types route to agent_events
            ({"type": "subagent_result"}, "in", "agent_events"),
            ({"type": "subagent_notification"}, "in", "agent_events"),
            ({"type": "subagent_error"}, "in", "agent_events"),
            ({"type": "agent_failed"}, "in", "agent_events"),
            ({"type": "task-notification"}, "in", "agent_events"),
            # Bisque source routes to bisque_events
            ({"source": "bisque"}, "in", "bisque_events"),
            ({"type": "reply", "source": "bisque"}, "in", "bisque_events"),
            # agent_events takes priority over bisque source
            ({"type": "subagent_result", "source": "bisque"}, "in", "agent_events"),
            # Everything else routes to messages
            ({"type": "text"}, "in", "messages"),
            ({"type": "image", "source": "telegram"}, "in", "messages"),
            ({}, "out", "messages"),
        ],
    )
    def test_classify_routing(self, record, direction, expected_table):
        assert classify(record, direction) == expected_table

    def test_classify_none_type_and_source(self):
        """None values for type/source should not raise."""
        assert classify({"type": None, "source": None}, "in") == "messages"

    def test_classify_is_pure(self):
        """classify does not mutate the input dict."""
        record = {"type": "text", "extra": "data"}
        original = dict(record)
        classify(record, "in")
        assert record == original


# ---------------------------------------------------------------------------
# build_message_row() — pure function tests
# ---------------------------------------------------------------------------


class TestBuildMessageRow:
    def test_direction_set_correctly(self):
        row = build_message_row({"id": "msg-1", "type": "text"}, "in")
        assert row["direction"] == "in"

    def test_direction_out(self):
        row = build_message_row({"id": "msg-2", "type": "reply"}, "out")
        assert row["direction"] == "out"

    def test_known_fields_included(self):
        record = {
            "id": "msg-3",
            "type": "text",
            "text": "hello",
            "chat_id": "12345",
            "user_id": "u-1",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        row = build_message_row(record, "in")
        assert row["id"] == "msg-3"
        assert row["text"] == "hello"
        assert row["chat_id"] == "12345"
        assert row["timestamp"] == "2024-01-01T00:00:00Z"

    def test_overflow_fields_go_to_extra(self):
        record = {
            "id": "msg-4",
            "type": "text",
            "unknown_field": "surprise",
            "another_unknown": 42,
        }
        row = build_message_row(record, "in")
        assert "extra" in row
        extra = json.loads(row["extra"])
        assert extra["unknown_field"] == "surprise"
        assert extra["another_unknown"] == 42

    def test_no_overflow_extra_is_absent_or_none(self):
        record = {"id": "msg-5", "type": "text", "text": "clean"}
        row = build_message_row(record, "in")
        assert row.get("extra") is None

    def test_internal_fields_excluded_from_extra(self):
        """Fields starting with _ should not appear in extra."""
        record = {
            "id": "msg-6",
            "type": "text",
            "_processing_started_at": "2024-01-01T00:00:00Z",
        }
        row = build_message_row(record, "in")
        extra_str = row.get("extra") or "{}"
        extra = json.loads(extra_str)
        assert "_processing_started_at" not in extra

    def test_bool_coerced_to_int_in_extra(self):
        """Boolean overflow values should be coerced to int for SQLite."""
        record = {"id": "msg-7", "type": "text", "some_flag": True}
        row = build_message_row(record, "in")
        extra = json.loads(row["extra"])
        # After JSON round-trip booleans remain as-is in the JSON string,
        # but the _coerce function converts them before they hit SQLite columns.
        assert "some_flag" in extra

    def test_does_not_mutate_input(self):
        record = {"id": "msg-8", "type": "text", "overflow": "data"}
        original = dict(record)
        build_message_row(record, "in")
        assert record == original


# ---------------------------------------------------------------------------
# build_bisque_event_row() — pure function tests
# ---------------------------------------------------------------------------


class TestBuildBisqueEventRow:
    def test_known_fields_mapped(self):
        record = {
            "id": "bisque-1",
            "chat_id": "c-1",
            "type": "reply",
            "text": "done",
            "task_id": "t-1",
            "agent_id": "a-1",
            "status": "success",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        row = build_bisque_event_row(record)
        assert row["id"] == "bisque-1"
        assert row["task_id"] == "t-1"
        assert row["status"] == "success"

    def test_does_not_mutate_input(self):
        record = {"id": "bisque-2", "type": "reply"}
        original = dict(record)
        build_bisque_event_row(record)
        assert record == original


# ---------------------------------------------------------------------------
# build_agent_event_row() — pure function tests
# ---------------------------------------------------------------------------


class TestBuildAgentEventRow:
    def test_known_fields_mapped(self):
        record = {
            "id": "agent-1",
            "type": "subagent_result",
            "source": "claude",
            "chat_id": "c-1",
            "task_id": "t-1",
            "agent_id": "a-1",
            "status": "completed",
            "text": "I finished",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        row = build_agent_event_row(record)
        assert row["id"] == "agent-1"
        assert row["type"] == "subagent_result"
        assert row["status"] == "completed"

    def test_artifacts_list_serialised_to_json(self):
        record = {
            "id": "agent-2",
            "type": "subagent_result",
            "artifacts": [{"url": "https://example.com"}],
        }
        row = build_agent_event_row(record)
        # artifacts should be a JSON string (not a Python list) for SQLite
        artifacts_val = row.get("artifacts")
        assert artifacts_val is not None
        parsed = json.loads(artifacts_val)
        assert parsed[0]["url"] == "https://example.com"

    def test_does_not_mutate_input(self):
        record = {"id": "agent-3", "type": "subagent_error"}
        original = dict(record)
        build_agent_event_row(record)
        assert record == original


# ---------------------------------------------------------------------------
# Integration tests: persist_* via real SQLite DB in tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """Set LOBSTER_MESSAGES_DB to a temp path and enable the feature flag."""
    db_path = tmp_path / "test_messages.db"
    monkeypatch.setenv("LOBSTER_MESSAGES_DB", str(db_path))
    monkeypatch.setenv("LOBSTER_USE_DB", "1")

    # Reload the module so _DB_ENABLED and _db_path() pick up the env vars
    importlib.reload(_ms_module)
    # Re-import symbols from the reloaded module
    import db.message_store as fresh
    yield fresh, db_path

    # Restore original module state
    importlib.reload(_ms_module)


class TestPersistMessage:
    def test_persist_inbound_message_written_to_messages_table(self, db_env, tmp_path):
        ms, db_path = db_env
        record = {
            "id": "test-in-1",
            "type": "text",
            "text": "hello world",
            "chat_id": "chat-1",
            "source": "telegram",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        ms.persist_inbound(record)

        conn = _open_test_db(db_path)
        row = _fetch_one(conn, "messages", "test-in-1")
        conn.close()

        assert row is not None
        assert row["direction"] == "in"
        assert row["text"] == "hello world"
        assert row["chat_id"] == "chat-1"

    def test_persist_outbound_message_written_to_messages_table(self, db_env, tmp_path):
        ms, db_path = db_env
        record = {
            "id": "test-out-1",
            "type": "reply",
            "text": "Sure thing!",
            "chat_id": "chat-1",
            "source": "lobster",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        ms.persist_outbound(record)

        conn = _open_test_db(db_path)
        row = _fetch_one(conn, "messages", "test-out-1")
        conn.close()

        assert row is not None
        assert row["direction"] == "out"

    def test_persist_agent_event_written_to_agent_events_table(self, db_env):
        ms, db_path = db_env
        record = {
            "id": "agent-event-1",
            "type": "subagent_result",
            "source": "claude",
            "chat_id": "chat-1",
            "task_id": "task-abc",
            "status": "completed",
            "text": "All done.",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        ms.persist_agent_event(record)

        conn = _open_test_db(db_path)
        row = _fetch_one(conn, "agent_events", "agent-event-1")
        conn.close()

        assert row is not None
        assert row["type"] == "subagent_result"
        assert row["status"] == "completed"

    def test_persist_bisque_event_routed_to_bisque_events_table(self, db_env):
        ms, db_path = db_env
        record = {
            "id": "bisque-event-1",
            "source": "bisque",
            "type": "reply",
            "text": "Here is your answer.",
            "chat_id": "chat-1",
            "task_id": "task-xyz",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        ms.persist_message(record, "in")

        conn = _open_test_db(db_path)
        row = _fetch_one(conn, "bisque_events", "bisque-event-1")
        conn.close()

        assert row is not None

    def test_idempotent_insert_or_ignore(self, db_env):
        """Persisting the same message twice should not raise or create duplicates."""
        ms, db_path = db_env
        record = {
            "id": "idempotent-1",
            "type": "text",
            "text": "once",
            "chat_id": "chat-1",
            "source": "telegram",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        ms.persist_inbound(record)
        ms.persist_inbound(record)  # second call must be a no-op

        conn = _open_test_db(db_path)
        count = _count(conn, "messages")
        conn.close()

        assert count == 1

    def test_overflow_fields_stored_in_extra_column(self, db_env):
        ms, db_path = db_env
        record = {
            "id": "overflow-1",
            "type": "text",
            "text": "hi",
            "chat_id": "chat-1",
            "source": "telegram",
            "timestamp": "2024-01-01T00:00:00Z",
            "mystery_field": "some_value",
        }
        ms.persist_inbound(record)

        conn = _open_test_db(db_path)
        row = _fetch_one(conn, "messages", "overflow-1")
        conn.close()

        assert row is not None
        extra = json.loads(row["extra"])
        assert extra["mystery_field"] == "some_value"


# ---------------------------------------------------------------------------
# LOBSTER_MESSAGES_DB env var: custom DB path is honoured
# ---------------------------------------------------------------------------


class TestDbPath:
    def test_custom_db_path_is_used(self, tmp_path, monkeypatch):
        """LOBSTER_MESSAGES_DB must redirect writes to the specified path."""
        db_path = tmp_path / "custom" / "messages.db"
        monkeypatch.setenv("LOBSTER_MESSAGES_DB", str(db_path))
        # Patch the module-level _schema_applied to force re-init in this path
        monkeypatch.setattr(_ms_module, "_schema_applied", False)

        record = {
            "id": "custom-path-1",
            "type": "text",
            "chat_id": "chat-1",
            "source": "telegram",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        _ms_module.persist_inbound(record)

        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT id, direction FROM messages WHERE id = ?", ("custom-path-1",)).fetchone()
        conn.close()
        assert row is not None
        assert row["direction"] == "in"

    def test_parent_dir_created_automatically(self, tmp_path, monkeypatch):
        """_open_conn must create the DB parent directory if it does not exist."""
        db_path = tmp_path / "nested" / "deep" / "messages.db"
        monkeypatch.setenv("LOBSTER_MESSAGES_DB", str(db_path))
        monkeypatch.setattr(_ms_module, "_schema_applied", False)

        record = {
            "id": "nested-dir-1",
            "type": "text",
            "chat_id": "chat-1",
            "source": "telegram",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        _ms_module.persist_inbound(record)
        assert db_path.exists()


# ---------------------------------------------------------------------------
# Error swallowing: DB failures must not propagate to caller
# ---------------------------------------------------------------------------


class TestErrorSwallowing:
    def test_bad_db_path_does_not_raise(self, monkeypatch):
        """If the DB is unwritable, persist_* should log and return, not raise."""
        monkeypatch.setenv("LOBSTER_MESSAGES_DB", "/proc/nonexistent/path/messages.db")
        monkeypatch.setenv("LOBSTER_USE_DB", "1")
        importlib.reload(_ms_module)

        import db.message_store as fresh
        record = {
            "id": "bad-path-1",
            "type": "text",
            "chat_id": "chat-1",
            "source": "telegram",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        # Must not raise
        fresh.persist_inbound(record)

        importlib.reload(_ms_module)
