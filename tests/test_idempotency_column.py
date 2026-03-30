"""
Tests for the idempotency column in agent_sessions (#886).

Acceptance criteria:
- Fresh install: agent_sessions has idempotency column with default 'unknown'
- Existing install after upgrade: column added via migration
- session_start accepts idempotency param without error
- Column is queryable: SELECT idempotency FROM agent_sessions works
- Values are constrained to 'safe' | 'unsafe' | 'unknown'
- Invalid values are silently normalised to 'unknown'
"""

import sqlite3
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from agents import session_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Each test gets a fresh, isolated SQLite DB."""
    db_path = tmp_path / "test_idempotency.db"
    session_store.init_db(db_path)
    yield db_path
    session_store._close_connection(db_path)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestIdempotencyColumnSchema:
    def test_column_exists_on_fresh_install(self, isolated_db):
        """Fresh init_db creates the idempotency column."""
        conn = sqlite3.connect(str(isolated_db))
        cursor = conn.execute("PRAGMA table_info(agent_sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "idempotency" in columns, f"Expected idempotency column, found: {columns}"

    def test_column_default_is_unknown(self, isolated_db):
        """Rows inserted without idempotency default to 'unknown'."""
        session_store.session_start(
            id="test-default-agent",
            description="Test default idempotency",
            chat_id="12345",
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("test-default-agent",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "unknown", f"Expected 'unknown', got {row[0]!r}"

    def test_migration_adds_column_to_existing_db(self, tmp_path):
        """Running init_db on a pre-existing DB without the column adds it."""
        db_path = tmp_path / "old_schema.db"
        # Create a DB with the old schema (without idempotency column).
        # Must include columns referenced by _SCHEMA_INDEXES (status, spawned_at,
        # task_id, notified_at) so init_db's index creation doesn't fail.
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE agent_sessions (
                id TEXT PRIMARY KEY,
                task_id TEXT,
                description TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                spawned_at TEXT NOT NULL,
                notified_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO agent_sessions (id, description, chat_id, spawned_at)
            VALUES ('old-agent', 'legacy row', '999', '2025-01-01T00:00:00+00:00')
        """)
        conn.commit()
        conn.close()

        # init_db should add the column via migration
        session_store.init_db(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(agent_sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("old-agent",),
        ).fetchone()
        conn.close()

        session_store._close_connection(db_path)

        assert "idempotency" in columns, "Migration should add idempotency column"
        assert row is not None
        assert row[0] == "unknown", f"Existing rows should default to 'unknown', got {row[0]!r}"

    def test_column_queryable(self, isolated_db):
        """SELECT idempotency FROM agent_sessions succeeds."""
        session_store.session_start(
            id="queryable-agent",
            description="Test column queryability",
            chat_id="12345",
            idempotency="safe",
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("queryable-agent",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "safe"


# ---------------------------------------------------------------------------
# session_start parameter tests
# ---------------------------------------------------------------------------

class TestSessionStartIdempotency:
    def test_session_start_accepts_safe(self, isolated_db):
        """session_start with idempotency='safe' stores 'safe'."""
        session_store.session_start(
            id="safe-agent",
            description="Read-only task",
            chat_id="12345",
            idempotency="safe",
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("safe-agent",),
        ).fetchone()
        conn.close()
        assert row[0] == "safe"

    def test_session_start_accepts_unsafe(self, isolated_db):
        """session_start with idempotency='unsafe' stores 'unsafe'."""
        session_store.session_start(
            id="unsafe-agent",
            description="Sends messages",
            chat_id="12345",
            idempotency="unsafe",
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("unsafe-agent",),
        ).fetchone()
        conn.close()
        assert row[0] == "unsafe"

    def test_session_start_accepts_unknown(self, isolated_db):
        """session_start with idempotency='unknown' stores 'unknown'."""
        session_store.session_start(
            id="unknown-agent",
            description="Unclassified task",
            chat_id="12345",
            idempotency="unknown",
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("unknown-agent",),
        ).fetchone()
        conn.close()
        assert row[0] == "unknown"

    def test_session_start_none_idempotency_defaults_to_unknown(self, isolated_db):
        """session_start with idempotency=None normalises to 'unknown'."""
        session_store.session_start(
            id="none-agent",
            description="No idempotency set",
            chat_id="12345",
            idempotency=None,
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("none-agent",),
        ).fetchone()
        conn.close()
        assert row[0] == "unknown", f"None should normalise to 'unknown', got {row[0]!r}"

    def test_session_start_invalid_idempotency_normalises_to_unknown(self, isolated_db):
        """session_start with an invalid idempotency value normalises to 'unknown'."""
        session_store.session_start(
            id="invalid-agent",
            description="Invalid classification",
            chat_id="12345",
            idempotency="maybe",  # not a valid value
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("invalid-agent",),
        ).fetchone()
        conn.close()
        assert row[0] == "unknown", f"Invalid value should normalise to 'unknown', got {row[0]!r}"

    def test_session_start_without_idempotency_param_still_works(self, isolated_db):
        """session_start without idempotency param is backward-compatible."""
        # This must not raise — existing callers do not pass idempotency.
        session_store.session_start(
            id="compat-agent",
            description="Backward-compatible call",
            chat_id="12345",
            path=isolated_db,
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT idempotency FROM agent_sessions WHERE id = ?",
            ("compat-agent",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "unknown"


# ---------------------------------------------------------------------------
# Active sessions include idempotency field
# ---------------------------------------------------------------------------

class TestGetActiveSessionsIdempotency:
    def test_get_active_sessions_returns_idempotency(self, isolated_db):
        """get_active_sessions includes the idempotency field."""
        session_store.session_start(
            id="active-safe-agent",
            description="Safe summarisation",
            chat_id="12345",
            idempotency="safe",
            path=isolated_db,
        )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert len(sessions) == 1
        assert sessions[0]["idempotency"] == "safe"

    def test_get_active_sessions_mixed_idempotency(self, isolated_db):
        """Multiple agents with different idempotency values are all queryable."""
        for agent_id, idem in [("s1", "safe"), ("s2", "unsafe"), ("s3", "unknown")]:
            session_store.session_start(
                id=agent_id,
                description=f"Agent {agent_id}",
                chat_id="12345",
                idempotency=idem,
                path=isolated_db,
            )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert len(sessions) == 3
        by_id = {s["id"]: s["idempotency"] for s in sessions}
        assert by_id["s1"] == "safe"
        assert by_id["s2"] == "unsafe"
        assert by_id["s3"] == "unknown"
