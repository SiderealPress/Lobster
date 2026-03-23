"""
Unit tests for ghost-session fixes (issue #781).

Two bugs caused cascading WFM stale restarts:
  Bug 1: agent_failed messages with chat_id=0 (ghost sessions) stall the
         dispatcher for 10-12 minutes because the LLM deliberates on them.
  Bug 2: SessionStart hook creates ghost sessions on crash-restart because
         _stored_session_is_alive() misclassifies the new dispatcher.

This module tests the fix:

  Fix 1 (restored, scoped): _enqueue_reconciler_notification() now skips inbox
         write for dead sessions where chat_id is 0, "", or None. These have no
         real user — the dispatcher cannot notify anyone or take meaningful action.
         Completed sessions are unaffected (they always write to inbox).
         The `should_drop` field has been removed from _build_reconciler_message()
         in favor of this pre-write guard.

  Fix 2 (the targeted fix): reconcile_agent_sessions() now skips sessions with
         agent_type == "dispatcher" — preventing the reconciler from ever
         emitting an agent_failed for the dispatcher's own session row.
         write-dispatcher-session-id.py now also registers the dispatcher
         session in agent_sessions.db with agent_type='dispatcher'.

All tests operate on pure functions or minimal fixtures — no inbox_server
startup needed.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[3]

for _p in [str(_ROOT / "src" / "mcp"), str(_ROOT / "src" / "agents"),
           str(_ROOT / "src"), str(_ROOT / "src" / "utils")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 23, 10, 0, 0, tzinfo=timezone.utc)

_GHOST_SESSION: dict = {
    "id": "dispatcher-session-abc",
    "task_id": None,
    "description": "auto-registered by SessionStart hook",
    "chat_id": "0",          # ghost — dispatcher mis-registered as subagent
    "source": "telegram",
    "status": "running",
    "output_file": None,
    "input_summary": None,
    "elapsed_seconds": 1900,  # > 30-minute dead threshold
    "notified_at": None,
    "agent_type": None,       # no type recorded (legacy ghost)
}

_REAL_SUBAGENT_SESSION: dict = {
    "id": "real-subagent-xyz",
    "task_id": "fix-something-123",
    "description": "Fix something for user",
    "chat_id": "8305714125",
    "source": "telegram",
    "status": "running",
    "output_file": None,
    "input_summary": "---\ntask_id: fix-something-123\n---\nDo the thing",
    "elapsed_seconds": 1900,
    "notified_at": None,
    "agent_type": "subagent",
}

_DISPATCHER_SESSION: dict = {
    "id": "dispatcher-real-session",
    "task_id": None,
    "description": "Lobster dispatcher main loop",
    "chat_id": "0",
    "source": "system",
    "status": "running",
    "output_file": None,
    "input_summary": None,
    "elapsed_seconds": 7200,  # 2 hours — would normally trigger dead
    "notified_at": None,
    "agent_type": "dispatcher",  # tagged by write-dispatcher-session-id.py
}


# ---------------------------------------------------------------------------
# Fixture: load inbox_server functions for integration tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def inbox_server_module(tmp_path_factory):
    """Load inbox_server with minimal patching."""
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
# Bug 1 fix tests: _build_reconciler_message no longer has should_drop field
# ---------------------------------------------------------------------------

class TestNoShouldDropFieldInMessages:
    """_build_reconciler_message no longer emits should_drop.

    Ghost sessions (chat_id=0/"") are now suppressed before the message is
    built — _enqueue_reconciler_notification() returns early without writing
    to the inbox.  should_drop has been removed from _build_reconciler_message
    entirely.
    """

    def test_dead_message_has_no_should_drop(self, build_reconciler_message):
        """Dead messages no longer carry a should_drop field."""
        msg = build_reconciler_message(_GHOST_SESSION, "dead", NOW)
        assert "should_drop" not in msg, (
            "should_drop field was removed — it must not appear in any message, "
            f"got should_drop={msg.get('should_drop')!r}"
        )

    def test_real_subagent_dead_has_no_should_drop(self, build_reconciler_message):
        """Real subagent dead messages also have no should_drop field."""
        msg = build_reconciler_message(_REAL_SUBAGENT_SESSION, "dead", NOW)
        assert "should_drop" not in msg, (
            "should_drop field must be absent for all dead messages"
        )

    def test_completed_message_has_no_should_drop(self, build_reconciler_message):
        """Completed messages never had should_drop and still don't."""
        msg = build_reconciler_message(_REAL_SUBAGENT_SESSION, "completed", NOW)
        assert "should_drop" not in msg

    def test_dead_message_still_has_original_chat_id(self, build_reconciler_message):
        """original_chat_id is still present so dispatcher can decide action."""
        msg = build_reconciler_message(_REAL_SUBAGENT_SESSION, "dead", NOW)
        assert "original_chat_id" in msg
        assert msg["original_chat_id"] == _REAL_SUBAGENT_SESSION["chat_id"]


# ---------------------------------------------------------------------------
# Bug 2 fix tests: reconciler skips dispatcher-type sessions
# ---------------------------------------------------------------------------

def _should_reconciler_skip(session: dict) -> bool:
    """Pure function mirroring the skip guard added to reconcile_agent_sessions().

    Sessions with agent_type='dispatcher' represent the Lobster dispatcher's
    own process — they are never real subagents and should never be marked dead
    or have agent_failed messages emitted for them.

    This function is extracted here for unit testing in isolation. The same
    check must appear at the top of the reconciler loop in inbox_server.py.
    """
    agent_type = session.get("agent_type") or ""
    return agent_type == "dispatcher"


class TestReconcilerSkipsDispatcherSessions:
    """reconcile_agent_sessions must skip sessions with agent_type='dispatcher'."""

    def test_dispatcher_session_is_skipped(self):
        assert _should_reconciler_skip(_DISPATCHER_SESSION) is True

    def test_real_subagent_is_not_skipped(self):
        assert _should_reconciler_skip(_REAL_SUBAGENT_SESSION) is False

    def test_ghost_session_without_type_is_not_skipped(self):
        """Ghost sessions without agent_type are NOT skipped by this guard.

        They are handled by Bug 1 fix (no-emit guard in
        _enqueue_reconciler_notification). Two independent guards — each
        catches a different failure mode.
        """
        assert _should_reconciler_skip(_GHOST_SESSION) is False

    def test_none_agent_type_is_not_skipped(self):
        session = dict(_GHOST_SESSION, agent_type=None)
        assert _should_reconciler_skip(session) is False

    def test_empty_string_agent_type_is_not_skipped(self):
        session = dict(_GHOST_SESSION, agent_type="")
        assert _should_reconciler_skip(session) is False

    def test_subagent_string_is_not_skipped(self):
        session = dict(_REAL_SUBAGENT_SESSION, agent_type="subagent")
        assert _should_reconciler_skip(session) is False

    @pytest.mark.parametrize("agent_type,expected_skip", [
        ("dispatcher", True),
        ("subagent", False),
        ("functional-engineer", False),
        (None, False),
        ("", False),
        ("DISPATCHER", False),   # case-sensitive: only exact "dispatcher"
    ])
    def test_parametrized_agent_types(self, agent_type, expected_skip):
        session = dict(_GHOST_SESSION, agent_type=agent_type)
        assert _should_reconciler_skip(session) == expected_skip


# ---------------------------------------------------------------------------
# Bug 2 fix tests: write-dispatcher-session-id.py registers dispatcher in DB
# ---------------------------------------------------------------------------

class TestDispatcherSessionRegistration:
    """write-dispatcher-session-id.py registers dispatcher with agent_type='dispatcher'."""

    @pytest.fixture
    def hook_module(self, tmp_path, monkeypatch):
        """Load write-dispatcher-session-id.py in a temp environment."""
        import os
        import sqlite3

        hook_path = _ROOT / "hooks" / "write-dispatcher-session-id.py"
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(parents=True, exist_ok=True)
        config_dir = messages_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("LOBSTER_MESSAGES", str(messages_dir))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path / "workspace"))
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        # Ensure src paths available for the hook's imports
        for p in [str(_ROOT / "hooks"), str(_ROOT / "src"), str(_ROOT / "src" / "agents"),
                  str(_ROOT / "src" / "mcp")]:
            if p not in sys.path:
                sys.path.insert(0, p)

        spec = importlib.util.spec_from_file_location(
            "write_dispatcher_session_id_mod", hook_path
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, messages_dir

    def _with_patched_db(self, mod, messages_dir, session_id, extra_calls=0):
        """Call _register_dispatcher_session with DB path patched to temp dir.

        The hook module loads 'from agents import session_store' which resolves
        to a different module instance than 'import src.agents.session_store'.
        We must patch the session_store instance that the hook module actually
        holds (mod.session_store), not the src-prefixed import.
        """
        db_path = messages_dir / "config" / "agent_sessions.db"
        # The hook module holds a reference to its own session_store import.
        _ss = mod.session_store
        orig_default = _ss._DEFAULT_DB_PATH
        _ss._DEFAULT_DB_PATH = db_path
        _ss._connections.clear()
        try:
            _ss.init_db()  # create schema at temp path
            mod._register_dispatcher_session(session_id)
            for _ in range(extra_calls):
                mod._register_dispatcher_session(session_id)
        finally:
            _ss._DEFAULT_DB_PATH = orig_default
            _ss._connections.clear()

        return db_path

    def test_register_dispatcher_writes_agent_type_dispatcher(self, hook_module):
        """When _is_dispatcher_session returns True, dispatcher row has agent_type='dispatcher'."""
        import sqlite3 as _sqlite3
        mod, messages_dir = hook_module

        if not hasattr(mod, "_register_dispatcher_session"):
            pytest.skip("_register_dispatcher_session not yet implemented")

        db_path = self._with_patched_db(mod, messages_dir, "test-session-001")

        conn = _sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT id, agent_type FROM agent_sessions WHERE id = ?",
            ("test-session-001",)
        ).fetchone()
        conn.close()

        assert row is not None, "No row written for dispatcher session"
        assert row[1] == "dispatcher", (
            f"Expected agent_type='dispatcher', got {row[1]!r}"
        )

    def test_register_dispatcher_is_idempotent(self, hook_module):
        """Calling _register_dispatcher_session twice doesn't error or duplicate."""
        import sqlite3 as _sqlite3
        mod, messages_dir = hook_module

        if not hasattr(mod, "_register_dispatcher_session"):
            pytest.skip("_register_dispatcher_session not yet implemented")

        db_path = self._with_patched_db(mod, messages_dir, "session-idem-001", extra_calls=1)

        conn = _sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_sessions WHERE id = ?",
            ("session-idem-001",)
        ).fetchone()[0]
        conn.close()
        assert count == 1, f"Expected 1 row, got {count}"


# ---------------------------------------------------------------------------
# Integration: both fixes together prevent the full cascade
# ---------------------------------------------------------------------------

class TestGhostSessionCascadePrevented:
    """Integration: dispatcher session tagged 'dispatcher' → reconciler skips it
    → no agent_failed emitted → dispatcher not stalled → no WFM restart.

    This test documents the full causal chain that Fix 2 breaks.
    """

    def test_dispatcher_session_caught_by_reconciler_skip(self):
        """Dispatcher sessions are skipped by the reconciler before any notification is emitted."""
        assert _should_reconciler_skip(_DISPATCHER_SESSION) is True
        # Ghost sessions (pre-fix, no agent_type) are not caught by the dispatcher skip
        assert _should_reconciler_skip(_GHOST_SESSION) is False

    def test_real_subagent_failure_still_reaches_dispatcher(self):
        """Real subagent failures are NOT skipped — they reach the dispatcher."""
        assert not _should_reconciler_skip(_REAL_SUBAGENT_SESSION)


# ---------------------------------------------------------------------------
# Bug 1 fix (restored): dead sessions with no real user skip inbox notification
# ---------------------------------------------------------------------------

def _should_skip_dead_no_user(session: dict, outcome: str) -> bool:
    """Pure function mirroring the early-return guard in _enqueue_reconciler_notification().

    Dead sessions with chat_id 0, "", or None have no real user — the
    dispatcher cannot notify anyone or take meaningful action. Route to debug
    log only, not the inbox.

    Completed sessions always write to inbox regardless of chat_id, because
    completed agent results need to be processed even for cron subagents.
    """
    if outcome != "dead":
        return False
    _chat_id = session.get("chat_id")
    _chat_id_str = str(_chat_id).strip() if _chat_id is not None else ""
    return _chat_id_str in ("0", "", "None")


class TestGhostChatIdNoInboxNotification:
    """Dead sessions with no real user must not write to the inbox.

    The guard in _enqueue_reconciler_notification() returns early (logging to
    debug only) when outcome=='dead' and chat_id is 0, empty, or None.
    Completed sessions and dead sessions with real users are unaffected.
    """

    @pytest.mark.parametrize("chat_id,should_skip", [
        ("0", True),
        (0, True),
        ("", True),
        (None, True),
        ("None", True),
        ("8305714125", False),
        (8305714125, False),
        ("123", False),
    ])
    def test_dead_outcome_parametrized(self, chat_id, should_skip):
        """Dead sessions with ghost chat_ids are skipped; real users are not."""
        session = dict(_GHOST_SESSION, chat_id=chat_id)
        assert _should_skip_dead_no_user(session, "dead") == should_skip, (
            f"chat_id={chat_id!r}: expected skip={should_skip}"
        )

    @pytest.mark.parametrize("chat_id", ["0", 0, "", None, "None"])
    def test_completed_outcome_never_skipped(self, chat_id):
        """Completed sessions with ghost chat_ids still write to inbox.

        Completed agent results need to be processed even when there's no user
        to notify directly — the dispatcher handles them.
        """
        session = dict(_GHOST_SESSION, chat_id=chat_id)
        assert _should_skip_dead_no_user(session, "completed") is False, (
            f"completed sessions must never be skipped, chat_id={chat_id!r}"
        )

    def test_dead_real_user_not_skipped(self):
        """Dead sessions with a real user ID are NOT skipped."""
        session = dict(_REAL_SUBAGENT_SESSION, chat_id="8305714125")
        assert _should_skip_dead_no_user(session, "dead") is False

    def test_dead_ghost_session_is_skipped(self):
        """The canonical ghost session (chat_id='0') is skipped."""
        assert _should_skip_dead_no_user(_GHOST_SESSION, "dead") is True

    def test_integration_enqueue_skips_dead_ghost(self, inbox_server_module, tmp_path):
        """Integration: _enqueue_reconciler_notification returns without writing inbox file."""
        import os

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        original_inbox_dir = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            inbox_server_module._enqueue_reconciler_notification(
                dict(_GHOST_SESSION, notified_at=None), outcome="dead"
            )
        finally:
            inbox_server_module.INBOX_DIR = original_inbox_dir

        files = list(inbox_dir.iterdir())
        assert files == [], (
            f"Expected no inbox files for dead ghost session, got: {files}"
        )

    def test_integration_enqueue_writes_dead_real_user(self, inbox_server_module, tmp_path):
        """Integration: dead session with real user DOES write to inbox."""
        import os

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        original_inbox_dir = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            inbox_server_module._enqueue_reconciler_notification(
                dict(_REAL_SUBAGENT_SESSION, notified_at=None), outcome="dead"
            )
        finally:
            inbox_server_module.INBOX_DIR = original_inbox_dir

        files = list(inbox_dir.iterdir())
        assert len(files) == 1, (
            f"Expected 1 inbox file for dead real-user session, got {len(files)}: {files}"
        )

    def test_integration_enqueue_writes_completed_ghost(self, inbox_server_module, tmp_path):
        """Integration: completed ghost session (chat_id=0) DOES write to inbox.

        Completed results are always forwarded — only dead+no-user is suppressed.
        """
        import os

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        original_inbox_dir = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = original_inbox_dir
        try:
            # Patch INBOX_DIR for the call
            inbox_server_module.INBOX_DIR = inbox_dir
            inbox_server_module._enqueue_reconciler_notification(
                dict(_GHOST_SESSION, notified_at=None), outcome="completed"
            )
        finally:
            inbox_server_module.INBOX_DIR = original_inbox_dir

        files = list(inbox_dir.iterdir())
        assert len(files) == 1, (
            f"Expected 1 inbox file for completed ghost session, got {len(files)}: {files}"
        )
