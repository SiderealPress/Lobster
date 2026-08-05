"""
Tests for dispatcher self-registration PID capture in handle_session_start()
(issue #2148, Phase 1 — PID ground truth).

handle_session_start() executes inside the stdio `lobster-inbox` MCP server
child process (spawned by the `claude` binary per .mcp.json), which is NOT
the same OS PID as the dispatcher's own `claude` process. A flat os.getpid()
there would record the MCP server child's PID, not the dispatcher's — so for
agent_type="dispatcher" self-registration, the real PID must be captured via
a process-tree walk to the nearest ancestor process named "claude"
(agents.pid_liveness.find_dispatcher_ancestor_pid), the same technique
hooks/session_role.py already uses for is_dispatcher_session().

Behaviors tested:
- agent_type="dispatcher": find_dispatcher_ancestor_pid() is called, and its
  result is threaded through to session_store.session_start(pid=...).
- Any other agent_type (or none): find_dispatcher_ancestor_pid() is NOT
  called, and session_start() receives pid=None — no behavior change for
  the vast majority of callers (Agent-tool subagents, docker-worker, etc.).
- find_dispatcher_ancestor_pid() returning None (no "claude" ancestor found)
  → session_start() receives pid=None, no crash.
- find_dispatcher_ancestor_pid() raising → best-effort: registration still
  proceeds with pid=None, never blocked by a PID-capture failure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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

import src.mcp.inbox_server  # noqa: F401 — pre-load so patch.multiple resolves it


def _mock_session_store() -> MagicMock:
    store = MagicMock()
    store.session_start.return_value = None
    return store


def _run_session_start(args: dict, dispatcher_pid_result=424242, dispatcher_pid_side_effect=None):
    """Run handle_session_start with _session_store and the ancestor-walk mocked.

    Returns (mock_store, mock_ancestor_walk) so callers can assert on calls.
    """
    mock_store = _mock_session_store()
    mock_ancestor_walk = MagicMock()
    if dispatcher_pid_side_effect is not None:
        mock_ancestor_walk.side_effect = dispatcher_pid_side_effect
    else:
        mock_ancestor_walk.return_value = dispatcher_pid_result

    with patch.multiple(
        "src.mcp.inbox_server",
        _session_store=mock_store,
        _find_dispatcher_ancestor_pid=mock_ancestor_walk,
        _http_session_manager=None,
    ):
        from src.mcp.inbox_server import handle_session_start
        asyncio.run(handle_session_start(args))

    return mock_store, mock_ancestor_walk


class TestDispatcherPidCapture:
    def test_dispatcher_registration_captures_pid_via_ancestor_walk(self):
        """agent_type='dispatcher' → ancestor walk is called and its result is passed as pid."""
        store, ancestor_walk = _run_session_start(
            {
                "agent_id": "lobster-dispatcher",
                "description": "Dispatcher main loop",
                "chat_id": 12345,
                "agent_type": "dispatcher",
            },
            dispatcher_pid_result=424242,
        )
        ancestor_walk.assert_called_once()
        _, kwargs = store.session_start.call_args
        assert kwargs["pid"] == 424242

    def test_non_dispatcher_registration_does_not_walk_process_tree(self):
        """A regular subagent registration never calls the ancestor walk, pid stays None."""
        store, ancestor_walk = _run_session_start(
            {
                "agent_id": "agent-123",
                "description": "Some subagent",
                "chat_id": 12345,
                "agent_type": "general-purpose",
            },
        )
        ancestor_walk.assert_not_called()
        _, kwargs = store.session_start.call_args
        assert kwargs["pid"] is None

    def test_missing_agent_type_does_not_walk_process_tree(self):
        """No agent_type at all → same as non-dispatcher: no walk, pid=None."""
        store, ancestor_walk = _run_session_start(
            {
                "agent_id": "agent-456",
                "description": "Agent with no type",
                "chat_id": 12345,
            },
        )
        ancestor_walk.assert_not_called()
        _, kwargs = store.session_start.call_args
        assert kwargs["pid"] is None

    def test_dispatcher_registration_pid_none_when_no_ancestor_found(self):
        """Ancestor walk returning None (no 'claude' ancestor) → pid=None, no crash."""
        store, ancestor_walk = _run_session_start(
            {
                "agent_id": "lobster-dispatcher",
                "description": "Dispatcher main loop",
                "chat_id": 12345,
                "agent_type": "dispatcher",
            },
            dispatcher_pid_result=None,
        )
        ancestor_walk.assert_called_once()
        _, kwargs = store.session_start.call_args
        assert kwargs["pid"] is None

    def test_dispatcher_registration_survives_ancestor_walk_exception(self):
        """If the ancestor walk raises, registration still proceeds with pid=None."""
        store, ancestor_walk = _run_session_start(
            {
                "agent_id": "lobster-dispatcher",
                "description": "Dispatcher main loop",
                "chat_id": 12345,
                "agent_type": "dispatcher",
            },
            dispatcher_pid_side_effect=OSError("simulated /proc read failure"),
        )
        ancestor_walk.assert_called_once()
        assert store.session_start.called, "session_start must still be called even if PID capture fails"
        _, kwargs = store.session_start.call_args
        assert kwargs["pid"] is None
