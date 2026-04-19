"""
Unit tests for maintenance flag cleanup in hooks/on-fresh-start.py (issue #1656).

When Lobster starts successfully (fresh dispatcher restart, not compaction),
the maintenance flag written by `lobster stop` must be removed.  This makes
`lobster stop` a true pause: Lobster stays down until explicitly restarted.

The health check's 1-hour auto-clear timer has been removed (issue #1656).
The flag is only cleared via two paths:
  1. on-fresh-start.py on confirmed successful dispatcher start (this module)
  2. lobster start (src/cli cmd_start) before starting services

Validates:
- _clear_maintenance_flag() deletes the flag when present
- _clear_maintenance_flag() is a no-op when the flag is absent
- _clear_maintenance_flag() is silent on errors (does not propagate exceptions)
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
from unittest.mock import MagicMock, patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"

# Named constant matching the env var used in on-fresh-start.py for test isolation.
# Must match LOBSTER_MAINTENANCE_FLAG_OVERRIDE used in the hook.
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


def _make_session_role_stub(is_dispatcher: bool = True):
    """Return a minimal session_role stub that reports the given dispatcher state."""
    stub = MagicMock()
    stub.is_dispatcher.return_value = is_dispatcher
    return stub


def _load_module(
    compaction_state_override: str = None,
    inbox_dir: str = None,
    session_file_pointer_override: str = None,
    maintenance_flag_override: str = None,
    is_dispatcher: bool = True,
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
        _saved_sr = sys.modules.get("session_role")
        sys.modules["session_role"] = _make_session_role_stub(is_dispatcher)
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


# ---------------------------------------------------------------------------
# _clear_maintenance_flag() unit tests
# ---------------------------------------------------------------------------


def test_clear_flag_deletes_existing_flag(tmp_path: Path) -> None:
    """
    _clear_maintenance_flag() must delete the flag file when it is present.

    Failure mode: if the flag is not deleted on successful start, the health
    check will see the flag indefinitely and suppress all monitoring — Lobster
    stays in maintenance mode even though it is running.
    """
    flag = tmp_path / "lobster-maintenance"
    flag.write_text("stopped_at=2026-01-01T00:00:00+00:00 stopped_by=lobster")

    mod = _load_module(maintenance_flag_override=str(flag))
    mod._clear_maintenance_flag()

    assert not flag.exists(), (
        "_clear_maintenance_flag() did not delete the maintenance flag. "
        "The health check will see a stale flag and suppress monitoring indefinitely."
    )


def test_clear_flag_is_noop_when_absent(tmp_path: Path) -> None:
    """
    _clear_maintenance_flag() must not raise when the flag does not exist.

    Failure mode: if missing-flag raises, on-fresh-start.py crashes on every
    normal startup (no prior lobster stop), breaking the startup hook.
    """
    flag = tmp_path / "lobster-maintenance"
    assert not flag.exists()

    mod = _load_module(maintenance_flag_override=str(flag))
    # Should complete without raising
    mod._clear_maintenance_flag()


def test_clear_flag_is_silent_on_permission_error(tmp_path: Path) -> None:
    """
    _clear_maintenance_flag() must not propagate OSError on permission failure.

    The health check and startup hook must not crash if the flag exists but
    cannot be deleted (e.g. permission mismatch after a manual deploy).
    """
    flag = tmp_path / "lobster-maintenance"
    flag.write_text("stopped_at=2026-01-01T00:00:00+00:00 stopped_by=lobster")

    mod = _load_module(maintenance_flag_override=str(flag))

    # Patch Path.unlink to raise PermissionError
    with patch.object(Path, "unlink", side_effect=PermissionError("no write")):
        # Must not raise
        mod._clear_maintenance_flag()


# ---------------------------------------------------------------------------
# main() integration: flag cleared on genuine fresh restart, not compaction
# ---------------------------------------------------------------------------


def _make_stale_compaction_state(path: Path) -> None:
    """Write a compaction-state.json with a mtime older than COMPACTION_RECENCY_SECONDS."""
    path.write_text(json.dumps({"last_catchup_ts": "2020-01-01T00:00:00Z"}))
    # Make mtime 300 seconds in the past so it is NOT considered a recent compaction.
    old_mtime = time.time() - 300
    os.utime(path, (old_mtime, old_mtime))


def test_main_clears_flag_on_fresh_restart(tmp_path: Path) -> None:
    """
    main() must clear the maintenance flag on a genuine fresh dispatcher restart.

    Failure mode: if main() does not call _clear_maintenance_flag(), then
    `lobster stop` followed by `lobster start` leaves the flag in place.
    The health check sees the flag and suppresses all monitoring, so a
    re-started Lobster appears stopped to the health check forever.
    """
    flag = tmp_path / "lobster-maintenance"
    flag.write_text("stopped_at=2026-01-01T00:00:00+00:00 stopped_by=lobster")

    compaction_state = tmp_path / "compaction-state.json"
    _make_stale_compaction_state(compaction_state)

    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()

    session_ptr = tmp_path / "lobster-current-session-file"
    # No session file pointer → no compact-reminder injection needed

    agent_monitor = tmp_path / "agent-monitor.py"
    agent_monitor.write_text("# stub")

    mod = _load_module(
        compaction_state_override=str(compaction_state),
        inbox_dir=str(inbox_dir),
        session_file_pointer_override=str(session_ptr),
        maintenance_flag_override=str(flag),
    )
    mod.AGENT_MONITOR = agent_monitor

    # Patch subprocess calls and heavy helpers so we only test flag clearing.
    # main() ends with sys.exit(0) — catch SystemExit so the assertion below can run.
    with (
        patch.object(mod, "_mark_all_running_failed"),
        patch.object(mod, "_inject_compact_reminder"),
        patch.object(mod, "_schedule_reflection_prompt"),
        patch("subprocess.run"),
    ):
        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            mod.main.__globals__["sys"].stdin = __import__("io").StringIO(
                json.dumps({"session_id": "test-fresh"})
            )
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
        assert exc_info.value.code == 0, (
            f"main() exited with unexpected code {exc_info.value.code!r}; expected 0."
        )

    assert not flag.exists(), (
        "main() did not clear the maintenance flag on a fresh dispatcher restart. "
        "After `lobster stop` + `lobster start`, the health check would see a "
        "stale flag and suppress monitoring indefinitely."
    )


def test_main_does_not_clear_flag_on_compaction(tmp_path: Path) -> None:
    """
    main() must NOT clear the maintenance flag during a compaction restart.

    A compaction event means the dispatcher is already running and subagents
    are still alive. Clearing the flag here would be wrong if maintenance mode
    was intentionally set and the system happened to compact at the same time.
    More importantly, on a compaction event main() exits early before reaching
    the flag-clearing code — this test verifies that early-exit path.
    """
    flag = tmp_path / "lobster-maintenance"
    flag.write_text("stopped_at=2026-01-01T00:00:00+00:00 stopped_by=lobster")

    # Write a compaction state file with a RECENT mtime so it looks like a compaction.
    compaction_state = tmp_path / "compaction-state.json"
    compaction_state.write_text(json.dumps({"last_catchup_ts": "2026-01-01T00:00:00Z"}))
    # mtime = now → recent compaction

    mod = _load_module(
        compaction_state_override=str(compaction_state),
        maintenance_flag_override=str(flag),
    )

    with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
        mod.main.__globals__["sys"].stdin = __import__("io").StringIO(
            json.dumps({"session_id": "test-compact"})
        )
        with pytest.raises(SystemExit) as exc_info:
            mod.main()

    # main() exits 0 on compaction (before flag logic)
    assert exc_info.value.code == 0, (
        "main() did not exit early on a compaction event. "
        f"Got exit code {exc_info.value.code!r}."
    )
    assert flag.exists(), (
        "main() incorrectly cleared the maintenance flag during a compaction event. "
        "The flag must only be cleared on genuine fresh dispatcher restarts."
    )
