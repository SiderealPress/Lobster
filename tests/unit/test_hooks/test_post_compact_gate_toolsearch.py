"""
Unit tests for the ToolSearch always-allow path in hooks/post-compact-gate.py.

When a ToolSearch tool call is detected, the gate must return exit 0 and allow
the call through — even if the compact-pending sentinel is fresh and the gate
would otherwise block.

This path is critical to avoid a deadlock: HTTP MCP servers register all tools
as deferred, so ToolSearch is the only way to fetch the wait_for_messages schema
before the dispatcher can call it. Blocking ToolSearch during the compact window
means the gate says "call wait_for_messages" but wait_for_messages cannot be
resolved, creating an infinite block. See issue #1914.
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

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "post-compact-gate.py"

# Relative path (from HOME) where the hook looks for the sentinel.
SENTINEL_REL = Path("messages") / "config" / "compact-pending"


def _make_sentinel(home: Path) -> Path:
    """Create a fresh sentinel file at the expected location under home."""
    sentinel = home / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    return sentinel


def _load_hook(monkeypatch, tmp_path: Path):
    """Load post-compact-gate.py with HOME and LOBSTER_MAIN_SESSION overridden."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

    spec = importlib.util.spec_from_file_location("post_compact_gate", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    if str(_HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOKS_DIR))
    spec.loader.exec_module(mod)
    return mod


def _make_hook_input(
    tool_name: str,
    tool_input: dict | None = None,
    agent_id: str | None = None,
) -> dict:
    payload = {"tool_name": tool_name, "tool_input": tool_input or {}}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    """Run mod.main() with hook_input as stdin. Returns (exit_code, stdout, stderr)."""
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
    """Write the hook marker file so is_dispatcher_session returns True."""
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / "dispatcher-session-id"
    marker.write_text(session_id)
    return marker


# ---------------------------------------------------------------------------
# ToolSearch always-allow path
# ---------------------------------------------------------------------------


class TestToolSearchAlwaysAllow:
    """ToolSearch is allowed through the gate even when sentinel is fresh.

    Failure mode: if this path is missing, the dispatcher is deadlocked after
    compaction — the gate says "call wait_for_messages" but wait_for_messages
    cannot be resolved without ToolSearch pre-loading its schema.
    """

    def test_toolsearch_allowed_when_sentinel_fresh(self, monkeypatch, tmp_path):
        """ToolSearch exits 0 with no deny output even if compact-pending is fresh.

        This is the core regression test for issue #1914. A fresh sentinel would
        cause the gate to block any other tool, but ToolSearch must pass through
        unconditionally.
        """
        _make_sentinel(tmp_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-toolsearch")

        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(tool_name="ToolSearch", tool_input={"query": "wait_for_messages"})
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, (
            f"ToolSearch should exit 0 even with fresh sentinel, got {exit_code}. "
            f"stderr: {stderr!r}"
        )
        assert stdout.strip() == "", (
            f"ToolSearch should produce no deny output (empty stdout), got: {stdout!r}"
        )

    def test_toolsearch_allowed_and_sentinel_remains(self, monkeypatch, tmp_path):
        """ToolSearch does not delete the sentinel — it only passes through.

        After ToolSearch is allowed, the sentinel must still be present so that
        subsequent non-ToolSearch / non-wait_for_messages calls are still blocked.
        """
        sentinel = _make_sentinel(tmp_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-toolsearch-nodel")

        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(tool_name="ToolSearch", tool_input={"query": "schemas"})
        _run_hook(mod, hook_input)

        assert sentinel.exists(), (
            "Sentinel must NOT be deleted by a ToolSearch call — only wait_for_messages "
            "with the confirmation token clears the sentinel."
        )

    def test_other_tool_blocked_when_sentinel_fresh(self, monkeypatch, tmp_path):
        """Confirm that a non-ToolSearch tool is still blocked with a fresh sentinel.

        This is a sanity check that the ToolSearch allow-path does not accidentally
        disable the gate for all tools.
        """
        _make_sentinel(tmp_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-toolsearch-sanity")

        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(tool_name="mcp__lobster-inbox__write_result")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"Hook exited non-zero: {stderr}"
        assert stdout.strip() != "", (
            "Non-ToolSearch tool should produce a deny decision when sentinel is fresh, "
            f"got empty stdout. stderr: {stderr!r}"
        )
        output = json.loads(stdout)
        decision = output.get("hookSpecificOutput", {}).get("permissionDecision", "")
        assert decision == "deny", (
            f"Expected permissionDecision=deny for non-ToolSearch tool, got: {decision!r}"
        )
