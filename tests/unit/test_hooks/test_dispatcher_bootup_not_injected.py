"""
Tests for issue #1994: dispatcher bootup body not injected into context.

sys.dispatcher.bootup.md is 84KB / ~1400 lines. The SessionStart hook was
injecting the full file into context, but the rendering layer truncated the
preview to ~2KB — making the injection useless. The dispatcher re-reads the
full file anyway via an explicit Read call at startup.

Fix C (remove injection): the hook still injects:
  - ADMIN_CHAT_ID preamble (small, fits in 2KB preview, genuinely useful)
  - startup_cause banner (small, fits in 2KB preview, genuinely useful)
  - user.base.bootup.md (if exists)
  - user.dispatcher.bootup.md (if exists)

It does NOT inject the body of sys.dispatcher.bootup.md. The dispatcher reads
the file explicitly at startup via Read('.claude/sys.dispatcher.bootup.md').

Subagent sessions are unchanged: sys.subagent.bootup.md is 28KB (smaller, but
still injected for now — it serves a different role and the model doesn't
re-read it explicitly).

Named constants match the spec (issue #1994):
  DISPATCHER_BOOTUP_FILENAME — the filename that must NOT appear in stdout for
                                dispatcher sessions after Fix C.
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


# Named constant matching the spec requirement (issue #1994).
# The body of this file must NOT appear in dispatcher session stdout after Fix C.
DISPATCHER_BOOTUP_FILENAME = "sys.dispatcher.bootup.md"

# Unique marker in the test stub bootup file, used to verify body presence/absence.
DISPATCHER_BOOTUP_BODY_MARKER = "DISPATCHER BOOTUP BODY SENTINEL"

# Startup-cause banner marker: must appear in dispatcher stdout (Fix C keeps it).
STARTUP_CAUSE_BANNER_MARKER = "startup-cause:"

# ADMIN_CHAT_ID preamble marker: must appear in dispatcher stdout when config provides it.
ADMIN_CHAT_ID_MARKER = "ADMIN_CHAT_ID="


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


def _make_bootup_files(claude_dir: Path) -> tuple[Path, Path]:
    """Write minimal dispatcher and subagent bootup stubs with distinct markers."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    dispatcher_bootup = claude_dir / DISPATCHER_BOOTUP_FILENAME
    subagent_bootup = claude_dir / "sys.subagent.bootup.md"
    dispatcher_bootup.write_text(f"# {DISPATCHER_BOOTUP_BODY_MARKER}\n")
    subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
    return dispatcher_bootup, subagent_bootup


def _run_hook_as_dispatcher(
    tmp_path: Path,
    *,
    session_id: str = "test-dispatcher-session",
    config_env_content: str | None = None,
    startup_cause_content: str | None = None,
) -> "CaptureResult":
    """Run the hook as a dispatcher session and return captured output.

    Sets up minimal file structure and writes a live PID to the startup flag.
    Returns a CaptureResult object with .out (stdout) and .err (stderr).
    """
    import uuid as _uuid

    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True, exist_ok=True)
    (workspace / "logs").mkdir(parents=True, exist_ok=True)

    # Write live PID to startup flag — marks this as a dispatcher session.
    flag = workspace / "data" / "dispatcher-startup-flag"
    flag.write_text(str(os.getpid()))

    claude_dir = tmp_path / "lobster" / ".claude"
    dispatcher_bootup, _ = _make_bootup_files(claude_dir)

    # Write config.env if requested (for ADMIN_CHAT_ID injection).
    config_env_path = tmp_path / "lobster-config" / "config.env"
    if config_env_content is not None:
        config_env_path.parent.mkdir(parents=True, exist_ok=True)
        config_env_path.write_text(config_env_content)

    # Write startup cause file if requested.
    startup_cause_path = workspace / "data" / "last-startup-cause.json"
    if startup_cause_content is not None:
        startup_cause_path.write_text(startup_cause_content)

    hook_input = json.dumps({"session_id": session_id})
    unique_name = f"inject_no_body_test_{_uuid.uuid4().hex}"

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        mod.STARTUP_FLAG_FILE = flag
        mod.DISPATCHER_BOOTUP = dispatcher_bootup
        mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
        mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
        mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
        mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
        mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"
        mod.CONFIG_ENV_PATH = config_env_path

        # Override startup cause file path.
        mod.STARTUP_CAUSE_FILE = startup_cause_path

        with patch("sys.stdin", io.StringIO(hook_input)):
            with patch("sys.stdout", io.StringIO()) as mock_stdout:
                with patch("sys.stderr", io.StringIO()) as mock_stderr:
                    try:
                        mod.main()
                    except SystemExit:
                        pass
                    stdout_lines = mock_stdout.getvalue()
                    stderr_lines = mock_stderr.getvalue()

    class CaptureResult:
        def __init__(self, out: str, err: str):
            self.out = out
            self.err = err

    return CaptureResult(stdout_lines, stderr_lines)


class TestDispatcherBootupBodyNotInjected:
    """Fix C: sys.dispatcher.bootup.md body must NOT appear in dispatcher stdout.

    The dispatcher reads the file explicitly at startup via Read(), so injecting
    it into context wastes ~21K tokens with zero benefit.
    """

    def test_dispatcher_bootup_body_absent_from_dispatcher_stdout(self, tmp_path):
        """After Fix C, the dispatcher bootup body must NOT appear in dispatcher stdout."""
        result = _run_hook_as_dispatcher(tmp_path)
        assert DISPATCHER_BOOTUP_BODY_MARKER not in result.out, (
            f"Dispatcher bootup body must NOT be injected into dispatcher context. "
            f"Found marker '{DISPATCHER_BOOTUP_BODY_MARKER}' in stdout:\n{result.out[:500]}"
        )

    def test_startup_cause_banner_still_present_for_dispatcher(self, tmp_path):
        """startup_cause banner must still appear in dispatcher stdout (genuinely useful preamble)."""
        result = _run_hook_as_dispatcher(tmp_path)
        assert STARTUP_CAUSE_BANNER_MARKER in result.out, (
            f"Startup-cause banner must still be injected for dispatcher sessions. "
            f"Expected '{STARTUP_CAUSE_BANNER_MARKER}' in stdout:\n{result.out[:500]}"
        )

    def test_admin_chat_id_still_present_when_config_provides_it(self, tmp_path):
        """ADMIN_CHAT_ID preamble must still appear when config.env has the value."""
        config_env = "LOBSTER_ADMIN_CHAT_ID=12345678\n"
        result = _run_hook_as_dispatcher(tmp_path, config_env_content=config_env)
        assert ADMIN_CHAT_ID_MARKER in result.out, (
            f"ADMIN_CHAT_ID preamble must still be injected when config.env provides it. "
            f"Expected '{ADMIN_CHAT_ID_MARKER}' in stdout:\n{result.out[:500]}"
        )
        assert "12345678" in result.out, (
            f"ADMIN_CHAT_ID value must appear in stdout. Got:\n{result.out[:500]}"
        )

    def test_admin_chat_id_absent_when_config_missing(self, tmp_path):
        """ADMIN_CHAT_ID preamble must be silently absent when config.env is missing."""
        # No config_env_content provided — file will not exist.
        result = _run_hook_as_dispatcher(tmp_path)
        assert ADMIN_CHAT_ID_MARKER not in result.out, (
            f"ADMIN_CHAT_ID must not appear in stdout when config.env is absent. "
            f"Got:\n{result.out[:500]}"
        )

    def test_startup_cause_is_compaction_when_cause_file_says_so(self, tmp_path):
        """When startup cause file says 'compaction', banner reflects that value."""
        import time

        now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cause_content = json.dumps({"cause": "compaction", "ts": now_utc})
        result = _run_hook_as_dispatcher(
            tmp_path, startup_cause_content=cause_content
        )
        assert "compaction" in result.out, (
            f"startup-cause banner must say 'compaction' when cause file has cause=compaction. "
            f"Got stdout:\n{result.out[:500]}"
        )

    def test_startup_cause_defaults_to_restart_when_cause_file_absent(self, tmp_path):
        """When startup cause file is absent, banner defaults to 'restart'."""
        result = _run_hook_as_dispatcher(tmp_path)
        assert "restart" in result.out, (
            f"startup-cause banner must say 'restart' when cause file is absent. "
            f"Got stdout:\n{result.out[:500]}"
        )

    def test_hook_exits_cleanly_without_bootup_body(self, tmp_path):
        """Hook must complete without error even when bootup body is not injected."""
        # This test verifies no exception is raised and exit code 0 is used.
        import uuid as _uuid

        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / "dispatcher-startup-flag"
        flag.write_text(str(os.getpid()))

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _ = _make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "exit-test-dispatcher"})
        unique_name = f"inject_exit_test_{_uuid.uuid4().hex}"

        with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
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
                with pytest.raises(SystemExit) as exc_info:
                    mod.main()

        assert exc_info.value.code == 0, (
            f"Hook must exit with code 0, got: {exc_info.value.code}"
        )


class TestSubagentBootupUnchanged:
    """Subagent sessions are unaffected by Fix C — sys.subagent.bootup.md still injected."""

    def test_subagent_bootup_body_present_in_subagent_stdout(self, tmp_path, capsys):
        """sys.subagent.bootup.md body must still appear in subagent stdout."""
        import uuid as _uuid

        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # No startup flag — this is a subagent session.
        claude_dir = tmp_path / "lobster" / ".claude"
        _, subagent_bootup = _make_bootup_files(claude_dir)

        hook_input = json.dumps({"session_id": "subagent-unchanged-test"})
        unique_name = f"inject_subagent_test_{_uuid.uuid4().hex}"

        with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = workspace / "data" / "dispatcher-startup-flag"  # absent
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
            "Subagent bootup body must still be injected for subagent sessions"
        )
        # Dispatcher bootup must NOT appear in subagent stdout.
        assert DISPATCHER_BOOTUP_BODY_MARKER not in captured.out


class TestDispatcherUserBootupFilesStillInjected:
    """User bootup files must still be injected for dispatcher sessions after Fix C."""

    def test_user_base_bootup_injected_for_dispatcher(self, tmp_path, capsys):
        """user.base.bootup.md must still be injected for dispatcher sessions."""
        import uuid as _uuid

        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / "dispatcher-startup-flag"
        flag.write_text(str(os.getpid()))

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _ = _make_bootup_files(claude_dir)

        # Write a user base bootup file.
        user_config_dir = tmp_path / "user-config" / "agents"
        user_config_dir.mkdir(parents=True, exist_ok=True)
        user_base = user_config_dir / "user.base.bootup.md"
        user_base.write_text("# USER BASE BOOTUP CONTENT\n")

        hook_input = json.dumps({"session_id": "dispatcher-user-bootup-test"})
        unique_name = f"inject_user_base_test_{_uuid.uuid4().hex}"

        with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.USER_BASE_BOOTUP = user_base
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "USER BASE BOOTUP CONTENT" in captured.out, (
            "user.base.bootup.md must still be injected for dispatcher sessions after Fix C"
        )

    def test_user_dispatcher_bootup_injected_for_dispatcher(self, tmp_path, capsys):
        """user.dispatcher.bootup.md must still be injected for dispatcher sessions."""
        import uuid as _uuid

        workspace = tmp_path / "workspace"
        (workspace / "data").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        flag = workspace / "data" / "dispatcher-startup-flag"
        flag.write_text(str(os.getpid()))

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _ = _make_bootup_files(claude_dir)

        # Write a user dispatcher bootup file.
        user_config_dir = tmp_path / "user-config" / "agents"
        user_config_dir.mkdir(parents=True, exist_ok=True)
        user_dispatcher = user_config_dir / "user.dispatcher.bootup.md"
        user_dispatcher.write_text("# USER DISPATCHER BOOTUP CONTENT\n")

        hook_input = json.dumps({"session_id": "dispatcher-user-dispatcher-test"})
        unique_name = f"inject_user_disp_test_{_uuid.uuid4().hex}"

        with _PatchEnv({"HOME": str(tmp_path), "LOBSTER_WORKSPACE": str(workspace)}):
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = user_dispatcher
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "USER DISPATCHER BOOTUP CONTENT" in captured.out, (
            "user.dispatcher.bootup.md must still be injected for dispatcher sessions after Fix C"
        )
