"""
Smoke tests for agent session tracking (BIS-51).

Tests:
  - session_store: full lifecycle, task_id matching, history queries
  - tracker adapter: public API unchanged over SQLite backend
  - format_active_sessions_block: compact display helper
"""

import os
import pathlib
import sys
import tempfile
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure src is on path
SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from agents import session_store

# Placeholder string used as a test chat_id value. Several tests pass this as
# a bare name (not a quoted string literal), so it must be defined here.
OWNER_CHAT_ID_PLACEHOLDER = "OWNER_CHAT_ID_PLACEHOLDER"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Each test gets its own fresh SQLite DB and isolated session_store state.

    Closes any cached connection after each test to prevent state leakage.
    """
    db_path = tmp_path / "test_sessions.db"
    session_store.init_db(db_path)
    yield db_path
    session_store._close_connection(db_path)


# ---------------------------------------------------------------------------
# Test: full lifecycle
# ---------------------------------------------------------------------------


def test_full_lifecycle(isolated_db):
    """Start a session, verify active, end it, verify history."""
    db = isolated_db

    # Initially no active sessions
    assert session_store.get_active_sessions(path=db) == []

    # Start a session
    session_store.session_start(
        id="test-001",
        description="Test agent doing work",
        chat_id="OWNER_CHAT_ID_PLACEHOLDER",
        agent_type="general-purpose",
        path=db,
    )

    # Verify it shows as active
    active = session_store.get_active_sessions(path=db)
    assert len(active) == 1
    assert active[0]["id"] == "test-001"
    assert active[0]["status"] == "running"
    assert active[0]["description"] == "Test agent doing work"
    assert active[0]["chat_id"] == "OWNER_CHAT_ID_PLACEHOLDER"
    assert active[0]["agent_type"] == "general-purpose"
    assert "elapsed_seconds" in active[0]
    assert active[0]["elapsed_seconds"] >= 0

    # End it
    session_store.session_end("test-001", "completed", "Task done.", path=db)

    # Verify active is now empty
    assert session_store.get_active_sessions(path=db) == []

    # Verify history includes it
    history = session_store.get_session_history(limit=10, path=db)
    assert len(history) == 1
    assert history[0]["id"] == "test-001"
    assert history[0]["status"] == "completed"
    assert history[0]["result_summary"] == "Task done."
    assert history[0]["completed_at"] is not None


def test_task_id_matching(isolated_db):
    """session_end matches on task_id when id is not provided."""
    db = isolated_db

    session_store.session_start(
        id="test-002",
        description="X",
        chat_id="123",
        task_id="my-task-id",
        path=db,
    )

    # End by task_id (not the agent id)
    session_store.session_end("my-task-id", "failed", path=db)

    # Verify by looking up the session by id
    result = session_store.find_session("test-002", path=db)
    assert result is not None
    assert result["status"] == "failed"
    assert result["task_id"] == "my-task-id"


def test_find_session_by_id(isolated_db):
    db = isolated_db
    session_store.session_start(id="agent-abc", description="Find me", chat_id="999", path=db)
    found = session_store.find_session("agent-abc", path=db)
    assert found is not None
    assert found["id"] == "agent-abc"
    assert found["status"] == "running"


def test_find_session_by_task_id(isolated_db):
    db = isolated_db
    session_store.session_start(
        id="agent-xyz", description="Find by task", chat_id="999",
        task_id="task-abc-123", path=db
    )
    found = session_store.find_session("task-abc-123", path=db)
    assert found is not None
    assert found["id"] == "agent-xyz"


def test_find_session_not_found(isolated_db):
    db = isolated_db
    result = session_store.find_session("nonexistent", path=db)
    assert result is None


def test_session_end_idempotent(isolated_db):
    """Ending a non-existent session is a no-op (no exception)."""
    db = isolated_db
    # Should not raise
    session_store.session_end("does-not-exist", "completed", path=db)


def test_session_end_does_not_double_close(isolated_db):
    """session_end only updates running sessions; completed sessions are unaffected."""
    db = isolated_db
    session_store.session_start(id="test-003", description="Y", chat_id="123", path=db)
    session_store.session_end("test-003", "completed", "First close", path=db)

    # Second close should not change result_summary
    session_store.session_end("test-003", "failed", "Second close", path=db)

    result = session_store.find_session("test-003", path=db)
    assert result["status"] == "completed"
    assert result["result_summary"] == "First close"


def test_session_end_closes_starting_row(isolated_db):
    """session_end closes rows with status='starting' (auto-registered by PostToolUse hook).

    The auto-register-agent.py hook inserts rows as 'starting' before the agent
    has fully initialised. Previously, session_end only matched 'running' rows,
    so write_result calls on 'starting' rows were silently no-ops and the rows
    accumulated indefinitely. This test verifies the fix: session_end now accepts
    both 'running' and 'starting'.
    """
    import sqlite3
    db = isolated_db
    conn = sqlite3.connect(str(db))
    # Insert a 'starting' row directly — mimics the PostToolUse hook
    conn.execute(
        """
        INSERT INTO agent_sessions
            (id, task_id, agent_type, description, chat_id, source, status, spawned_at)
        VALUES
            (?, ?, 'subagent', 'auto-registered by PostToolUse hook', '123', 'telegram',
             'starting', datetime('now'))
        """,
        ("hook-agent-id", "hook-task-id"),
    )
    conn.commit()
    conn.close()

    # End by task_id (as write_result does)
    session_store.session_end("hook-task-id", "completed", "Done.", path=db)

    result = session_store.find_session("hook-task-id", path=db)
    assert result is not None
    assert result["status"] == "completed"
    assert result["completed_at"] is not None
    assert result["result_summary"] == "Done."


def test_multiple_sessions(isolated_db):
    """Multiple concurrent sessions are tracked independently."""
    db = isolated_db
    for i in range(5):
        session_store.session_start(
            id=f"agent-{i}",
            description=f"Agent {i}",
            chat_id="123",
            agent_type="general-purpose",
            path=db,
        )

    active = session_store.get_active_sessions(path=db)
    assert len(active) == 5

    # End two of them
    session_store.session_end("agent-1", "completed", path=db)
    session_store.session_end("agent-3", "failed", path=db)

    active = session_store.get_active_sessions(path=db)
    assert len(active) == 3
    active_ids = {s["id"] for s in active}
    assert "agent-1" not in active_ids
    assert "agent-3" not in active_ids


def test_session_history_limit(isolated_db):
    db = isolated_db
    for i in range(10):
        session_store.session_start(id=f"h-{i}", description="X", chat_id="0", path=db)
        session_store.session_end(f"h-{i}", "completed", path=db)

    history = session_store.get_session_history(limit=5, path=db)
    assert len(history) == 5


def test_session_history_status_filter(isolated_db):
    db = isolated_db
    session_store.session_start(id="ok-1", description="A", chat_id="0", path=db)
    session_store.session_end("ok-1", "completed", path=db)
    session_store.session_start(id="fail-1", description="B", chat_id="0", path=db)
    session_store.session_end("fail-1", "failed", path=db)
    session_store.session_start(id="running-1", description="C", chat_id="0", path=db)

    completed = session_store.get_session_history(status="completed", path=db)
    assert all(s["status"] == "completed" for s in completed)
    assert any(s["id"] == "ok-1" for s in completed)

    failed = session_store.get_session_history(status="failed", path=db)
    assert all(s["status"] == "failed" for s in failed)

    running = session_store.get_session_history(status="running", path=db)
    assert any(s["id"] == "running-1" for s in running)


def test_session_start_replaces_duplicate_id(isolated_db):
    """INSERT OR REPLACE handles duplicate IDs gracefully."""
    db = isolated_db
    session_store.session_start(id="dup", description="First", chat_id="123", path=db)
    session_store.session_start(id="dup", description="Second", chat_id="456", path=db)

    found = session_store.find_session("dup", path=db)
    assert found is not None
    assert found["description"] == "Second"

    active = session_store.get_active_sessions(path=db)
    assert len(active) == 1


def test_optional_fields(isolated_db):
    """Optional fields default to None without error."""
    db = isolated_db
    session_store.session_start(
        id="minimal",
        description="Minimal session",
        chat_id=OWNER_CHAT_ID_PLACEHOLDER,  # int chat_id gets converted to str
        path=db,
    )
    found = session_store.find_session("minimal", path=db)
    assert found is not None
    assert found["agent_type"] is None
    assert found["task_id"] is None
    assert found["output_file"] is None
    assert found["timeout_minutes"] is None
    assert found["chat_id"] == "OWNER_CHAT_ID_PLACEHOLDER"  # stored as TEXT


# ---------------------------------------------------------------------------
# Test: format_active_sessions_block
# ---------------------------------------------------------------------------


def test_format_active_sessions_block_empty():
    result = session_store.format_active_sessions_block([])
    assert result == ""


def test_format_active_sessions_block_single():
    sessions = [{
        "id": "abc",
        "agent_type": "functional-engineer",
        "description": "Implement feature X",
        "chat_id": "OWNER_CHAT_ID_PLACEHOLDER",
        "elapsed_seconds": 720,
        "status": "running",
    }]
    result = session_store.format_active_sessions_block(sessions)
    assert "[1 agent running]" in result
    assert "functional-engineer" in result
    assert "Implement feature X" in result
    assert "12m ago" in result


def test_format_active_sessions_block_multiple():
    sessions = [
        {"agent_type": "functional-engineer", "description": "Work A",
         "chat_id": "123", "elapsed_seconds": 720, "id": "1"},
        {"agent_type": "general-purpose", "description": "Work B",
         "chat_id": "123", "elapsed_seconds": 120, "id": "2"},
    ]
    result = session_store.format_active_sessions_block(sessions)
    assert "[2 agents running]" in result
    assert "functional-engineer" in result
    assert "general-purpose" in result


def test_format_truncates_long_description():
    sessions = [{
        "agent_type": "agent",
        "description": "A" * 100,
        "chat_id": "0",
        "elapsed_seconds": 60,
        "id": "x",
    }]
    result = session_store.format_active_sessions_block(sessions)
    # Description should be truncated
    assert "..." in result


# ---------------------------------------------------------------------------
# Test: tracker.py adapter compatibility
# ---------------------------------------------------------------------------


def test_tracker_adapter_compat(isolated_db):
    """tracker.py public API must work unchanged over SQLite backend.

    Note: tracker.py uses the module-level default DB path, not the test path.
    We prime the session_store with an init_db call using the test path,
    then test the tracker functions against that same DB.
    """
    # Import tracker after session_store is initialized
    from agents.tracker import (
        add_pending_agent,
        remove_pending_agent,
        get_pending_agents,
        is_agent_pending,
        pending_agent_count,
    )

    db = isolated_db

    # Test add
    add_pending_agent("a1", "Do thing", 123456, path=db)
    assert is_agent_pending("a1", path=db)
    assert pending_agent_count(path=db) == 1

    # Test multiple
    add_pending_agent("a2", "Other thing", 789012, task_id="task-xyz", path=db)
    assert pending_agent_count(path=db) == 2

    # Test list
    agents = get_pending_agents(path=db)
    assert len(agents) == 2
    ids = {a["id"] for a in agents}
    assert "a1" in ids
    assert "a2" in ids

    # Test remove
    remove_pending_agent("a1", path=db)
    assert not is_agent_pending("a1", path=db)
    assert pending_agent_count(path=db) == 1

    # Remove remaining
    remove_pending_agent("a2", path=db)
    assert get_pending_agents(path=db) == []


def test_tracker_remove_nonexistent_is_noop(isolated_db):
    """Removing a non-existent agent is idempotent (no exception)."""
    from agents.tracker import remove_pending_agent, is_agent_pending

    db = isolated_db
    remove_pending_agent("no-such-agent", path=db)
    assert not is_agent_pending("no-such-agent", path=db)


def test_tracker_add_with_all_params(isolated_db):
    """add_pending_agent supports all optional params without error."""
    from agents.tracker import add_pending_agent, get_pending_agents

    db = isolated_db
    add_pending_agent(
        agent_id="full-agent",
        description="Full params test",
        chat_id=OWNER_CHAT_ID_PLACEHOLDER,
        task_id="task-full-001",
        source="telegram",
        output_file="/tmp/claude-1000/tasks/full-agent.output",
        timeout_minutes=30,
        path=db,
    )
    agents = get_pending_agents(path=db)
    assert len(agents) == 1
    a = agents[0]
    assert a["id"] == "full-agent"
    assert a["task_id"] == "task-full-001"
    assert a["output_file"] == "/tmp/claude-1000/tasks/full-agent.output"
    assert a["timeout_minutes"] == 30


# ---------------------------------------------------------------------------
# Test: JSON migration
# ---------------------------------------------------------------------------


def test_json_migration(tmp_path):
    """pending-agents.json is migrated to SQLite on init_db()."""
    import json

    db_path = tmp_path / "sessions.db"
    json_path = tmp_path / "pending-agents.json"

    # Write a pending-agents.json in the same directory as the DB
    agents_data = {
        "agents": [
            {
                "id": "migrated-001",
                "description": "Migrated agent",
                "chat_id": OWNER_CHAT_ID_PLACEHOLDER,
                "source": "telegram",
                "started_at": "2026-03-15T10:00:00+00:00",
                "status": "running",
            }
        ]
    }
    json_path.write_text(json.dumps(agents_data))

    # init_db should migrate the JSON
    session_store.init_db(db_path)

    # Verify migration
    active = session_store.get_active_sessions(path=db_path)
    assert len(active) == 1
    assert active[0]["id"] == "migrated-001"
    assert active[0]["description"] == "Migrated agent"

    # JSON file should be renamed to .migrated
    migrated_marker = tmp_path / "pending-agents.json.migrated"
    assert migrated_marker.exists()
    assert not json_path.exists()

    # Cleanup
    session_store._close_connection(db_path)


def test_json_migration_idempotent(tmp_path):
    """Migration is not re-run if .migrated marker exists."""
    import json

    db_path = tmp_path / "sessions.db"
    json_path = tmp_path / "pending-agents.json"
    migrated_marker = tmp_path / "pending-agents.json.migrated"

    # Pre-create the migrated marker (simulates already-migrated system)
    migrated_marker.write_text("{}")

    # Write a fresh JSON — should be ignored because .migrated exists
    agents_data = {"agents": [{"id": "should-not-migrate", "description": "X",
                                "chat_id": "0", "started_at": "2026-03-15T10:00:00+00:00"}]}
    json_path.write_text(json.dumps(agents_data))

    session_store.init_db(db_path)

    # No migration should have happened
    active = session_store.get_active_sessions(path=db_path)
    assert len(active) == 0

    # Original JSON file should still exist (not renamed again)
    assert json_path.exists()

    session_store._close_connection(db_path)


def test_json_migration_missing_json_is_noop(tmp_path):
    """init_db with no pending-agents.json is a no-op (no error)."""
    db_path = tmp_path / "sessions.db"
    session_store.init_db(db_path)  # No JSON file — should succeed
    active = session_store.get_active_sessions(path=db_path)
    assert active == []
    session_store._close_connection(db_path)


# ---------------------------------------------------------------------------
# Test: cleanup_stale_running_sessions (issue #510)
# ---------------------------------------------------------------------------


def test_cleanup_stale_running_no_output_file(isolated_db):
    """Session with no output_file and elapsed > timeout_minutes is marked dead."""
    db = isolated_db

    # Spawn a session with a timeout of 1 minute, spawned_at 120 minutes ago
    old_spawned_at = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
    session_store._get_connection(db).execute(
        """
        INSERT INTO agent_sessions
            (id, description, chat_id, source, status, spawned_at, timeout_minutes)
        VALUES ('stale-no-file', 'Old agent', '123', 'telegram', 'running', ?, 60)
        """,
        (old_spawned_at,),
    )
    session_store._get_connection(db).commit()

    server_start = datetime.now(timezone.utc)
    dead = session_store.cleanup_stale_running_sessions(server_start, path=db)

    assert "stale-no-file" in dead
    result = session_store.find_session("stale-no-file", path=db)
    assert result["status"] == "dead"


def test_cleanup_stale_running_output_missing(isolated_db, tmp_path):
    """Session whose output_file does not exist on disk is marked dead."""
    db = isolated_db
    missing_path = str(tmp_path / "nonexistent.output")

    session_store.session_start(
        id="stale-missing-file",
        description="Agent with missing output",
        chat_id="123",
        output_file=missing_path,
        path=db,
    )

    server_start = datetime.now(timezone.utc)
    dead = session_store.cleanup_stale_running_sessions(server_start, path=db)

    assert "stale-missing-file" in dead
    result = session_store.find_session("stale-missing-file", path=db)
    assert result["status"] == "dead"
    assert "missing" in result["result_summary"]


def test_cleanup_stale_running_output_old_mtime(isolated_db, tmp_path):
    """Session whose output_file mtime predates server_start is marked dead."""
    db = isolated_db
    output_file = tmp_path / "old_agent.output"
    output_file.write_text('{"stop_reason": "tool_use"}')

    # Set mtime to 10 minutes ago
    old_ts = _time.time() - 600
    os.utime(str(output_file), (old_ts, old_ts))

    session_store.session_start(
        id="stale-old-mtime",
        description="Agent with old mtime",
        chat_id="123",
        output_file=str(output_file),
        path=db,
    )

    # Server started 5 minutes ago — still newer than the file
    server_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    dead = session_store.cleanup_stale_running_sessions(server_start, path=db)

    assert "stale-old-mtime" in dead
    result = session_store.find_session("stale-old-mtime", path=db)
    assert result["status"] == "dead"
    assert "mtime" in result["result_summary"]


def test_cleanup_stale_running_skips_fresh_file(isolated_db, tmp_path):
    """Session whose output_file mtime is newer than server_start is left running."""
    db = isolated_db
    output_file = tmp_path / "fresh_agent.output"
    output_file.write_text('{"stop_reason": "tool_use"}')
    # File was just written — mtime is now, which is after server_start

    # Server started 10 minutes ago
    server_start = datetime.now(timezone.utc) - timedelta(minutes=10)

    session_store.session_start(
        id="fresh-agent",
        description="Fresh running agent",
        chat_id="123",
        output_file=str(output_file),
        path=db,
    )

    dead = session_store.cleanup_stale_running_sessions(server_start, path=db)

    assert "fresh-agent" not in dead
    result = session_store.find_session("fresh-agent", path=db)
    assert result["status"] == "running"


def test_cleanup_stale_running_no_op_when_empty(isolated_db):
    """cleanup_stale_running_sessions returns empty list when no running sessions."""
    db = isolated_db
    server_start = datetime.now(timezone.utc)
    dead = session_store.cleanup_stale_running_sessions(server_start, path=db)
    assert dead == []


def test_cleanup_stale_running_skips_no_file_within_timeout(isolated_db):
    """Session with no output_file but spawned recently (within timeout) is skipped."""
    db = isolated_db

    # Spawned 30 minutes ago, timeout=120 minutes — not yet over threshold
    recent_spawned_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    session_store._get_connection(db).execute(
        """
        INSERT INTO agent_sessions
            (id, description, chat_id, source, status, spawned_at, timeout_minutes)
        VALUES ('recent-no-file', 'Recent agent', '123', 'telegram', 'running', ?, 120)
        """,
        (recent_spawned_at,),
    )
    session_store._get_connection(db).commit()

    server_start = datetime.now(timezone.utc)
    dead = session_store.cleanup_stale_running_sessions(server_start, path=db)

    assert "recent-no-file" not in dead
    result = session_store.find_session("recent-no-file", path=db)
    assert result["status"] == "running"


def test_cleanup_stale_running_oserror_marks_dead(isolated_db, tmp_path):
    """Session whose output_file raises OSError is marked dead (unreadable path)."""
    db = isolated_db
    # Use a path inside a non-existent directory to provoke OSError on resolve/stat.
    # Path.resolve() on a dangling path inside a missing parent dir can raise OSError
    # on some filesystems; we simulate by patching Path.resolve to raise.
    import unittest.mock as mock

    output_path = str(tmp_path / "unreadable.output")

    session_store.session_start(
        id="oserror-agent",
        description="Agent with unreadable output",
        chat_id="123",
        output_file=output_path,
        path=db,
    )

    # Patch Path.resolve to raise OSError for this specific path
    original_resolve = Path.resolve

    def patched_resolve(self, **kwargs):
        if str(self) == output_path:
            raise OSError("Permission denied (simulated)")
        return original_resolve(self, **kwargs)

    with mock.patch.object(Path, "resolve", patched_resolve):
        server_start = datetime.now(timezone.utc)
        dead = session_store.cleanup_stale_running_sessions(server_start, path=db)

    assert "oserror-agent" in dead
    result = session_store.find_session("oserror-agent", path=db)
    assert result["status"] == "dead"
    assert "unreadable" in result["result_summary"]


def test_cleanup_stale_running_null_spawned_at_no_output_file_skips_with_warning(
    isolated_db, caplog
):
    """Session with no output_file AND no spawned_at stays running and logs a warning.

    The current schema has spawned_at NOT NULL, so this combination cannot arise
    through normal inserts. The warning branch is defensive code for future schema
    changes or direct DB manipulation. We test it by patching the DB cursor to
    return a synthetic row with both fields set to None.
    """
    import logging
    import unittest.mock as mock

    db = isolated_db
    server_start = datetime.now(timezone.utc)

    # Build a fake row dict that looks like what the cursor would return
    fake_row = {
        "id": "null-everything",
        "output_file": None,
        "spawned_at": None,
        "timeout_minutes": None,
    }

    # Patch _get_connection to return a mock whose .execute().fetchall() returns our row
    mock_cursor = mock.MagicMock()
    mock_cursor.fetchall.return_value = [fake_row]
    mock_conn = mock.MagicMock()
    mock_conn.execute.return_value = mock_cursor

    with mock.patch.object(session_store, "_get_connection", return_value=mock_conn):
        with caplog.at_level(logging.WARNING, logger="agents.session_store"):
            dead = session_store.cleanup_stale_running_sessions(server_start, path=db)

    # Row has no actionable info — should not be marked dead
    assert "null-everything" not in dead

    # Should have logged a warning about the uncleanable row
    assert any("null-everything" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_reconciler_check_output_file_status_running_for_stuck_tool_use(tmp_path):
    """check_output_file_status returns 'running' for a file with stop_reason=tool_use.

    This exercises the precondition for the reconciler's 60-min dead-threshold
    branch: a file stuck at tool_use is treated as 'running', not 'missing' or
    'done', so the normal 25-min threshold does not fire and only the 60-min
    threshold applies.
    """
    output_file = tmp_path / "stuck_agent.output"
    # JSONL with last stop_reason = tool_use (simulating mid-turn stuck state)
    output_file.write_text(
        '{"type": "result", "stop_reason": "tool_use", "subtype": "tool_use"}\n'
    )

    status = session_store.check_output_file_status(str(output_file))
    assert status == "running", (
        f"Expected 'running' for tool_use output file, got {status!r}"
    )


def test_reconciler_check_output_file_status_done_for_end_turn(tmp_path):
    """check_output_file_status returns 'done' for a file with stop_reason=end_turn."""
    output_file = tmp_path / "finished_agent.output"
    output_file.write_text(
        '{"type": "result", "stop_reason": "end_turn", "subtype": "end_turn"}\n'
    )

    status = session_store.check_output_file_status(str(output_file))
    assert status == "done", (
        f"Expected 'done' for end_turn output file, got {status!r}"
    )
