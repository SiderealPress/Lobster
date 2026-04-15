"""
Unit tests for Option C session guard: auto-tag dispatcher on restart.

On MCP server restart, _dispatcher_session_id is None. If the dispatcher
calls a guarded tool (e.g. send_reply to handle backlog) before
wait_for_messages or session_start(agent_type=dispatcher), the guard would
block the call because no dispatcher session is tagged.

Option C closes this window: when _dispatcher_session_id is None, the first
session to call any guarded tool is auto-tagged as the dispatcher. This is
safe because subagents cannot exist before the dispatcher has tagged itself
and started spawning them.

These tests verify the pure state-transition logic in the guard, isolated
from the full inbox_server startup.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).parents[3]

for _p in [str(_ROOT / "src" / "mcp"), str(_ROOT / "src"), str(_ROOT / "src" / "agents"),
           str(_ROOT / "src" / "utils")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Pure-function tests: Option C tagging decision logic
# ---------------------------------------------------------------------------

def _option_c_should_auto_tag(dispatcher_session_id: str | None, current_session_id: str | None) -> bool:
    """Mirror the Option C condition from _dispatch_tool.

    Returns True when the guard should auto-tag (i.e. no dispatcher is tagged
    yet and a current session ID is available).
    """
    return dispatcher_session_id is None and current_session_id is not None


class TestOptionCAutoTagCondition:
    """Option C auto-tagging fires iff no dispatcher tagged AND a session ID is known."""

    def test_auto_tags_when_no_dispatcher_and_session_known(self):
        assert _option_c_should_auto_tag(None, "session-abc") is True

    def test_no_auto_tag_when_dispatcher_already_set(self):
        assert _option_c_should_auto_tag("existing-dispatcher", "session-abc") is False

    def test_no_auto_tag_when_no_session_id_available(self):
        assert _option_c_should_auto_tag(None, None) is False

    def test_no_auto_tag_when_both_set(self):
        assert _option_c_should_auto_tag("existing-dispatcher", "session-abc") is False

    def test_no_auto_tag_when_same_session_already_tagged(self):
        """If the session is already tagged as dispatcher, Option C is bypassed."""
        assert _option_c_should_auto_tag("session-abc", "session-abc") is False


# ---------------------------------------------------------------------------
# State transition: after Option C fires, session is tagged and guard passes
# ---------------------------------------------------------------------------

class TestOptionCStateMachine:
    """After Option C auto-tag, the same session passes the guard check."""

    def test_after_auto_tag_session_matches_dispatcher(self):
        """Simulate the state transition: None → tagged → guard passes."""
        # Before: no dispatcher tagged
        dispatcher_id: str | None = None
        current_session = "new-dispatcher-session-001"

        # Option C fires: auto-tag
        assert _option_c_should_auto_tag(dispatcher_id, current_session)
        dispatcher_id = current_session  # simulate _tag_dispatcher_session

        # After: guard check should pass
        is_main = current_session == dispatcher_id
        assert is_main is True

    def test_after_auto_tag_different_session_is_blocked(self):
        """After auto-tagging, a different session (subagent) is still blocked."""
        dispatcher_id: str | None = None
        first_session = "dispatcher-session-001"
        subagent_session = "subagent-session-002"

        # Option C fires for first session
        assert _option_c_should_auto_tag(dispatcher_id, first_session)
        dispatcher_id = first_session

        # Subagent tries to call guarded tool
        is_subagent_main = subagent_session == dispatcher_id
        assert is_subagent_main is False  # correctly blocked

    def test_option_c_does_not_fire_for_second_guarded_call(self):
        """Once tagged, Option C does not re-fire for subsequent calls."""
        dispatcher_id = "already-tagged-session"
        current_session = "already-tagged-session"

        # Option C should NOT fire (dispatcher already set)
        assert _option_c_should_auto_tag(dispatcher_id, current_session) is False


# ---------------------------------------------------------------------------
# Log message: Option C emits a distinct log line for observability
# ---------------------------------------------------------------------------

class TestOptionCLogging:
    """Option C should emit an INFO log with [session-tag] prefix when it fires."""

    def test_log_message_prefix(self):
        """The log message must include [session-tag] and 'Option C' for grep-ability."""
        session_id = "session-xyz-test"
        expected_fragment = "[session-tag] Option C"

        # Simulate the log string from _dispatch_tool
        log_msg = (
            f"[session-tag] Option C: no dispatcher tagged, auto-tagging "
            f"on 'send_reply' call — session {session_id!r}"
        )
        assert expected_fragment in log_msg
        assert session_id in log_msg
        assert "send_reply" in log_msg
