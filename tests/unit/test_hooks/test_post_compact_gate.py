"""
Unit tests for hooks/post-compact-gate.py

Tests cover:
- Subagent fast path (agent_id present) → always passes through
- Non-dispatcher session → passes through
- No sentinel file → passes through
- Stale sentinel → passes through, sentinel cleaned up
- wait_for_messages with correct token → clears sentinel and passes
- wait_for_messages without token → blocked; error message contains token (#1762)
- Non-wait_for_messages tool with fresh sentinel → blocked; error message contains token (#1762)
"""

import importlib.util
import json
import os
import sys
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "post-compact-gate.py"

CONFIRMATION_TOKEN = "LOBSTER_COMPACTED_REORIENTED"
WAIT_FOR_MESSAGES_TOOL = "mcp__lobster-inbox__wait_for_messages"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_hook(monkeypatch, tmp_path: Path, *, sentinel_fresh: bool = False):
    """Load post-compact-gate.py as a fresh module with tmp paths wired in."""
    monkeypatch.setenv("HOME", str(tmp_path))

    spec = importlib.util.spec_from_file_location("post_compact_gate", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    spec.loader.exec_module(mod)

    # Override sentinel and log paths to tmp dirs.
    sentinel_dir = tmp_path / "messages" / "config"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    sentinel_file = sentinel_dir / "compact-pending"
    monkeypatch.setattr(mod, "SENTINEL_FILE", sentinel_file)

    log_dir = tmp_path / "lobster-workspace" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "LOG_FILE", log_dir / "compact-gate.log")

    if sentinel_fresh:
        sentinel_file.write_text("")

    return mod, sentinel_file


def _make_hook_input(
    tool_name: str = "mcp__lobster-inbox__write_result",
    tool_input: dict | None = None,
    agent_id: str | None = None,
    session_id: str = "sess-dispatcher-001",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "session_id": session_id,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    """Run mod.main() with hook_input on stdin. Returns (exit_code, stdout, stderr)."""
    stdout_cap = StringIO()
    stderr_cap = StringIO()
    stdin_data = json.dumps(hook_input)
    exit_code = None

    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
    ):
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _make_dispatcher_session_file(tmp_path: Path, session_id: str) -> Path:
    """Write hook marker file so is_dispatcher_session returns True."""
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / "dispatcher-session-id"
    marker.write_text(session_id)
    return marker


# ---------------------------------------------------------------------------
# Fast path: subagent / non-dispatcher detection
# ---------------------------------------------------------------------------

class TestFastPath:
    def test_subagent_passes_through(self, monkeypatch, tmp_path):
        """agent_id present → subagent → exit 0 immediately."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            agent_id="agent-sub-abc",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Subagent should always pass through"

    def test_non_dispatcher_passes_through(self, monkeypatch, tmp_path):
        """Non-dispatcher session → exit 0 even when sentinel is fresh."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: False)
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-unknown-999",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Non-dispatcher should always pass through"


# ---------------------------------------------------------------------------
# No sentinel / stale sentinel → allow everything
# ---------------------------------------------------------------------------

class TestNoSentinel:
    def test_no_sentinel_passes_through(self, monkeypatch, tmp_path):
        """No sentinel file → exit 0 for any tool."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=False)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        hook_input = _make_hook_input(tool_name="Bash", tool_input={"command": "ls"})
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0

    def test_stale_sentinel_passes_through(self, monkeypatch, tmp_path):
        """Sentinel older than TTL → exit 0, sentinel deleted."""
        mod, sentinel_file = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        # Make sentinel appear 20 minutes old (beyond the 10-minute TTL).
        old_time = time.time() - 1200
        os.utime(str(sentinel_file), (old_time, old_time))
        hook_input = _make_hook_input(tool_name="Bash", tool_input={"command": "ls"})
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Stale sentinel should not block"
        assert not sentinel_file.exists(), "Stale sentinel should be cleaned up"


# ---------------------------------------------------------------------------
# wait_for_messages with fresh sentinel
# ---------------------------------------------------------------------------

class TestWaitForMessages:
    def test_correct_token_clears_sentinel_and_passes(self, monkeypatch, tmp_path):
        """wait_for_messages with correct token → clears sentinel, exit 0."""
        mod, sentinel_file = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        hook_input = _make_hook_input(
            tool_name=WAIT_FOR_MESSAGES_TOOL,
            tool_input={"confirmation": CONFIRMATION_TOKEN},
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Correct token should be accepted"
        assert not sentinel_file.exists(), "Sentinel should be deleted after correct token"

    def test_no_token_blocked_with_token_in_error(self, monkeypatch, tmp_path):
        """wait_for_messages without token → blocked, error includes the token (#1762)."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        hook_input = _make_hook_input(
            tool_name=WAIT_FOR_MESSAGES_TOOL,
            tool_input={},
        )
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Gate denial uses permissionDecision=deny (exit 0 with deny payload)"
        output = json.loads(stdout)
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert CONFIRMATION_TOKEN in reason, (
            f"Error for wait_for_messages without token must include the confirmation token. "
            f"Got: {reason!r}"
        )

    def test_wrong_token_blocked_with_token_in_error(self, monkeypatch, tmp_path):
        """wait_for_messages with wrong token → blocked, error includes the token (#1762)."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        hook_input = _make_hook_input(
            tool_name=WAIT_FOR_MESSAGES_TOOL,
            tool_input={"confirmation": "WRONG_TOKEN"},
        )
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        output = json.loads(stdout)
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert CONFIRMATION_TOKEN in reason, (
            f"Error for wrong token must include the correct confirmation token. "
            f"Got: {reason!r}"
        )


# ---------------------------------------------------------------------------
# Non-wait_for_messages tools blocked — issue #1762: token must be in message
# ---------------------------------------------------------------------------

class TestBlockedToolsIncludeToken:
    """Regression tests for issue #1762.

    Before the fix, DENY_REASON did not include the confirmation token, forcing
    the dispatcher to make at least 2-3 extra blocked calls before learning the
    token value. After the fix, every blocked non-wait_for_messages call should
    include the full token so the dispatcher can recover in exactly one attempt.
    """

    @pytest.mark.parametrize("tool_name", [
        "ToolSearch",
        "Read",
        "Bash",
        "mcp__lobster-inbox__write_result",
        "mcp__lobster-inbox__create_task",
        "Agent",
    ])
    def test_blocked_call_includes_confirmation_token(
        self, tool_name, monkeypatch, tmp_path
    ):
        """Every gate-blocked tool call must include the confirmation token in the error."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        hook_input = _make_hook_input(tool_name=tool_name)
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, f"Gate denial exits 0 (permissionDecision=deny), got {exit_code}"
        output = json.loads(stdout)
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert CONFIRMATION_TOKEN in reason, (
            f"Blocked {tool_name!r} call must include the confirmation token "
            f"'{CONFIRMATION_TOKEN}' in the error message. Got: {reason!r}"
        )

    def test_deny_decision_is_deny(self, monkeypatch, tmp_path):
        """Blocked calls emit permissionDecision='deny'."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=True)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        hook_input = _make_hook_input(tool_name="Read")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        output = json.loads(stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# wait_for_messages without sentinel → passes through (normal operation)
# ---------------------------------------------------------------------------

class TestWaitForMessagesNoSentinel:
    def test_passes_through_when_no_sentinel(self, monkeypatch, tmp_path):
        """wait_for_messages with no sentinel → always passes (normal main loop)."""
        mod, _ = _load_hook(monkeypatch, tmp_path, sentinel_fresh=False)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)
        hook_input = _make_hook_input(
            tool_name=WAIT_FOR_MESSAGES_TOOL,
            tool_input={},
        )
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        # No deny payload should be emitted.
        assert stdout.strip() == "" or "permissionDecision" not in stdout
