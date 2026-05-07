"""
Unit tests for the simplified flag-file based dispatcher detection (issue #1908).

The new approach replaces UUID-based detection with a launcher-side flag file:
  ~/lobster-workspace/data/dispatcher-startup-flag

The launcher (claude-persistent.sh) writes its PID to this file before exec-ing
claude. inject-bootup-context.py checks:
  1. Flag file exists
  2. PID inside is still alive (kill -0)
  → If both: inject dispatcher bootup, delete the flag file
  → If flag exists but PID dead: stale flag, treat as subagent
  → If flag absent: treat as subagent

Tests cover:
- _is_startup_flag_dispatcher() returns True when flag present + PID live
- _is_startup_flag_dispatcher() returns False when flag absent
- _is_startup_flag_dispatcher() returns False when PID is dead (stale flag)
- _is_startup_flag_dispatcher() returns False when flag contains non-integer
- _is_startup_flag_dispatcher() returns False on OSError
- Flag file is deleted after dispatcher is detected
- Integration: main() injects dispatcher bootup when flag is live
- Integration: main() injects subagent bootup when flag is absent
- Integration: main() injects subagent bootup when flag PID is dead (stale)

session_role.is_dispatcher() new implementation:
- Returns True when startup flag exists + PID live
- Returns False when flag absent or PID dead
- is_dispatcher_session() (PreToolUse) left unchanged
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_INJECT_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"
_SESSION_ROLE_PATH = _HOOKS_DIR / "session_role.py"

# Threshold constant from inject-bootup-context.py — tests should reference this
# rather than hardcode the magic value.
STARTUP_FLAG_FILENAME = "dispatcher-startup-flag"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _load_inject_hook(*, workspace: Path) -> object:
    """Load inject-bootup-context.py with a controlled LOBSTER_WORKSPACE."""
    import uuid

    unique_name = f"inject_bootup_{uuid.uuid4().hex}"
    with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
        spec = importlib.util.spec_from_file_location(unique_name, _INJECT_HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _load_session_role(*, workspace: Path) -> object:
    """Load session_role.py with a controlled LOBSTER_WORKSPACE."""
    import uuid

    unique_name = f"session_role_{uuid.uuid4().hex}"
    with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
        spec = importlib.util.spec_from_file_location(unique_name, _SESSION_ROLE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _write_flag(data_dir: Path, pid: int) -> Path:
    """Write a startup flag file with the given PID."""
    data_dir.mkdir(parents=True, exist_ok=True)
    flag = data_dir / STARTUP_FLAG_FILENAME
    flag.write_text(f"{pid}\n")
    return flag


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be alive.

    Uses a PID in the high range that is very unlikely to exist, then verifies
    it is dead. Falls back to checking /proc for a non-existent PID.
    """
    # Try PIDs from a high range that almost certainly don't exist.
    for candidate in range(999990, 999999):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            # Exists but no permission — still alive
            continue
    # Fallback: use a nonsense PID
    return 999999


# ---------------------------------------------------------------------------
# Tests for _is_startup_flag_dispatcher() in inject-bootup-context.py
# ---------------------------------------------------------------------------


class TestIsStartupFlagDispatcher:
    """Unit tests for _is_startup_flag_dispatcher() in inject-bootup-context.py."""

    def test_returns_true_when_flag_present_and_pid_live(self, tmp_path):
        """Flag exists with a live PID → this is the dispatcher."""
        data_dir = tmp_path / "data"
        flag = _write_flag(data_dir, os.getpid())  # our own PID is definitely alive

        mod = _load_inject_hook(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag
        result = mod._is_startup_flag_dispatcher()

        assert result is True

    def test_returns_false_when_flag_absent(self, tmp_path):
        """Flag absent → not a launcher-marked dispatcher start."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        flag = data_dir / STARTUP_FLAG_FILENAME
        # flag does NOT exist

        mod = _load_inject_hook(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag
        result = mod._is_startup_flag_dispatcher()

        assert result is False

    def test_returns_false_when_pid_is_dead(self, tmp_path):
        """Flag contains a dead PID → stale flag, treat as subagent."""
        data_dir = tmp_path / "data"
        dead_pid = _dead_pid()
        flag = _write_flag(data_dir, dead_pid)

        mod = _load_inject_hook(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag
        result = mod._is_startup_flag_dispatcher()

        assert result is False

    def test_returns_false_when_flag_contains_non_integer(self, tmp_path):
        """Corrupted flag (non-integer content) → treat as absent, return False."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        flag = data_dir / STARTUP_FLAG_FILENAME
        flag.write_text("not-a-pid\n")

        mod = _load_inject_hook(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag
        result = mod._is_startup_flag_dispatcher()

        assert result is False

    def test_returns_false_when_flag_is_empty(self, tmp_path):
        """Empty flag file → treat as absent, return False."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        flag = data_dir / STARTUP_FLAG_FILENAME
        flag.write_text("")

        mod = _load_inject_hook(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag
        result = mod._is_startup_flag_dispatcher()

        assert result is False

    def test_returns_false_on_oserror(self, tmp_path):
        """OSError reading the flag → return False (safe default)."""
        mod = _load_inject_hook(workspace=tmp_path)

        mock_flag = MagicMock(spec=Path)
        mock_flag.exists.return_value = True
        mock_flag.read_text.side_effect = OSError("permission denied")
        mod.STARTUP_FLAG_FILE = mock_flag

        result = mod._is_startup_flag_dispatcher()

        assert result is False


# ---------------------------------------------------------------------------
# Tests: flag file is deleted after successful detection
# ---------------------------------------------------------------------------


class TestStartupFlagDeletion:
    """After dispatcher is detected via the startup flag, the flag must be deleted.

    This prevents a second session (e.g. a fast-spawning subagent) from also
    seeing the flag as a dispatcher marker.
    """

    def test_flag_deleted_after_dispatcher_detection(self, tmp_path):
        """Flag is removed when main() runs and detects the dispatcher."""
        # Set up bootup files.
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
        subagent_bootup = claude_dir / "sys.subagent.bootup.md"
        subagent_bootup.write_text("# SUBAGENT BOOTUP\n")

        data_dir = tmp_path / "data"
        flag = _write_flag(data_dir, os.getpid())
        assert flag.exists(), "flag must exist before main() runs"

        hook_input = json.dumps({"session_id": "dispatcher-uuid-abc"})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(tmp_path)}):
            spec = importlib.util.spec_from_file_location(
                "inject_flag_delete_test", _INJECT_HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        assert not flag.exists(), (
            "startup flag must be deleted after dispatcher bootup is injected"
        )

    def test_flag_NOT_deleted_for_subagent(self, tmp_path):
        """Flag is NOT deleted when the session is identified as a subagent.

        If a subagent starts while an old/stale flag exists (dead PID), the
        flag should remain untouched so the dispatcher can inspect it if needed.
        Actually: a dead-PID flag is treated as absent (subagent path), so
        no deletion occurs — the file stays until explicitly cleaned.
        """
        data_dir = tmp_path / "data"
        dead_pid = _dead_pid()
        flag = _write_flag(data_dir, dead_pid)

        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        subagent_bootup = claude_dir / "sys.subagent.bootup.md"
        subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")

        hook_input = json.dumps({"session_id": "some-subagent-uuid"})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(tmp_path)}):
            spec = importlib.util.spec_from_file_location(
                "inject_no_delete_test", _INJECT_HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        # Flag is still present (dead-PID path = subagent, no deletion)
        assert flag.exists(), "stale flag should remain — only deleted on live dispatcher detection"


# ---------------------------------------------------------------------------
# Integration: main() routing based on startup flag
# ---------------------------------------------------------------------------


class TestMainRoutingViaStartupFlag:
    """main() injects dispatcher or subagent bootup based on the startup flag."""

    def _setup_bootup_files(self, tmp_path: Path) -> tuple[Path, Path]:
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        subagent_bootup = claude_dir / "sys.subagent.bootup.md"
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
        subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
        return dispatcher_bootup, subagent_bootup

    def _run_hook(self, tmp_path: Path, flag: Path, session_id: str) -> tuple[str, str]:
        dispatcher_bootup, subagent_bootup = self._setup_bootup_files(tmp_path)
        hook_input = json.dumps({"session_id": session_id})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(tmp_path)}):
            spec = importlib.util.spec_from_file_location(
                f"inject_routing_{session_id[:8]}", _INJECT_HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            import io as _io
            from contextlib import redirect_stdout
            out_buf = _io.StringIO()
            with patch("sys.stdin", io.StringIO(hook_input)):
                with redirect_stdout(out_buf):
                    with pytest.raises(SystemExit):
                        mod.main()

        return out_buf.getvalue(), ""

    def test_live_flag_injects_dispatcher_bootup(self, tmp_path, capsys):
        """Flag present with live PID → dispatcher bootup injected."""
        data_dir = tmp_path / "data"
        flag = _write_flag(data_dir, os.getpid())

        self._setup_bootup_files(tmp_path)
        hook_input = json.dumps({"session_id": "any-session-uuid"})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(tmp_path)}):
            spec = importlib.util.spec_from_file_location(
                "inject_live_flag_test", _INJECT_HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = tmp_path / "lobster" / ".claude" / "sys.dispatcher.bootup.md"
            mod.SUBAGENT_BOOTUP = tmp_path / "lobster" / ".claude" / "sys.subagent.bootup.md"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out
        assert "SUBAGENT BOOTUP" not in captured.out

    def test_absent_flag_injects_subagent_bootup(self, tmp_path, capsys):
        """Flag absent → subagent bootup injected."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        flag = data_dir / STARTUP_FLAG_FILENAME  # does NOT exist

        self._setup_bootup_files(tmp_path)
        hook_input = json.dumps({"session_id": "subagent-uuid-1234"})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(tmp_path)}):
            spec = importlib.util.spec_from_file_location(
                "inject_no_flag_test", _INJECT_HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = tmp_path / "lobster" / ".claude" / "sys.dispatcher.bootup.md"
            mod.SUBAGENT_BOOTUP = tmp_path / "lobster" / ".claude" / "sys.subagent.bootup.md"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out
        assert "DISPATCHER BOOTUP" not in captured.out

    def test_stale_flag_dead_pid_injects_subagent_bootup(self, tmp_path, capsys):
        """Flag present with dead PID → stale, subagent bootup injected."""
        data_dir = tmp_path / "data"
        dead_pid = _dead_pid()
        flag = _write_flag(data_dir, dead_pid)

        self._setup_bootup_files(tmp_path)
        hook_input = json.dumps({"session_id": "stale-flag-session-uuid"})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(tmp_path)}):
            spec = importlib.util.spec_from_file_location(
                "inject_stale_flag_test", _INJECT_HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = tmp_path / "lobster" / ".claude" / "sys.dispatcher.bootup.md"
            mod.SUBAGENT_BOOTUP = tmp_path / "lobster" / ".claude" / "sys.subagent.bootup.md"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out, (
            "Stale flag (dead PID) must not trigger dispatcher bootup"
        )
        assert "DISPATCHER BOOTUP" not in captured.out


# ---------------------------------------------------------------------------
# Tests for session_role.is_dispatcher() — simplified flag-file check
# ---------------------------------------------------------------------------


class TestSessionRoleIsDispatcher:
    """is_dispatcher() must use the startup flag file, not UUID state files."""

    def test_returns_true_when_flag_present_and_pid_live(self, tmp_path):
        """is_dispatcher() returns True when startup flag has a live PID."""
        data_dir = tmp_path / "data"
        flag = _write_flag(data_dir, os.getpid())

        mod = _load_session_role(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag

        result = mod.is_dispatcher({})
        assert result is True

    def test_returns_false_when_flag_absent(self, tmp_path):
        """is_dispatcher() returns False when startup flag is absent."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        flag = data_dir / STARTUP_FLAG_FILENAME  # does NOT exist

        mod = _load_session_role(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag

        result = mod.is_dispatcher({})
        assert result is False

    def test_returns_false_when_flag_pid_is_dead(self, tmp_path):
        """is_dispatcher() returns False when flag PID is dead (stale)."""
        data_dir = tmp_path / "data"
        dead_pid = _dead_pid()
        flag = _write_flag(data_dir, dead_pid)

        mod = _load_session_role(workspace=tmp_path)
        mod.STARTUP_FLAG_FILE = flag

        result = mod.is_dispatcher({})
        assert result is False

    def test_is_dispatcher_session_unchanged(self, tmp_path):
        """is_dispatcher_session() must still exist and accept hook_input dict.

        PreToolUse hooks depend on is_dispatcher_session() — it must not be
        removed or have its signature changed.
        """
        mod = _load_session_role(workspace=tmp_path)

        # Verify the function exists and is callable.
        assert hasattr(mod, "is_dispatcher_session"), (
            "is_dispatcher_session() must remain in session_role.py"
        )
        assert callable(mod.is_dispatcher_session)

        # Verify it accepts a dict argument (hook_input format).
        # agent_id present → subagent fast path → False, no I/O needed.
        result = mod.is_dispatcher_session({"agent_id": "some-agent-id-123"})
        assert result is False, (
            "is_dispatcher_session() with agent_id present should return False "
            "(subagent fast path must remain intact)"
        )
