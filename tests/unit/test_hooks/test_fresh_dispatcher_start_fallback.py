"""
Unit tests for the fresh-dispatcher-start fallback in hooks/on-fresh-start.py.

Issue #1768: when write-dispatcher-session-id.py misidentifies a newly restarted
dispatcher as a subagent (because the old JSONL file has a recent mtime), the
tertiary state file retains the previous session UUID.  is_dispatcher() then
returns False for the new dispatcher session, and on-fresh-start.py exits
before calling _mark_all_running_failed() — leaving stale "running" sessions
in agent_sessions.db.

The fix adds _is_fresh_dispatcher_start() as a fallback: when is_dispatcher()
would return False, we check whether the primary state file
(dispatcher-claude-session-id) is absent.  If so, the MCP server has not yet
received a session_start() call, which means the dispatcher has not started its
main loop yet — i.e., this IS the dispatcher's fresh start, not a subagent.

Subagents cannot fire this fallback because by the time any subagent starts
(Task() spawning), the dispatcher has already called session_start(), which
writes the primary file.  Compaction events are also excluded: on-compact.py
writes the primary file proactively for the new post-compact session UUID.

Tests cover:
- _is_fresh_dispatcher_start() returns True when primary file absent + no compaction
- _is_fresh_dispatcher_start() returns False when primary file is present
- _is_fresh_dispatcher_start() returns False during compaction events
- _is_fresh_dispatcher_start() returns False when LOBSTER_MAIN_SESSION != 1
- _is_fresh_dispatcher_start() returns False on OSError reading the path
- Integration: main() proceeds when is_dispatcher() returns False but primary file absent
- Integration: main() exits when is_dispatcher() returns False and primary file present
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"


class _PatchEnv:
    """Context manager to temporarily set / restore environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved: dict = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _load_module(
    compaction_state_override: str | None = None,
    mcp_claude_session_file_override: str | None = None,
    session_file_pointer_override: str | None = None,
    is_dispatcher_return: bool = True,
) -> object:
    """Load on-fresh-start.py in an isolated namespace, overriding key paths.

    Uses a unique module name per call to avoid polluting the sys.modules cache
    between test invocations.  The session_role stub is installed and removed
    within this function so it does not bleed into other test modules.
    """
    import types
    import uuid

    env_patch: dict = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override
    if mcp_claude_session_file_override:
        env_patch["LOBSTER_MCP_CLAUDE_SESSION_FILE_OVERRIDE"] = mcp_claude_session_file_override
    if session_file_pointer_override:
        env_patch["LOBSTER_CURRENT_SESSION_FILE_OVERRIDE"] = session_file_pointer_override

    # Build the stub before installing it.
    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: is_dispatcher_return  # type: ignore[attr-defined]

    # Use a unique module name so repeated calls don't share cached state.
    unique_name = f"on_fresh_start_fallback_test_{uuid.uuid4().hex}"

    # Save and restore the real session_role to avoid test pollution.
    real_session_role = sys.modules.get("session_role")
    try:
        sys.modules["session_role"] = stub
        with _PatchEnv(env_patch):
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        if real_session_role is None:
            sys.modules.pop("session_role", None)
        else:
            sys.modules["session_role"] = real_session_role

    # Override module-level path constants for isolation.
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    if mcp_claude_session_file_override:
        mod._MCP_CLAUDE_SESSION_FILE = Path(mcp_claude_session_file_override)
    if session_file_pointer_override:
        mod.CURRENT_SESSION_FILE_POINTER = Path(session_file_pointer_override)

    # Attach the stub to the module for test introspection, and point the
    # module's session_role reference at the stub (not the real module).
    mod.session_role = stub
    return mod


def _make_session_role_stub(is_dispatcher: bool = True) -> object:
    """Return a minimal session_role stub (kept for backwards compatibility)."""
    import types

    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: is_dispatcher  # type: ignore[attr-defined]
    return stub


def _make_non_compaction_input() -> dict:
    """Return a SessionStart payload that is NOT a compaction event.

    _is_compact_event() inspects compaction-state.json mtime, NOT the payload.
    An empty dict is sufficient because the function only reads the file.
    """
    return {"session_id": "new-session-uuid-1234"}


# ---------------------------------------------------------------------------
# Tests for _is_fresh_dispatcher_start()
# ---------------------------------------------------------------------------


class TestIsFreshDispatcherStart:
    """Unit tests for _is_fresh_dispatcher_start()."""

    def test_returns_true_when_primary_file_absent_and_no_compaction(
        self, tmp_path, monkeypatch
    ):
        """Primary file absent + non-compaction → fresh dispatcher start."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        absent_primary = str(tmp_path / "nonexistent-dispatcher-claude-session-id")
        # Compaction state file absent → _is_compact_event returns False.
        absent_compaction = str(tmp_path / "nonexistent-compaction-state.json")

        mod = _load_module(
            mcp_claude_session_file_override=absent_primary,
            compaction_state_override=absent_compaction,
        )
        result = mod._is_fresh_dispatcher_start(_make_non_compaction_input())
        assert result is True

    def test_returns_false_when_primary_file_present(self, tmp_path, monkeypatch):
        """Primary file exists → dispatcher already called session_start, not a fresh boot."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        primary_file = tmp_path / "dispatcher-claude-session-id"
        primary_file.write_text("old-session-uuid-abcd")
        absent_compaction = str(tmp_path / "nonexistent-compaction-state.json")

        mod = _load_module(
            mcp_claude_session_file_override=str(primary_file),
            compaction_state_override=absent_compaction,
        )
        result = mod._is_fresh_dispatcher_start(_make_non_compaction_input())
        assert result is False

    def test_returns_false_when_main_session_not_set(self, tmp_path, monkeypatch):
        """LOBSTER_MAIN_SESSION != 1 → not a Lobster-managed session, return False."""
        monkeypatch.delenv("LOBSTER_MAIN_SESSION", raising=False)
        absent_primary = str(tmp_path / "nonexistent-dispatcher-claude-session-id")
        absent_compaction = str(tmp_path / "nonexistent-compaction-state.json")

        mod = _load_module(
            mcp_claude_session_file_override=absent_primary,
            compaction_state_override=absent_compaction,
        )
        result = mod._is_fresh_dispatcher_start(_make_non_compaction_input())
        assert result is False

    def test_returns_false_when_main_session_is_zero(self, tmp_path, monkeypatch):
        """LOBSTER_MAIN_SESSION=0 → return False."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "0")
        absent_primary = str(tmp_path / "nonexistent-dispatcher-claude-session-id")
        absent_compaction = str(tmp_path / "nonexistent-compaction-state.json")

        mod = _load_module(
            mcp_claude_session_file_override=absent_primary,
            compaction_state_override=absent_compaction,
        )
        result = mod._is_fresh_dispatcher_start(_make_non_compaction_input())
        assert result is False

    def test_returns_false_during_compaction_event(self, tmp_path, monkeypatch):
        """Compaction event → subagents are still alive, must not mark them failed."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        absent_primary = str(tmp_path / "nonexistent-dispatcher-claude-session-id")

        # Write a fresh compaction-state.json (mtime = now) → _is_compact_event returns True.
        compaction_state = tmp_path / "compaction-state.json"
        compaction_state.write_text(
            json.dumps({"last_compaction_ts": "2026-04-23T07:00:00Z"})
        )

        mod = _load_module(
            mcp_claude_session_file_override=absent_primary,
            compaction_state_override=str(compaction_state),
        )
        result = mod._is_fresh_dispatcher_start(_make_non_compaction_input())
        assert result is False

    def test_returns_false_on_oserror_stat(self, tmp_path, monkeypatch):
        """OSError when stat-ing the primary file → return False (safe default)."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        absent_compaction = str(tmp_path / "nonexistent-compaction-state.json")

        mod = _load_module(
            compaction_state_override=absent_compaction,
        )

        # Patch _MCP_CLAUDE_SESSION_FILE.exists() to raise OSError.
        mock_path = MagicMock(spec=Path)
        mock_path.exists.side_effect = OSError("permission denied")
        mod._MCP_CLAUDE_SESSION_FILE = mock_path

        result = mod._is_fresh_dispatcher_start(_make_non_compaction_input())
        assert result is False


# ---------------------------------------------------------------------------
# Integration: main() gating — is_dispatcher() False + _is_fresh_dispatcher_start()
# ---------------------------------------------------------------------------


class TestMainGatingFallback:
    """Integration tests: main() proceeds / exits based on the combined dispatcher check."""

    def _make_compaction_state(self, tmp_path: Path, age_seconds: float = 3600) -> Path:
        """Write a compaction-state.json whose mtime is `age_seconds` in the past."""
        p = tmp_path / "compaction-state.json"
        p.write_text(json.dumps({"last_compaction_ts": "2026-01-01T00:00:00Z"}))
        mtime = time.time() - age_seconds
        os.utime(str(p), (mtime, mtime))
        return p

    def test_proceeds_when_is_dispatcher_false_but_primary_file_absent(
        self, tmp_path, monkeypatch
    ):
        """Issue #1768 fix: mark-failed runs even when is_dispatcher() returns False.

        Simulates the exact failure mode: write-dispatcher-session-id.py skips
        writing the tertiary file, so is_dispatcher() returns False.  But the
        primary file is absent (MCP server cleared it on restart), so
        _is_fresh_dispatcher_start() returns True and the hook proceeds.
        """
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        # Primary file absent → fresh start fallback active.
        absent_primary = str(tmp_path / "nonexistent-dispatcher-claude-session-id")
        # Compaction state file old (> 60s) → not a compaction event.
        old_compaction = self._make_compaction_state(tmp_path, age_seconds=3600)

        mod = _load_module(
            is_dispatcher_return=False,  # Simulate is_dispatcher() → False
            mcp_claude_session_file_override=absent_primary,
            compaction_state_override=str(old_compaction),
        )

        # Stub agent monitor path so the hook doesn't bail early.
        mock_agent_monitor = tmp_path / "agent-monitor.py"
        mock_agent_monitor.write_text("# stub")
        mod.AGENT_MONITOR = mock_agent_monitor

        # Capture _mark_all_running_failed calls.
        mark_failed_called: list[bool] = []
        mod._mark_all_running_failed = lambda: mark_failed_called.append(True)

        # Avoid _inject_compact_reminder side effects.
        mod._is_catchup_stale = lambda: False
        mod._has_recent_session_file = lambda: False
        mod._schedule_reflection_prompt = lambda trigger: None

        hook_input = json.dumps({"session_id": "new-session-uuid-1234"})

        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

        assert exc_info.value.code == 0
        assert len(mark_failed_called) == 1, (
            "_mark_all_running_failed must be called — the hook should not exit early "
            "just because is_dispatcher() returned False"
        )

    def test_exits_when_is_dispatcher_false_and_primary_file_present(
        self, tmp_path, monkeypatch
    ):
        """When primary file is present, _is_fresh_dispatcher_start returns False.

        This is the subagent case: the dispatcher has already called session_start
        (writing the primary file), so a non-matching session must be a subagent.
        The hook must exit without calling _mark_all_running_failed.
        """
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        # Primary file present → dispatcher already started.
        primary_file = tmp_path / "dispatcher-claude-session-id"
        primary_file.write_text("old-session-uuid-abcd")
        old_compaction = self._make_compaction_state(tmp_path, age_seconds=3600)

        mod = _load_module(
            is_dispatcher_return=False,  # is_dispatcher() returns False
            mcp_claude_session_file_override=str(primary_file),
            compaction_state_override=str(old_compaction),
        )

        mark_failed_called: list[bool] = []
        mod._mark_all_running_failed = lambda: mark_failed_called.append(True)

        hook_input = json.dumps({"session_id": "subagent-session-uuid-9999"})

        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

        # Exits 0 (early exit on the subagent guard).
        assert exc_info.value.code == 0
        assert len(mark_failed_called) == 0, (
            "_mark_all_running_failed must NOT be called for a subagent session"
        )

    def test_proceeds_normally_when_is_dispatcher_true(self, tmp_path, monkeypatch):
        """Happy path: is_dispatcher() returns True → hook proceeds regardless of primary file."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        # Primary file absent (but is_dispatcher() already returns True — doesn't matter).
        absent_primary = str(tmp_path / "nonexistent-primary")
        old_compaction = self._make_compaction_state(tmp_path, age_seconds=3600)

        mod = _load_module(
            is_dispatcher_return=True,
            mcp_claude_session_file_override=absent_primary,
            compaction_state_override=str(old_compaction),
        )

        mock_agent_monitor = tmp_path / "agent-monitor.py"
        mock_agent_monitor.write_text("# stub")
        mod.AGENT_MONITOR = mock_agent_monitor

        mark_failed_called: list[bool] = []
        mod._mark_all_running_failed = lambda: mark_failed_called.append(True)
        mod._is_catchup_stale = lambda: False
        mod._has_recent_session_file = lambda: False
        mod._schedule_reflection_prompt = lambda trigger: None

        hook_input = json.dumps({"session_id": "dispatcher-session-uuid"})

        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

        assert exc_info.value.code == 0
        assert len(mark_failed_called) == 1
