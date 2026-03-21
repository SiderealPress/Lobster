"""
tests/test_messages_db.py — Unit tests for the messages.db schema and migration.

Tests cover:
  - Schema creation (all tables, indexes, FTS5 virtual tables)
  - Row classification (classify function)
  - Row builders (build_message_row, build_bisque_event_row, build_agent_event_row)
  - FTS5 full-text search on each table
  - FTS5 triggers (insert / delete / update)
  - Idempotent migration (INSERT OR IGNORE)
  - Malformed JSON recovery (strict=False fallback)
  - End-to-end migrate_directory with in-memory fixtures
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the modules under test
# ---------------------------------------------------------------------------

import sys

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.db.connection import get_connection, apply_schema, open_messages_db
from scripts.migrate_json_to_db import (
    classify,
    build_message_row,
    build_bisque_event_row,
    build_agent_event_row,
    iter_json_files,
    migrate_directory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn():
    """In-memory SQLite connection with schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def tmp_dir(tmp_path):
    """Return a temporary directory for fake message JSON files."""
    return tmp_path


def write_json(directory: Path, name: str, data: dict) -> Path:
    """Write *data* as JSON to *directory/name* and return the path."""
    p = directory / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchema:
    def test_tables_exist(self, db_conn):
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "messages" in names
        assert "bisque_events" in names
        assert "agent_events" in names
        assert "schema_migrations" in names

    def test_fts5_virtual_tables_exist(self, db_conn):
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "messages_fts" in names
        assert "bisque_events_fts" in names
        assert "agent_events_fts" in names

    def test_indexes_exist(self, db_conn):
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_messages_timestamp" in names
        assert "idx_bisque_events_timestamp" in names
        assert "idx_agent_events_timestamp" in names
        assert "idx_agent_events_task_id" in names
        assert "idx_messages_chat_id" in names

    def test_migration_version_recorded(self, db_conn):
        row = db_conn.execute(
            "SELECT version, description FROM schema_migrations WHERE version = '001'"
        ).fetchone()
        assert row is not None
        assert "messages" in row["description"].lower()

    def test_schema_is_idempotent(self, db_conn):
        """Applying the schema a second time must not raise."""
        apply_schema(db_conn)  # already applied by fixture — apply again
        row = db_conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()
        assert row["n"] == 1  # INSERT OR IGNORE keeps exactly one row

    def test_open_messages_db_creates_file(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = open_messages_db(db_path)
        conn.close()
        assert db_path.exists()

    def test_open_messages_db_has_wal_mode(self, tmp_path):
        db_path = tmp_path / "test_wal.db"
        conn = open_messages_db(db_path)
        row = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert row[0] == "wal"


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize(
        "msg_type,source,direction,expected_table",
        [
            # Agent event types always go to agent_events
            ("subagent_result", "telegram", "in", "agent_events"),
            ("subagent_notification", "telegram", "in", "agent_events"),
            ("subagent_error", "telegram", "in", "agent_events"),
            ("agent_failed", "system", "in", "agent_events"),
            ("task-notification", "internal", "in", "agent_events"),
            # Bisque source → bisque_events (unless it is also an agent type)
            ("text", "bisque", "in", "bisque_events"),
            ("voice", "bisque", "in", "bisque_events"),
            (None, "bisque", "in", "bisque_events"),
            # Everything else → messages
            ("telegram", "telegram", "in", "messages"),
            ("self_check", "system", "in", "messages"),
            (None, "telegram", "out", "messages"),
            (None, "telegram", "in", "messages"),
        ],
    )
    def test_classify(self, msg_type, source, direction, expected_table):
        record = {"source": source}
        if msg_type is not None:
            record["type"] = msg_type
        assert classify(record, direction) == expected_table


# ---------------------------------------------------------------------------
# Row builder tests
# ---------------------------------------------------------------------------


class TestBuildMessageRow:
    def _minimal(self, direction="in") -> dict:
        return {
            "id": "1769219731361_22",
            "source": "telegram",
            "chat_id": ADMIN_CHAT_ID_REDACTED,
            "text": "Hello",
            "timestamp": "2026-01-24T01:55:31.361389",
        }

    def test_basic_fields(self):
        record = self._minimal()
        row = build_message_row(record, "in")
        assert row["id"] == "1769219731361_22"
        assert row["direction"] == "in"
        assert row["source"] == "telegram"
        assert row["text"] == "Hello"
        assert row["timestamp"] == "2026-01-24T01:55:31.361389"

    def test_chat_id_coerced_to_str(self):
        record = self._minimal()
        record["chat_id"] = ADMIN_CHAT_ID_REDACTED  # int in JSON
        row = build_message_row(record, "in")
        assert row["chat_id"] == "ADMIN_CHAT_ID_REDACTED"

    def test_overflow_fields_go_to_extra(self):
        record = self._minimal()
        record["unknown_field_xyz"] = "abc"
        row = build_message_row(record, "in")
        assert row["extra"] is not None
        extra = json.loads(row["extra"])
        assert extra["unknown_field_xyz"] == "abc"

    def test_no_overflow_leaves_extra_null(self):
        record = self._minimal()
        row = build_message_row(record, "in")
        assert row["extra"] is None

    def test_int_fields_coerced(self):
        record = self._minimal()
        record["image_width"] = "1920"
        record["image_height"] = "1080"
        row = build_message_row(record, "in")
        assert row["image_width"] == 1920
        assert row["image_height"] == 1080

    def test_outbound_direction(self):
        record = self._minimal()
        row = build_message_row(record, "out")
        assert row["direction"] == "out"


class TestBuildBisqueEventRow:
    def test_basic_fields(self):
        record = {
            "id": "bisque_123",
            "source": "bisque",
            "chat_id": "drew@lobster.ai",
            "type": "text",
            "text": "Hello from bisque",
            "timestamp": "2026-03-19T09:33:49.129780+00:00",
        }
        row = build_bisque_event_row(record)
        assert row["id"] == "bisque_123"
        assert row["chat_id"] == "drew@lobster.ai"
        assert row["type"] == "text"
        assert row["text"] == "Hello from bisque"

    def test_sent_reply_to_user_bool_int(self):
        record = {
            "id": "bisque_999",
            "timestamp": "2026-01-01T00:00:00",
            "sent_reply_to_user": True,
        }
        row = build_bisque_event_row(record)
        assert row["sent_reply_to_user"] == 1

    def test_attachments_serialised_as_json(self):
        record = {
            "id": "bisque_attach",
            "timestamp": "2026-01-01T00:00:00",
            "attachments": [{"file": "a.txt"}, {"file": "b.txt"}],
        }
        row = build_bisque_event_row(record)
        assert row["attachments"] is not None
        parsed = json.loads(row["attachments"])
        assert len(parsed) == 2


class TestBuildAgentEventRow:
    def test_basic_fields(self):
        record = {
            "id": "1773682753027_reconciler_abc",
            "type": "subagent_result",
            "source": "telegram",
            "chat_id": "ADMIN_CHAT_ID_REDACTED",
            "task_id": "abc123",
            "status": "success",
            "text": "Done",
            "timestamp": "2026-03-16T17:39:13.027852+00:00",
        }
        row = build_agent_event_row(record)
        assert row["id"] == "1773682753027_reconciler_abc"
        assert row["type"] == "subagent_result"
        assert row["status"] == "success"
        assert row["task_id"] == "abc123"

    def test_artifacts_serialised_as_json(self):
        record = {
            "id": "evt_1",
            "type": "subagent_notification",
            "timestamp": "2026-01-01T00:00:00",
            "artifacts": ["/tmp/report.md"],
        }
        row = build_agent_event_row(record)
        assert row["artifacts"] is not None
        assert json.loads(row["artifacts"]) == ["/tmp/report.md"]

    def test_sent_reply_false_stored_as_zero(self):
        record = {
            "id": "evt_2",
            "type": "agent_failed",
            "timestamp": "2026-01-01T00:00:00",
            "sent_reply_to_user": False,
        }
        row = build_agent_event_row(record)
        assert row["sent_reply_to_user"] == 0


# ---------------------------------------------------------------------------
# iter_json_files tests
# ---------------------------------------------------------------------------


class TestIterJsonFiles:
    def test_yields_valid_files(self, tmp_dir):
        write_json(tmp_dir, "msg1.json", {"id": "1", "text": "hello"})
        write_json(tmp_dir, "msg2.json", {"id": "2", "text": "world"})
        results = list(iter_json_files(tmp_dir))
        assert len(results) == 2
        ids = {r["id"] for _, r in results}
        assert ids == {"1", "2"}

    def test_skips_invalid_json(self, tmp_dir):
        (tmp_dir / "bad.json").write_text("{{not valid json}}")
        write_json(tmp_dir, "good.json", {"id": "ok"})
        results = list(iter_json_files(tmp_dir))
        assert len(results) == 1
        assert results[0][1]["id"] == "ok"

    def test_handles_control_chars_via_non_strict(self, tmp_dir):
        # Simulate a JSON file with an embedded control character (e.g. \x0b)
        # inside a string value — which strict json.loads rejects.
        raw = b'{"id": "ctrl", "text": "hello\x0bworld"}'
        (tmp_dir / "ctrl.json").write_bytes(raw)
        results = list(iter_json_files(tmp_dir))
        assert len(results) == 1
        assert results[0][1]["id"] == "ctrl"

    def test_empty_directory_yields_nothing(self, tmp_dir):
        results = list(iter_json_files(tmp_dir))
        assert results == []

    def test_ignores_non_json_files(self, tmp_dir):
        (tmp_dir / "readme.txt").write_text("not json")
        write_json(tmp_dir, "real.json", {"id": "real"})
        results = list(iter_json_files(tmp_dir))
        assert len(results) == 1


# ---------------------------------------------------------------------------
# migrate_directory integration tests
# ---------------------------------------------------------------------------


class TestMigrateDirectory:
    def _setup_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_schema(conn)
        return conn

    def test_migrate_plain_messages(self, tmp_dir):
        write_json(
            tmp_dir,
            "msg.json",
            {
                "id": "msg001",
                "source": "telegram",
                "chat_id": "ADMIN_CHAT_ID_REDACTED",
                "text": "hi there",
                "timestamp": "2026-01-01T00:00:00",
            },
        )
        conn = self._setup_db()
        counts = migrate_directory(conn, tmp_dir, "in")
        assert counts["messages"] == 1
        assert counts["agent_events"] == 0
        assert counts["errors"] == 0
        row = conn.execute("SELECT * FROM messages WHERE id = 'msg001'").fetchone()
        assert row is not None
        assert row["text"] == "hi there"
        assert row["direction"] == "in"

    def test_migrate_agent_events(self, tmp_dir):
        write_json(
            tmp_dir,
            "agent.json",
            {
                "id": "agent001",
                "type": "subagent_result",
                "source": "telegram",
                "chat_id": "ADMIN_CHAT_ID_REDACTED",
                "task_id": "t001",
                "status": "success",
                "text": "Task done",
                "timestamp": "2026-01-01T00:00:01",
            },
        )
        conn = self._setup_db()
        counts = migrate_directory(conn, tmp_dir, "in")
        assert counts["agent_events"] == 1
        assert counts["messages"] == 0
        row = conn.execute("SELECT * FROM agent_events WHERE id = 'agent001'").fetchone()
        assert row is not None
        assert row["status"] == "success"

    def test_migrate_bisque_events(self, tmp_dir):
        write_json(
            tmp_dir,
            "bisque.json",
            {
                "id": "bisque001",
                "source": "bisque",
                "chat_id": "drew@lobster.ai",
                "type": "text",
                "text": "Bisque message",
                "timestamp": "2026-01-01T00:00:02",
            },
        )
        conn = self._setup_db()
        counts = migrate_directory(conn, tmp_dir, "in")
        assert counts["bisque_events"] == 1
        assert counts["messages"] == 0
        row = conn.execute(
            "SELECT * FROM bisque_events WHERE id = 'bisque001'"
        ).fetchone()
        assert row is not None
        assert row["chat_id"] == "drew@lobster.ai"

    def test_idempotent_migration(self, tmp_dir):
        write_json(
            tmp_dir,
            "dup.json",
            {
                "id": "dup001",
                "source": "telegram",
                "chat_id": "ADMIN_CHAT_ID_REDACTED",
                "text": "duplicate",
                "timestamp": "2026-01-01T00:00:00",
            },
        )
        conn = self._setup_db()
        migrate_directory(conn, tmp_dir, "in")
        migrate_directory(conn, tmp_dir, "in")  # second run — should be no-op
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE id = 'dup001'"
        ).fetchone()["n"]
        assert count == 1

    def test_dry_run_does_not_insert(self, tmp_dir):
        write_json(
            tmp_dir,
            "msg.json",
            {
                "id": "dry001",
                "source": "telegram",
                "chat_id": "1234",
                "text": "dry run",
                "timestamp": "2026-01-01T00:00:00",
            },
        )
        conn = self._setup_db()
        counts = migrate_directory(conn, tmp_dir, "in", dry_run=True)
        assert counts["messages"] == 1
        count = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        assert count == 0  # nothing written

    def test_outbound_direction_stored(self, tmp_dir):
        write_json(
            tmp_dir,
            "sent.json",
            {
                "id": "sent001",
                "source": "telegram",
                "chat_id": "ADMIN_CHAT_ID_REDACTED",
                "text": "reply text",
                "timestamp": "2026-01-01T00:00:00",
            },
        )
        conn = self._setup_db()
        migrate_directory(conn, tmp_dir, "out")
        row = conn.execute("SELECT direction FROM messages WHERE id = 'sent001'").fetchone()
        assert row["direction"] == "out"


# ---------------------------------------------------------------------------
# FTS5 search tests
# ---------------------------------------------------------------------------


class TestFTS5Search:
    def _insert_message(self, conn, msg_id, text, source="telegram"):
        conn.execute(
            """
            INSERT INTO messages (id, direction, source, text, timestamp)
            VALUES (?, 'in', ?, ?, '2026-01-01T00:00:00')
            """,
            (msg_id, source, text),
        )
        conn.commit()

    def _insert_agent_event(self, conn, event_id, text, event_type="subagent_result"):
        conn.execute(
            """
            INSERT INTO agent_events (id, type, source, chat_id, text, timestamp)
            VALUES (?, ?, 'telegram', '123', ?, '2026-01-01T00:00:00')
            """,
            (event_id, event_type, text),
        )
        conn.commit()

    def test_messages_fts_basic_search(self, db_conn):
        self._insert_message(db_conn, "m1", "The quick brown fox jumps")
        self._insert_message(db_conn, "m2", "The lazy dog sleeps")
        rows = db_conn.execute(
            """
            SELECT m.id FROM messages m
            JOIN messages_fts ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH 'fox'
            """
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "m1"

    def test_messages_fts_no_match(self, db_conn):
        self._insert_message(db_conn, "m1", "Hello world")
        rows = db_conn.execute(
            """
            SELECT m.id FROM messages m
            JOIN messages_fts ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH 'nonexistent'
            """
        ).fetchall()
        assert rows == []

    def test_agent_events_fts_search(self, db_conn):
        self._insert_agent_event(db_conn, "a1", "GitHub issue resolved successfully")
        self._insert_agent_event(db_conn, "a2", "Telegram message delivered")
        rows = db_conn.execute(
            """
            SELECT a.id FROM agent_events a
            JOIN agent_events_fts ON a.rowid = agent_events_fts.rowid
            WHERE agent_events_fts MATCH 'github'
            """
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "a1"

    def test_fts_trigger_on_delete(self, db_conn):
        """After deleting a row, FTS should no longer find it."""
        self._insert_message(db_conn, "del1", "deletable content xyz")
        # Confirm it's findable
        rows = db_conn.execute(
            """
            SELECT m.id FROM messages m
            JOIN messages_fts ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH 'deletable'
            """
        ).fetchall()
        assert len(rows) == 1
        # Delete it
        db_conn.execute("DELETE FROM messages WHERE id = 'del1'")
        db_conn.commit()
        # Should be gone from FTS
        rows = db_conn.execute(
            """
            SELECT m.id FROM messages m
            JOIN messages_fts ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH 'deletable'
            """
        ).fetchall()
        assert rows == []

    def test_fts_trigger_on_update(self, db_conn):
        """After updating a row, FTS should reflect new content."""
        self._insert_message(db_conn, "upd1", "original text")
        db_conn.execute(
            "UPDATE messages SET text = 'updated content' WHERE id = 'upd1'"
        )
        db_conn.commit()
        # Old text should not match
        old_rows = db_conn.execute(
            """
            SELECT m.id FROM messages m
            JOIN messages_fts ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH 'original'
            """
        ).fetchall()
        assert old_rows == []
        # New text should match
        new_rows = db_conn.execute(
            """
            SELECT m.id FROM messages m
            JOIN messages_fts ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH 'updated'
            """
        ).fetchall()
        assert len(new_rows) == 1
