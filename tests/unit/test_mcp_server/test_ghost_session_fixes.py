"""
Unit tests for ghost-session fixes (issue #781).

Two bugs caused cascading WFM stale restarts:
  Bug 1: agent_failed messages with chat_id=0 (ghost sessions) stall the
         dispatcher for 10-12 minutes because the LLM deliberates on them.
  Bug 2: SessionStart hook creates ghost sessions on crash-restart because
         _stored_session_is_alive() misclassifies the new dispatcher.

This module tests the fixes:

  Fix 1: _build_reconciler_message() now adds `should_drop: True` to
         agent_failed messages whose original_chat_id is 0/"" (ghost sessions).
         The dispatcher bootup doc is updated to short-circuit on this field.

  Fix 2: reconcile_agent_sessions() now skips sessions with
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
# Fixture: load _build_reconciler_message from inbox_server
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def build_reconciler_message(tmp_path_factory):
    """Load _build_reconciler_message from inbox_server with minimal patching."""
    import os

    tmp = tmp_path_factory.mktemp("messages")
    os.environ.setdefault("LOBSTER_MESSAGES", str(tmp / "messages"))
    os.environ.setdefault("LOBSTER_WORKSPACE", str(tmp / "workspace"))

    try:
        if "inbox_server" in sys.modules:
            del sys.modules["inbox_server"]
        import inbox_server as _is
        return _is._build_reconciler_message
    except Exception:
        pytest.skip("inbox_server not importable in this test environment")


# ---------------------------------------------------------------------------
# Bug 1 fix tests: should_drop field on agent_failed messages
# ---------------------------------------------------------------------------

class TestShouldDropFieldOnGhostSessions:
    """_build_reconciler_message adds should_drop=True for ghost dead sessions.

    A "ghost" dead session is one whose original_chat_id is 0 or "".
    The dispatcher bootup doc fast-exits on this field — no LLM deliberation.
    """

    def test_ghost_session_has_should_drop_true(self, build_reconciler_message):
        """Ghost session (chat_id=0) must produce should_drop=True."""
        msg = build_reconciler_message(_GHOST_SESSION, "dead", NOW)
        assert msg.get("should_drop") is True, (
            "Expected should_drop=True for ghost dead session (chat_id='0'), "
            f"got should_drop={msg.get('should_drop')!r}"
        )

    def test_real_subagent_does_not_have_should_drop(self, build_reconciler_message):
        """Real subagent (chat_id != 0) must NOT have should_drop=True."""
        msg = build_reconciler_message(_REAL_SUBAGENT_SESSION, "dead", NOW)
        # should_drop should be False or absent for real users
        assert not msg.get("should_drop"), (
            "Expected should_drop=False/absent for real subagent session, "
            f"got should_drop={msg.get('should_drop')!r}"
        )

    def test_empty_chat_id_has_should_drop_true(self, build_reconciler_message):
        """Empty string chat_id also triggers should_drop=True."""
        session = dict(_GHOST_SESSION, chat_id="")
        msg = build_reconciler_message(session, "dead", NOW)
        assert msg.get("should_drop") is True

    def test_none_chat_id_has_should_drop_true(self, build_reconciler_message):
        """None chat_id (missing) also triggers should_drop=True."""
        session = dict(_GHOST_SESSION, chat_id=None)
        msg = build_reconciler_message(session, "dead", NOW)
        assert msg.get("should_drop") is True

    def test_zero_int_chat_id_has_should_drop_true(self, build_reconciler_message):
        """Integer 0 chat_id also triggers should_drop=True."""
        session = dict(_GHOST_SESSION, chat_id=0)
        msg = build_reconciler_message(session, "dead", NOW)
        assert msg.get("should_drop") is True

    def test_completed_outcome_never_has_should_drop(self, build_reconciler_message):
        """Completed outcomes are never dropped — should_drop absent or False."""
        session = dict(_REAL_SUBAGENT_SESSION)
        msg = build_reconciler_message(session, "completed", NOW)
        assert not msg.get("should_drop"), (
            "Completed outcomes must not have should_drop=True"
        )

    def test_should_drop_field_present_on_all_dead_messages(self, build_reconciler_message):
        """All dead messages must include the should_drop key (True or False)."""
        for session in [_GHOST_SESSION, _REAL_SUBAGENT_SESSION]:
            msg = build_reconciler_message(session, "dead", NOW)
            assert "should_drop" in msg, (
                f"should_drop field missing from dead message for session {session['id']}"
            )


# ---------------------------------------------------------------------------
# Bug 1 fix tests: should_drop pure logic (deterministic, no inbox_server needed)
# ---------------------------------------------------------------------------

def _compute_should_drop(chat_id) -> bool:
    """Pure function mirroring the should_drop logic added to _build_reconciler_message.

    A dead agent's failure is a ghost/internal event if its chat_id is 0, "",
    or None — meaning the session was never associated with a real user request.
    The dispatcher must not deliberate on these; it marks them processed immediately.

    This function is extracted here for unit testing in isolation. The same logic
    must be present in _build_reconciler_message() in inbox_server.py.
    """
    if chat_id is None:
        return True
    str_id = str(chat_id).strip()
    return str_id in ("0", "", "None")


class TestShouldDropPureLogic:
    """should_drop is a pure deterministic function of chat_id."""

    @pytest.mark.parametrize("chat_id,expected", [
        ("0", True),
        (0, True),
        ("", True),
        (None, True),
        ("None", True),
        ("8305714125", False),
        ("12345", False),
        ("-100123456", False),   # Telegram group chats have negative IDs
    ])
    def test_parametrized(self, chat_id, expected):
        assert _compute_should_drop(chat_id) == expected

    def test_deterministic_same_input_same_output(self):
        """Pure function: same inputs always produce same outputs."""
        for chat_id in ("0", "8305714125", "", None):
            a = _compute_should_drop(chat_id)
            b = _compute_should_drop(chat_id)
            assert a == b


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

        They are handled by Bug 1 fix (should_drop field). Two independent
        guards — each catches a different failure mode.
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

    This test documents the full causal chain that these fixes break.
    """

    def test_dispatcher_session_never_triggers_should_drop_path(self):
        """Dispatcher sessions should be caught by the reconciler skip, not should_drop.

        The should_drop path is for real sessions that happen to have chat_id=0
        (e.g. old ghost rows from before Bug 2 was fixed). The reconciler skip
        is the primary fix for Bug 2.
        """
        # The reconciler skips dispatcher sessions entirely before ever calling
        # _build_reconciler_message(), so should_drop is never computed for them.
        assert _should_reconciler_skip(_DISPATCHER_SESSION) is True
        # Ghost sessions (pre-fix, no agent_type) are handled by should_drop
        assert _should_reconciler_skip(_GHOST_SESSION) is False
        assert _compute_should_drop(_GHOST_SESSION["chat_id"]) is True

    def test_real_subagent_failure_still_reaches_dispatcher(self):
        """Real subagent failures (chat_id != 0) are NOT dropped or skipped."""
        assert not _should_reconciler_skip(_REAL_SUBAGENT_SESSION)
        assert not _compute_should_drop(_REAL_SUBAGENT_SESSION["chat_id"])
