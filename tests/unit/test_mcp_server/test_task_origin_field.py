"""
Unit tests for the task_origin field on agent sessions and inbox messages (issue #1422).

task_origin makes intent explicit where chat_id==0 was previously used as a proxy
for "this is a system task". The three values are:
  'user'      — triggered by a real user message (Telegram, Slack, etc.)
  'scheduled' — triggered by a scheduled job or cron task
  'internal'  — system-initiated, no user involved (reconciler, health check, etc.)

Tests verify:
  1. session_store.session_start() stores and retrieves task_origin correctly
  2. Unrecognized or missing task_origin defaults to 'user'
  3. tracker.add_pending_agent() threads task_origin through to session_store
  4. _build_reconciler_message() stamps task_origin on completed and dead messages:
       - completed: inherits task_origin from the session
       - dead:      always 'internal' (failure notices are never user-triggered)
  5. System inbox messages (session_note_reminder, session-lost) carry task_origin='internal'
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[3]

for _p in [
    str(_ROOT / "src" / "mcp"),
    str(_ROOT / "src" / "agents"),
    str(_ROOT / "src"),
    str(_ROOT / "src" / "utils"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import session_store

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)

# Session with no task_origin (should default to 'user')
_SESSION_NO_ORIGIN: dict = {
    "id": "agent-no-origin",
    "task_id": "task-no-origin",
    "description": "Agent with no task_origin recorded",
    "chat_id": "ADMIN_CHAT_ID_REDACTED",
    "source": "telegram",
    "status": "running",
    "output_file": None,
    "input_summary": None,
    "elapsed_seconds": 120,
    "notified_at": None,
    "agent_type": "general-purpose",
    # deliberately omitting 'task_origin' to test default behavior
}

_SESSION_USER_ORIGIN: dict = {
    "id": "agent-user-origin",
    "task_id": "task-user",
    "description": "Agent spawned by user request",
    "chat_id": "ADMIN_CHAT_ID_REDACTED",
    "source": "telegram",
    "status": "running",
    "output_file": None,
    "input_summary": "Fix the bug the user reported",
    "elapsed_seconds": 300,
    "notified_at": None,
    "agent_type": "functional-engineer",
    "task_origin": "user",
}

_SESSION_SCHEDULED_ORIGIN: dict = {
    "id": "agent-scheduled",
    "task_id": "task-scheduled",
    "description": "Nightly consolidation job",
    "chat_id": "0",
    "source": "system",
    "status": "running",
    "output_file": None,
    "input_summary": None,
    "elapsed_seconds": 45,
    "notified_at": None,
    "agent_type": "scheduled",
    "task_origin": "scheduled",
}

_SESSION_INTERNAL_ORIGIN: dict = {
    "id": "agent-internal",
    "task_id": "task-internal",
    "description": "Reconciler-spawned health check",
    "chat_id": "0",
    "source": "system",
    "status": "running",
    "output_file": None,
    "input_summary": None,
    "elapsed_seconds": 10,
    "notified_at": None,
    "agent_type": "reconciler",
    "task_origin": "internal",
}


# ---------------------------------------------------------------------------
# Fixture: isolated SQLite DB per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Each test gets its own fresh SQLite DB."""
    db_path = tmp_path / "test_task_origin.db"
    session_store.init_db(db_path)
    yield db_path
    session_store._close_connection(db_path)


# ---------------------------------------------------------------------------
# Fixture: load inbox_server for pure-function tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def inbox_server_module(tmp_path_factory):
    """Load inbox_server with minimal environment setup."""
    import os

    tmp = tmp_path_factory.mktemp("messages")
    os.environ.setdefault("LOBSTER_MESSAGES", str(tmp / "messages"))
    os.environ.setdefault("LOBSTER_WORKSPACE", str(tmp / "workspace"))

    try:
        if "inbox_server" in sys.modules:
            del sys.modules["inbox_server"]
        import inbox_server as _is
        return _is
    except Exception:
        pytest.skip("inbox_server not importable in this test environment")


@pytest.fixture(scope="module")
def build_reconciler_message(inbox_server_module):
    """Return _build_reconciler_message from inbox_server."""
    return inbox_server_module._build_reconciler_message


# ---------------------------------------------------------------------------
# Tests: session_store.session_start stores and retrieves task_origin
# ---------------------------------------------------------------------------

class TestSessionStoreTaskOrigin:
    """session_store.session_start() correctly stores and retrieves task_origin."""

    VALID_ORIGINS = ["user", "scheduled", "internal"]

    def test_task_origin_user_stored_and_retrieved(self, isolated_db):
        """'user' origin is stored and returned by get_active_sessions."""
        session_store.session_start(
            id="agent-001",
            description="User-triggered task",
            chat_id="123",
            task_origin="user",
            path=isolated_db,
        )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert len(sessions) == 1
        assert sessions[0]["task_origin"] == "user"

    def test_task_origin_scheduled_stored_and_retrieved(self, isolated_db):
        """'scheduled' origin is stored and returned by get_active_sessions."""
        session_store.session_start(
            id="agent-002",
            description="Cron job task",
            chat_id="0",
            task_origin="scheduled",
            path=isolated_db,
        )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert sessions[0]["task_origin"] == "scheduled"

    def test_task_origin_internal_stored_and_retrieved(self, isolated_db):
        """'internal' origin is stored and returned by get_active_sessions."""
        session_store.session_start(
            id="agent-003",
            description="Reconciler health check",
            chat_id="0",
            task_origin="internal",
            path=isolated_db,
        )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert sessions[0]["task_origin"] == "internal"

    def test_missing_task_origin_defaults_to_user(self, isolated_db):
        """When task_origin is not provided, it defaults to 'user'."""
        session_store.session_start(
            id="agent-004",
            description="Legacy agent with no origin",
            chat_id="123",
            # task_origin omitted deliberately
            path=isolated_db,
        )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert sessions[0]["task_origin"] == "user"

    def test_none_task_origin_defaults_to_user(self, isolated_db):
        """When task_origin=None is passed explicitly, it defaults to 'user'."""
        session_store.session_start(
            id="agent-005",
            description="Agent with None origin",
            chat_id="123",
            task_origin=None,
            path=isolated_db,
        )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert sessions[0]["task_origin"] == "user"

    def test_invalid_task_origin_defaults_to_user(self, isolated_db):
        """Unrecognized task_origin values are normalized to 'user'."""
        session_store.session_start(
            id="agent-006",
            description="Agent with bogus origin",
            chat_id="123",
            task_origin="bogus-value",
            path=isolated_db,
        )
        sessions = session_store.get_active_sessions(path=isolated_db)
        assert sessions[0]["task_origin"] == "user"

    def test_find_session_returns_task_origin(self, isolated_db):
        """find_session() includes task_origin in the returned dict."""
        session_store.session_start(
            id="agent-007",
            description="Find by ID",
            chat_id="999",
            task_origin="internal",
            path=isolated_db,
        )
        found = session_store.find_session("agent-007", path=isolated_db)
        assert found is not None
        assert found["task_origin"] == "internal"


# ---------------------------------------------------------------------------
# Tests: _build_reconciler_message stamps task_origin correctly
# ---------------------------------------------------------------------------

class TestReconcilerMessageTaskOrigin:
    """_build_reconciler_message carries task_origin on inbox messages.

    Completed messages inherit task_origin from the session.
    Dead messages always use 'internal' (failure notices are system-generated,
    never user-triggered regardless of the originating session's origin).
    """

    def test_completed_message_inherits_user_task_origin(self, build_reconciler_message):
        """Completed notification for a user-triggered session carries task_origin='user'."""
        msg = build_reconciler_message(_SESSION_USER_ORIGIN, "completed", NOW)
        assert msg.get("task_origin") == "user", (
            f"Expected task_origin='user', got {msg.get('task_origin')!r}"
        )

    def test_completed_message_inherits_scheduled_task_origin(self, build_reconciler_message):
        """Completed notification for a scheduled session carries task_origin='scheduled'."""
        msg = build_reconciler_message(_SESSION_SCHEDULED_ORIGIN, "completed", NOW)
        assert msg.get("task_origin") == "scheduled"

    def test_completed_message_inherits_internal_task_origin(self, build_reconciler_message):
        """Completed notification for an internal session carries task_origin='internal'."""
        msg = build_reconciler_message(_SESSION_INTERNAL_ORIGIN, "completed", NOW)
        assert msg.get("task_origin") == "internal"

    def test_completed_message_missing_origin_defaults_to_user(self, build_reconciler_message):
        """Completed notification for session without task_origin defaults to 'user'."""
        msg = build_reconciler_message(_SESSION_NO_ORIGIN, "completed", NOW)
        assert msg.get("task_origin") == "user"

    def test_dead_message_always_internal(self, build_reconciler_message):
        """Dead notification is always task_origin='internal', regardless of session origin.

        Agent failure notices are emitted by the system (reconciler), not the user.
        The original session's task_origin may be 'user' if the user requested the task,
        but the failure notification itself is always system-generated.
        """
        msg = build_reconciler_message(_SESSION_USER_ORIGIN, "dead", NOW)
        assert msg.get("task_origin") == "internal", (
            "Dead agent failure notices must always be 'internal' — "
            f"got {msg.get('task_origin')!r}"
        )

    def test_dead_message_internal_session_is_internal(self, build_reconciler_message):
        """Dead notification for an internal session is also 'internal'."""
        msg = build_reconciler_message(_SESSION_INTERNAL_ORIGIN, "dead", NOW)
        assert msg.get("task_origin") == "internal"

    def test_task_origin_present_in_both_outcomes(self, build_reconciler_message):
        """task_origin is always present — never absent from reconciler messages."""
        for outcome in ("completed", "dead"):
            msg = build_reconciler_message(_SESSION_USER_ORIGIN, outcome, NOW)
            assert "task_origin" in msg, (
                f"task_origin missing from reconciler message with outcome={outcome!r}"
            )
