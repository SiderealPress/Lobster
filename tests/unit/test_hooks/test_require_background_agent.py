"""
Unit tests for hooks/require-background-agent.py

Tests cover:
- Non-Agent tool calls pass through (exit 0)
- Agent with run_in_background=True passes through (exit 0)
- Agent without run_in_background called by dispatcher: hard block (exit 2)
- Agent without run_in_background called by subagent: allowed (exit 0)
- Dispatcher detection via marker file
- Missing run_in_background key (falsy) from dispatcher: blocked (exit 2)
- Block message goes to stderr
"""

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

# Add hooks directory to path so session_role can be imported by both
# the hook under test (via exec) and directly by test methods.
_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOKS_DIR = _HOOKS_DIR
HOOK_PATH = HOOKS_DIR / "require-background-agent.py"


def _load_hook(monkeypatch, tmp_path):
    """Load require-background-agent.py as a fresh module for each test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("require_background_agent", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hook(hook_input: dict) -> tuple[int, str, str]:
    """
    Run the hook script as a subprocess-like call via exec.
    Returns (exit_code, stdout_text, stderr_text).
    """
    stdout_capture = StringIO()
    stderr_capture = StringIO()
    stdin_data = json.dumps(hook_input)

    exit_code = None
    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_capture),
        patch("sys.stderr", stderr_capture),
    ):
        try:
            # Execute the hook script directly; __file__ must be set so the
            # hook's sys.path.insert(0, Path(__file__).parent) works correctly.
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()


def _make_hook_input(
    tool_name: str,
    tool_input: dict,
    session_id: str = "sess-sub-001",
) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


def _setup_dispatcher_marker(tmp_path: Path, session_id: str) -> None:
    """Write the dispatcher session marker file under tmp_path/messages/config/."""
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "dispatcher-session-id").write_text(session_id)


# ---------------------------------------------------------------------------
# Non-Agent tool passthrough
# ---------------------------------------------------------------------------


class TestNonAgentTool:
    def test_non_agent_tool_exits_0(self, monkeypatch, tmp_path):
        """Any tool that is not Agent passes through immediately."""
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input("Bash", {"command": "ls"})
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0

    def test_mcp_tool_exits_0(self, monkeypatch, tmp_path):
        """MCP tools are not the Agent tool and must pass through."""
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "mcp__lobster-inbox__check_inbox", {}, session_id="dispatcher-sess"
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Agent with run_in_background=True
# ---------------------------------------------------------------------------


class TestAgentWithBackground:
    def test_agent_with_background_true_exits_0_dispatcher(self, monkeypatch, tmp_path):
        """Dispatcher calling Agent with run_in_background=True is always OK."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work", "run_in_background": True},
            session_id="dispatcher-sess-001",
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0

    def test_agent_with_background_true_exits_0_subagent(self, monkeypatch, tmp_path):
        """Subagent calling Agent with run_in_background=True is also fine."""
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work", "run_in_background": True},
            session_id="subagent-sess-999",
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Dispatcher calling Agent synchronously (the bad case)
# ---------------------------------------------------------------------------


class TestDispatcherSynchronousAgent:
    def test_dispatcher_agent_no_background_key_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher omitting run_in_background is hard-blocked (exit 2)."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work"},
            session_id="dispatcher-sess-001",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, f"Expected hard block (exit 2), got {exit_code}"

    def test_dispatcher_agent_background_false_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher passing run_in_background=False explicitly is hard-blocked."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work", "run_in_background": False},
            session_id="dispatcher-sess-001",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, f"Expected hard block (exit 2), got {exit_code}"

    def test_block_message_goes_to_stderr(self, monkeypatch, tmp_path):
        """Block message must appear on stderr so Claude Code injects it as feedback."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work"},
            session_id="dispatcher-sess-001",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2
        assert "BLOCKED" in stderr, f"Expected BLOCKED in stderr, got: {stderr!r}"
        assert "run_in_background" in stderr, f"Expected guidance in stderr, got: {stderr!r}"

    def test_block_message_not_in_stdout(self, monkeypatch, tmp_path):
        """Block message must not appear on stdout (stdout is for JSON responses)."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work"},
            session_id="dispatcher-sess-001",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2
        assert "BLOCKED" not in stdout


# ---------------------------------------------------------------------------
# Subagent calling Agent synchronously (must be allowed)
# ---------------------------------------------------------------------------


class TestSubagentSynchronousAgent:
    def test_subagent_agent_no_background_exits_0(self, monkeypatch, tmp_path):
        """Subagents may call Agent synchronously — hook must not fire for them."""
        # Marker file points to a different session ID (dispatcher is someone else).
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do nested work"},
            session_id="subagent-sess-999",  # Different from dispatcher session
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Subagent should be allowed to call Agent synchronously, got exit {exit_code}. "
            f"stderr={stderr!r}"
        )

    def test_subagent_no_marker_file_exits_0(self, monkeypatch, tmp_path):
        """No marker file means is_dispatcher() returns False → treat as subagent → allow."""
        import session_role
        # Point to a nonexistent file so marker check returns None → fallback → False
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do nested work"},
            session_id="some-sess",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Missing marker file should default to subagent (allow), got exit {exit_code}. "
            f"stderr={stderr!r}"
        )


# ---------------------------------------------------------------------------
# "Task" tool name (older CC versions use Task instead of Agent)
# ---------------------------------------------------------------------------


class TestTaskToolName:
    """CC older versions use "Task" as the tool name for spawning subagents.

    The hook must treat "Task" identically to "Agent".
    """

    def test_task_tool_dispatcher_sync_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher calling Task (old CC) without run_in_background is hard-blocked."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Task",
            {"prompt": "do work"},
            session_id="dispatcher-sess-001",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, (
            f"Dispatcher calling Task without run_in_background should be hard-blocked, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_task_tool_dispatcher_background_true_exits_0(self, monkeypatch, tmp_path):
        """Dispatcher calling Task with run_in_background=True is allowed."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Task",
            {"prompt": "do work", "run_in_background": True},
            session_id="dispatcher-sess-001",
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0

    def test_task_tool_subagent_sync_exits_0(self, monkeypatch, tmp_path):
        """Subagent calling Task synchronously is allowed (not the dispatcher)."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Task",
            {"prompt": "do nested work"},
            session_id="subagent-sess-999",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Subagent should be allowed to call Task synchronously, got exit {exit_code}. "
            f"stderr={stderr!r}"
        )
