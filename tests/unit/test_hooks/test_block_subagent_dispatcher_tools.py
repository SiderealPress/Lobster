"""
Unit tests for hooks/block-subagent-dispatcher-tools.py

Tests cover:
- Non-blocked tool calls pass through (exit 0), regardless of agent_id
- wait_for_messages / check_inbox called by the dispatcher (no agent_id) pass
  through (exit 0)
- wait_for_messages / check_inbox called by a subagent (agent_id present) are
  hard-blocked (exit 2)
- Block message goes to stderr and names the blocked tool
- Malformed/missing stdin is tolerated (exit 0, fail open)
- agent_id present but falsy (empty string) is treated as dispatcher (exit 0)
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "block-subagent-dispatcher-tools.py"

_WFM_TOOL = "mcp__lobster-inbox__wait_for_messages"
_CHECK_INBOX_TOOL = "mcp__lobster-inbox__check_inbox"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hook_raw(stdin_text: str) -> tuple[int, str, str]:
    """Run the hook with raw stdin text (supports malformed-input tests)."""
    stdout_cap = StringIO()
    stderr_cap = StringIO()
    exit_code = None
    with (
        patch("sys.stdin", StringIO(stdin_text)),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
    ):
        try:
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code
    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _run_hook(hook_input: dict) -> tuple[int, str, str]:
    return _run_hook_raw(json.dumps(hook_input))


def _make_input(tool_name: str, agent_id: str | None = None, session_id: str = "sess-1") -> dict:
    data = {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": {},
    }
    if agent_id is not None:
        data["agent_id"] = agent_id
    return data


# ---------------------------------------------------------------------------
# Non-blocked tools always pass through
# ---------------------------------------------------------------------------


class TestNonBlockedToolPassthrough:
    def test_bash_passes_regardless_of_agent_id(self):
        exit_code, _, _ = _run_hook(_make_input("Bash", agent_id="agent-123"))
        assert exit_code == 0

    def test_send_reply_passes_even_as_subagent(self):
        # send_reply is dispatcher-conventional but not in this hook's scope —
        # only wait_for_messages/check_inbox are blocked here.
        exit_code, _, _ = _run_hook(
            _make_input("mcp__lobster-inbox__send_reply", agent_id="agent-123")
        )
        assert exit_code == 0

    def test_write_result_passes_as_subagent(self):
        exit_code, _, _ = _run_hook(
            _make_input("mcp__lobster-inbox__write_result", agent_id="agent-123")
        )
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Dispatcher (no agent_id) is allowed to call the guarded tools
# ---------------------------------------------------------------------------


class TestDispatcherAllowed:
    def test_dispatcher_wait_for_messages_exits_0(self):
        exit_code, _, _ = _run_hook(_make_input(_WFM_TOOL))
        assert exit_code == 0

    def test_dispatcher_check_inbox_exits_0(self):
        exit_code, _, _ = _run_hook(_make_input(_CHECK_INBOX_TOOL))
        assert exit_code == 0

    def test_empty_string_agent_id_treated_as_dispatcher(self):
        """CC should never send agent_id="" but the hook should fail open if it did."""
        exit_code, _, _ = _run_hook(_make_input(_WFM_TOOL, agent_id=""))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Subagent (agent_id present) is blocked from the guarded tools
# ---------------------------------------------------------------------------


class TestSubagentBlocked:
    def test_subagent_wait_for_messages_exits_2(self):
        exit_code, _, _ = _run_hook(_make_input(_WFM_TOOL, agent_id="agent-abc123"))
        assert exit_code == 2

    def test_subagent_check_inbox_exits_2(self):
        exit_code, _, _ = _run_hook(_make_input(_CHECK_INBOX_TOOL, agent_id="agent-abc123"))
        assert exit_code == 2

    def test_block_message_on_stderr_names_tool(self):
        exit_code, _, stderr = _run_hook(_make_input(_WFM_TOOL, agent_id="agent-abc123"))
        assert exit_code == 2
        assert _WFM_TOOL in stderr
        assert "BLOCKED" in stderr

    def test_block_message_mentions_write_result_guidance(self):
        _, _, stderr = _run_hook(_make_input(_CHECK_INBOX_TOOL, agent_id="agent-xyz"))
        assert "write_result" in stderr


# ---------------------------------------------------------------------------
# Fail-open on malformed input
# ---------------------------------------------------------------------------


class TestMalformedInput:
    def test_invalid_json_exits_0(self):
        exit_code, _, _ = _run_hook_raw("not valid json {{{")
        assert exit_code == 0

    def test_empty_stdin_exits_0(self):
        exit_code, _, _ = _run_hook_raw("")
        assert exit_code == 0
