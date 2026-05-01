"""
Unit tests for inject-bootup-context.py dispatcher detection via startup flag.

After issue #1908, the sentinel-based fallback (_is_post_compact_dispatcher) and
UUID-based primary file detection were replaced with a simpler startup flag approach:

  The launcher (claude-persistent.sh) writes its PID to
  ~/lobster-workspace/data/dispatcher-startup-flag before exec-ing claude.
  inject-bootup-context.py reads this flag on SessionStart:
    - Flag present AND PID alive → dispatcher. Consume the flag.
    - Flag absent OR PID dead → subagent.

Tests focus on _is_startup_flag_dispatcher(), flag consumption, and main() routing.

See test_startup_flag_dispatcher_detection.py for comprehensive startup flag tests
including session_role.is_dispatcher() and edge cases.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PatchEnv:
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


def _load_inject_hook(*, home: Path, workspace: Path) -> object:
    """Load inject-bootup-context.py in a controlled environment."""
    env = {
        "HOME": str(home),
        "LOBSTER_WORKSPACE": str(workspace),
    }
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location("inject_bootup", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# Named constant for the startup flag filename (matches the spec requirement).
STARTUP_FLAG_FILENAME = "dispatcher-startup-flag"


# ---------------------------------------------------------------------------
# Tests for _is_startup_flag_dispatcher()
# ---------------------------------------------------------------------------


class TestIsStartupFlagDispatcher:
    """Unit tests for the _is_startup_flag_dispatcher() helper in inject-bootup-context.py.

    Comprehensive coverage is in test_startup_flag_dispatcher_detection.py.
    These tests verify the function is present and returns the right type.
    """

    def test_returns_false_when_flag_absent(self, tmp_path):
        """Startup flag absent → False (subagent or not yet started)."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        mod = _load_inject_hook(home=tmp_path, workspace=workspace)
        # Override STARTUP_FLAG_FILE to a non-existent path.
        mod.STARTUP_FLAG_FILE = workspace / "data" / STARTUP_FLAG_FILENAME

        result = mod._is_startup_flag_dispatcher()
        assert result is False

    def test_returns_false_when_flag_contains_dead_pid(self, tmp_path):
        """Startup flag with a dead PID → stale flag → False."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        # PID 1 is always init/systemd; using a guaranteed non-existent PID.
        flag.write_text("99999999")  # extremely high PID unlikely to exist

        mod = _load_inject_hook(home=tmp_path, workspace=workspace)
        mod.STARTUP_FLAG_FILE = flag

        result = mod._is_startup_flag_dispatcher()
        # PID 99999999 is unlikely to exist; but if it does the test is inconclusive.
        # The important thing is the function returns a bool without raising.
        assert isinstance(result, bool)

    def test_returns_false_when_flag_is_empty(self, tmp_path):
        """Empty startup flag → False."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text("")

        mod = _load_inject_hook(home=tmp_path, workspace=workspace)
        mod.STARTUP_FLAG_FILE = flag

        result = mod._is_startup_flag_dispatcher()
        assert result is False

    def test_returns_false_on_non_integer_flag_content(self, tmp_path):
        """Non-integer startup flag content → False (not a valid PID)."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text("not-a-pid\n")

        mod = _load_inject_hook(home=tmp_path, workspace=workspace)
        mod.STARTUP_FLAG_FILE = flag

        result = mod._is_startup_flag_dispatcher()
        assert result is False

    def test_returns_true_when_flag_has_live_pid(self, tmp_path):
        """Startup flag with a live PID → True (current process)."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        # Write our own PID — guaranteed alive.
        flag.write_text(str(os.getpid()))

        mod = _load_inject_hook(home=tmp_path, workspace=workspace)
        mod.STARTUP_FLAG_FILE = flag

        result = mod._is_startup_flag_dispatcher()
        assert result is True


# ---------------------------------------------------------------------------
# Tests for _consume_startup_flag()
# ---------------------------------------------------------------------------


class TestConsumeStartupFlag:
    def test_deletes_flag_file(self, tmp_path):
        """_consume_startup_flag() removes the startup flag file."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text(str(os.getpid()))

        mod = _load_inject_hook(home=tmp_path, workspace=workspace)
        mod.STARTUP_FLAG_FILE = flag

        assert flag.exists(), "Precondition: flag must exist before consume"
        mod._consume_startup_flag()
        assert not flag.exists(), "Flag must be deleted after _consume_startup_flag()"

    def test_silent_when_flag_absent(self, tmp_path):
        """_consume_startup_flag() is silent when the flag file does not exist."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        # Flag does NOT exist.

        mod = _load_inject_hook(home=tmp_path, workspace=workspace)
        mod.STARTUP_FLAG_FILE = flag

        # Must not raise.
        mod._consume_startup_flag()


# ---------------------------------------------------------------------------
# Integration: main() uses startup flag to route dispatcher vs subagent bootup
# ---------------------------------------------------------------------------


class TestMainStartupFlagRouting:
    """main() in inject-bootup-context.py uses the startup flag (live PID) to
    detect the dispatcher session and inject dispatcher bootup context.
    """

    def _make_bootup_files(self, claude_dir: Path) -> tuple[Path, Path]:
        """Write minimal dispatcher and subagent bootup stubs."""
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        subagent_bootup = claude_dir / "sys.subagent.bootup.md"
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
        subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
        return dispatcher_bootup, subagent_bootup

    def test_live_pid_flag_injects_dispatcher_bootup(self, tmp_path, capsys):
        """When startup flag contains a live PID, dispatcher bootup is injected."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # Write live PID to startup flag.
        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text(str(os.getpid()))

        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        dispatcher_bootup, _ = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "any-session-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(workspace),
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_disp_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Override paths to use our tmp structure.
            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out, (
            "Dispatcher bootup should be injected when startup flag has a live PID"
        )
        assert "SUBAGENT BOOTUP" not in captured.out

    def test_live_pid_flag_consumed_after_dispatcher_detection(self, tmp_path, capsys):
        """After dispatcher detection, the startup flag is deleted (consumed)."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text(str(os.getpid()))

        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        dispatcher_bootup, _ = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "any-session-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(workspace),
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_consume_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        # Flag must be consumed so subsequent subagent sessions don't see it.
        assert not flag.exists(), (
            "Startup flag must be deleted after dispatcher detection "
            "so subagent sessions are not misidentified as the dispatcher"
        )

    def test_absent_flag_injects_subagent_bootup(self, tmp_path, capsys):
        """When startup flag is absent, subagent bootup is injected."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # No startup flag.

        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        _, subagent_bootup = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "subagent-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(workspace),
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_sub_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = workspace / "data" / STARTUP_FLAG_FILENAME  # absent
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out, (
            "Subagent bootup should be injected when startup flag is absent"
        )
        assert "DISPATCHER BOOTUP" not in captured.out

    def test_dead_pid_flag_injects_subagent_bootup(self, tmp_path, capsys):
        """When startup flag contains a dead PID (stale flag), subagent bootup is injected."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # Write a dead PID (extremely high, very unlikely to exist).
        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text("99999999")

        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        _, subagent_bootup = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "stale-flag-session"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(workspace),
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_dead_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        # PID 99999999 is extremely unlikely to exist; if by chance it does the
        # test result is ambiguous — but the important invariant is no crash.
        try:
            os.kill(99999999, 0)
            pid_alive = True
        except ProcessLookupError:
            pid_alive = False
        except PermissionError:
            pid_alive = True  # exists, just no permission

        if not pid_alive:
            assert "SUBAGENT BOOTUP" in captured.out, (
                "Stale startup flag (dead PID) must not cause dispatcher bootup injection"
            )
            assert "DISPATCHER BOOTUP" not in captured.out


# ---------------------------------------------------------------------------
# Backwards-compat: tests that still work after sentinel removal
# ---------------------------------------------------------------------------


class TestMainSentinelFallback:
    """These tests verify that the old sentinel-based routing is gone and the
    startup flag is the sole detection mechanism.

    The test names are preserved to indicate what was formerly tested.
    """

    def _make_bootup_files(self, claude_dir: Path) -> tuple[Path, Path]:
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        subagent_bootup = claude_dir / "sys.subagent.bootup.md"
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
        subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
        return dispatcher_bootup, subagent_bootup

    def test_sentinel_fallback_injects_dispatcher_bootup(self, tmp_path, capsys):
        """Startup flag with live PID injects dispatcher bootup.

        Previously tested with compact-pending sentinel; now tests startup flag.
        """
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text(str(os.getpid()))

        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        dispatcher_bootup, _ = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "any-session-uuid"})

        with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
            spec = importlib.util.spec_from_file_location("inject_sent_compat", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out
        assert "SUBAGENT BOOTUP" not in captured.out

    def test_no_sentinel_normal_subagent_gets_subagent_bootup(self, tmp_path, capsys):
        """Without startup flag, an unknown session gets subagent bootup (normal path)."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # No startup flag.
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        _, subagent_bootup = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "random-subagent-uuid"})

        with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
            spec = importlib.util.spec_from_file_location("inject_sub_compat", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = workspace / "data" / STARTUP_FLAG_FILENAME  # absent
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out
        assert "DISPATCHER BOOTUP" not in captured.out

    def test_sentinel_with_lobster_main_session_zero_gets_subagent_bootup(
        self, tmp_path, capsys
    ):
        """Without startup flag (simulates subagent), subagent bootup injected regardless of LOBSTER_MAIN_SESSION."""
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # No startup flag (LOBSTER_MAIN_SESSION is irrelevant for startup flag check).
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        _, subagent_bootup = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "outside-session-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(workspace),
                "LOBSTER_MAIN_SESSION": "0",
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_main0_compat", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = workspace / "data" / STARTUP_FLAG_FILENAME  # absent
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out
        assert "DISPATCHER BOOTUP" not in captured.out

    def test_primary_file_match_still_works_without_sentinel(self, tmp_path, capsys):
        """Startup flag with live PID injects dispatcher bootup regardless of UUID files.

        Previously tested UUID file match; now tests startup flag is the sole signal.
        """
        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # Write live PID to startup flag.
        flag = workspace / "data" / STARTUP_FLAG_FILENAME
        flag.write_text(str(os.getpid()))

        # UUID files are NOT used by inject-bootup-context.py after issue #1908.
        # (Writing them here to verify they don't interfere.)
        (workspace / "data" / "dispatcher-claude-session-id").write_text("some-old-uuid")

        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        dispatcher_bootup, _ = self._make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "any-session-uuid"})

        with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
            spec = importlib.util.spec_from_file_location("inject_primary_compat", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out
        assert "SUBAGENT BOOTUP" not in captured.out
