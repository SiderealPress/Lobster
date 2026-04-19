"""
Unit tests for maintenance flag cleanup in hooks/on-fresh-start.py (issue #1656).

When Lobster starts successfully (fresh dispatcher restart, not compaction),
the maintenance flag written by `lobster stop` must be removed.  This makes
`lobster stop` a true pause: Lobster stays down until explicitly restarted,
and the health-check's 1-hour timer heuristic is no longer needed as the
primary recovery mechanism.

Validates:
- _clear_maintenance_flag() deletes the flag when present
- _clear_maintenance_flag() is a no-op when the flag is absent
- _clear_maintenance_flag() is silent on permission errors
- main() calls _clear_maintenance_flag() on a genuine fresh restart
- main() does NOT call _clear_maintenance_flag() during a compaction event
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"

# Named constant matching the env var used in on-fresh-start.py for test isolation.
MAINTENANCE_FLAG_ENV_VAR = "LOBSTER_MAINTENANCE_FLAG_OVERRIDE"


class _PatchEnv:
    """Context manager to temporarily set environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _load_module(
    compaction_state_override: str = None,
    inbox_dir: str = None,
    session_file_pointer_override: str = None,
    maintenance_flag_override: str = None,
) -> object:
    """Load on-fresh-start.py as a module with isolated file paths."""
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override
    if session_file_pointer_override:
        env_patch["LOBSTER_CURRENT_SESSION_FILE_OVERRIDE"] = session_file_pointer_override
    if maintenance_flag_override:
        env_patch[MAINTENANCE_FLAG_ENV_VAR] = maintenance_flag_override

    with _PatchEnv(env_patch):
        spec = importlib.util.spec_from_file_location("on_fresh_start", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        # Force-install the stub so the real session_role (which checks actual
        # session data) doesn't get picked up if it was loaded by a prior test.
        # setdefault() would leave the real module in place if already registered.
        _saved_sr = sys.modules.get("session_role")
        sys.modules["session_role"] = _make_session_role_stub()
        try:
            spec.loader.exec_module(mod)
        finally:
            if _saved_sr is None:
                sys.modules.pop("session_role", None)
            else:
                sys.modules["session_role"] = _saved_sr

    # Override runtime-resolved paths on the loaded module
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    if inbox_dir:
        mod.INBOX_DIR = Path(inbox_dir)
    if session_file_pointer_override:
        mod.CURRENT_SESSION_FILE_POINTER = Path(session_file_pointer_override)
    if maintenance_flag_override:
        mod.MAINTENANCE_FLAG = Path(maintenance_flag_override)
    return mod


def _make_session_role_stub():
    """Return a minimal session_role stub module."""
    import types
    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: True
    return stub


def _make_fresh_compaction_state(tmp_path: Path) -> Path:
    """Write a compaction-state.json updated within the recency window (simulates compaction)."""
    state_file = tmp_path / "compaction-state.json"
    state_file.write_text(json.dumps({"last_compaction_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    return state_file


def _make_stale_compaction_state(tmp_path: Path) -> Path:
    """Write a compaction-state.json with an old mtime (simulates fresh restart)."""
    state_file = tmp_path / "compaction-state.json"
    state_file.write_text(json.dumps({"last_catchup_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    # Set mtime to 2 minutes ago — outside the 60s compaction-recency window
    old_time = time.time() - 120
    os.utime(str(state_file), (old_time, old_time))
    return state_file


class TestClearMaintenanceFlag:
    """Tests for _clear_maintenance_flag()."""

    def test_deletes_flag_when_present(self, tmp_path):
        """Flag is removed when Lobster starts successfully."""
        flag = tmp_path / "lobster-maintenance"
        flag.write_text("stopped_at=2026-04-19T10:00:00+00:00 stopped_by=lobster\n")

        mod = _load_module(maintenance_flag_override=str(flag))
        mod._clear_maintenance_flag()

        assert not flag.exists(), "Maintenance flag should be deleted after successful start"

    def test_noop_when_flag_absent(self, tmp_path):
        """No-op when no maintenance flag exists — must not raise."""
        flag = tmp_path / "lobster-maintenance"
        assert not flag.exists()

        mod = _load_module(maintenance_flag_override=str(flag))
        # Must not raise
        mod._clear_maintenance_flag()

    def test_silent_on_permission_error(self, tmp_path):
        """Permission error while deleting the flag must not raise or crash the hook."""
        # Point to a path inside /proc where unlink will always fail
        mod = _load_module()
        mod.MAINTENANCE_FLAG = Path("/proc/lobster_test_nonexistent/lobster-maintenance")
        # Must not raise
        mod._clear_maintenance_flag()

    def test_logs_deletion_on_success(self, tmp_path):
        """_clear_maintenance_flag() emits a message to stderr when it deletes the flag."""
        import io
        flag = tmp_path / "lobster-maintenance"
        flag.write_text("stopped_at=2026-04-19T10:00:00+00:00 stopped_by=lobster\n")

        mod = _load_module(maintenance_flag_override=str(flag))

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            mod._clear_maintenance_flag()
        finally:
            sys.stderr = old_stderr

        output = captured.getvalue()
        assert "maintenance" in output.lower() or "flag" in output.lower(), (
            f"Expected maintenance/flag mention in stderr, got: {output!r}"
        )


class TestMainCallsClearMaintenanceFlag:
    """Integration tests: main() clears the maintenance flag on fresh restart."""

    def test_main_clears_flag_on_fresh_restart(self, tmp_path):
        """main() removes the maintenance flag when a genuine fresh dispatcher restart occurs.

        A genuine fresh restart: LOBSTER_MAIN_SESSION=1, is_dispatcher=True,
        and no recent compaction event (compaction-state.json mtime > 60s old).
        """
        # Set up directories main() will need
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        config = tmp_path / "config"
        config.mkdir()

        flag = config / "lobster-maintenance"
        flag.write_text("stopped_at=2026-04-19T10:00:00+00:00 stopped_by=lobster\n")

        state_file = _make_stale_compaction_state(tmp_path)

        # Prevent agent-monitor from actually running
        mod = _load_module(
            compaction_state_override=str(state_file),
            inbox_dir=str(inbox),
            maintenance_flag_override=str(flag),
        )
        mod.AGENT_MONITOR = Path("/nonexistent/agent-monitor.py")  # skip _mark_all_running_failed
        mod.INBOX_DIR = inbox
        mod.CURRENT_SESSION_FILE_POINTER = tmp_path / "nonexistent-pointer"

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            import io
            # Redirect stdin so main() can parse JSON (or empty) without hanging
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("{}")
            try:
                mod.main()
            except SystemExit:
                pass
            finally:
                sys.stdin = old_stdin

        assert not flag.exists(), (
            "Maintenance flag must be deleted when main() runs on a fresh dispatcher restart"
        )

    def test_main_does_not_clear_flag_on_compaction(self, tmp_path):
        """main() exits early during compaction events — maintenance flag is left untouched.

        During compaction, subagents are still running.  The hook must not run
        and must not clear the maintenance flag.
        """
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        config = tmp_path / "config"
        config.mkdir()

        flag = config / "lobster-maintenance"
        flag.write_text("stopped_at=2026-04-19T10:00:00+00:00 stopped_by=lobster\n")

        # Fresh compaction state — within the 60s recency window
        state_file = _make_fresh_compaction_state(tmp_path)

        mod = _load_module(
            compaction_state_override=str(state_file),
            inbox_dir=str(inbox),
            maintenance_flag_override=str(flag),
        )
        mod.AGENT_MONITOR = Path("/nonexistent/agent-monitor.py")
        mod.INBOX_DIR = inbox

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            import io
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("{}")
            try:
                mod.main()
            except SystemExit:
                pass
            finally:
                sys.stdin = old_stdin

        assert flag.exists(), (
            "Maintenance flag must NOT be deleted during a compaction event"
        )

    def test_main_noop_when_no_flag(self, tmp_path):
        """main() proceeds normally when no maintenance flag exists — no error raised."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        state_file = _make_stale_compaction_state(tmp_path)
        nonexistent_flag = tmp_path / "config" / "lobster-maintenance"
        assert not nonexistent_flag.exists()

        mod = _load_module(
            compaction_state_override=str(state_file),
            inbox_dir=str(inbox),
            maintenance_flag_override=str(nonexistent_flag),
        )
        mod.AGENT_MONITOR = Path("/nonexistent/agent-monitor.py")
        mod.INBOX_DIR = inbox
        mod.CURRENT_SESSION_FILE_POINTER = tmp_path / "nonexistent-pointer"

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            import io
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("{}")
            try:
                mod.main()
            except SystemExit:
                pass
            finally:
                sys.stdin = old_stdin

        # Flag still absent — no error
        assert not nonexistent_flag.exists()
