"""
Unit tests for context injection logging in inject-bootup-context.py.

Issue #1889: add append-only logging so we can debug whether dispatchers
received the correct bootup file.

Every hook run must append exactly one line to the log file:
  <ISO UTC timestamp> | session=<id> | role=<dispatcher|subagent> | injected=[file1, file2, ...]

Tests verify:
- Log file is created when absent
- One line is written per hook run
- Line format matches the spec
- Only actually-injected files (exist + non-empty) appear in injected list
- Role field is "dispatcher" or "subagent" depending on session type
- session field is "unknown" when hook_input has no session_id
- Existing log content is preserved (append-only)
- No stdout pollution — injected content unchanged
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
# Helpers (shared with other inject-bootup-context test files)
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


def _make_bootup_files(claude_dir: Path) -> tuple[Path, Path]:
    """Write minimal dispatcher and subagent bootup stubs."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
    subagent_bootup = claude_dir / "sys.subagent.bootup.md"
    dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
    subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
    return dispatcher_bootup, subagent_bootup


def _run_hook(
    tmp_path: Path,
    *,
    hook_input: dict,
    dispatcher_uuid: str | None = None,
    is_dispatcher_session: bool = False,
    log_path: Path | None = None,
) -> tuple[object, Path]:
    """Load and run inject-bootup-context.py in a controlled environment.

    Returns (module, log_path).

    Sets up minimal file structure: bootup files, primary state file if
    dispatcher_uuid is given. Suppresses user config files.

    The primary state file is written to $LOBSTER_WORKSPACE/data/
    because session_role._get_mcp_claude_session_file() reads LOBSTER_WORKSPACE
    from the environment at call time.
    """
    import uuid as _uuid

    claude_dir = tmp_path / "lobster" / ".claude"
    dispatcher_bootup, subagent_bootup = _make_bootup_files(claude_dir)

    lobster_workspace = tmp_path / "lobster-workspace"
    lobster_workspace.mkdir(parents=True, exist_ok=True)
    (lobster_workspace / "logs").mkdir(parents=True, exist_ok=True)

    # Startup flag and data dir must live under LOBSTER_WORKSPACE/data/
    # (inject-bootup-context.py resolves STARTUP_FLAG_FILE relative to LOBSTER_WORKSPACE)
    ws_data_dir = lobster_workspace / "data"
    ws_data_dir.mkdir(parents=True, exist_ok=True)
    if dispatcher_uuid:
        # Legacy: write UUID file for compatibility with tests that don't use startup flag.
        (ws_data_dir / "dispatcher-claude-session-id").write_text(dispatcher_uuid)

    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    if log_path is None:
        log_path = lobster_workspace / "logs" / "context-injection.log"

    unique_name = f"inject_log_test_{_uuid.uuid4().hex}"

    with _PatchEnv(
        {
            "HOME": str(tmp_path),
            "LOBSTER_WORKSPACE": str(lobster_workspace),
            "LOBSTER_MAIN_SESSION": "1" if is_dispatcher_session else "0",
        }
    ):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        mod.DISPATCHER_BOOTUP = dispatcher_bootup
        mod.SUBAGENT_BOOTUP = subagent_bootup
        mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
        mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
        mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
        mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
        mod.CONTEXT_INJECTION_LOG = log_path

        # Issue #1908: startup flag is now the sole dispatcher detection mechanism.
        # Override STARTUP_FLAG_FILE and write current PID to simulate dispatcher.
        if is_dispatcher_session:
            startup_flag = ws_data_dir / "dispatcher-startup-flag"
            startup_flag.write_text(str(os.getpid()))
            mod.STARTUP_FLAG_FILE = startup_flag

        stdin_data = json.dumps(hook_input)
        with patch("sys.stdin", io.StringIO(stdin_data)):
            with pytest.raises(SystemExit):
                mod.main()

    return mod, log_path


# ---------------------------------------------------------------------------
# Tests: log file creation and line format
# ---------------------------------------------------------------------------


class TestLogFileCreation:
    """Log file is created on first run and lines are appended on subsequent runs."""

    def test_log_file_created_if_absent(self, tmp_path, capsys):
        """Hook creates the log file when it does not exist yet."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"
        assert not log_path.exists()

        _run_hook(
            tmp_path,
            hook_input={"session_id": "test-session-abc"},
            log_path=log_path,
        )

        assert log_path.exists(), "Log file should be created if absent"

    def test_each_run_appends_exactly_one_line(self, tmp_path, capsys):
        """Each hook invocation appends exactly one line to the log."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        for i in range(3):
            _run_hook(
                tmp_path,
                hook_input={"session_id": f"session-{i}"},
                log_path=log_path,
            )

        lines = log_path.read_text().splitlines()
        assert len(lines) == 3, f"Expected 3 log lines, got {len(lines)}"

    def test_existing_log_content_preserved(self, tmp_path, capsys):
        """Pre-existing log lines are not overwritten (append-only)."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"
        log_path.write_text("pre-existing line\n")

        _run_hook(
            tmp_path,
            hook_input={"session_id": "new-session"},
            log_path=log_path,
        )

        lines = log_path.read_text().splitlines()
        assert lines[0] == "pre-existing line", "Pre-existing content must be preserved"
        assert len(lines) == 2, "Should have original line + new line"


# ---------------------------------------------------------------------------
# Tests: log line format
# ---------------------------------------------------------------------------


class TestLogLineFormat:
    """The format of each logged line matches the spec."""

    def test_log_line_has_four_pipe_delimited_fields(self, tmp_path, capsys):
        """Each log line has exactly 4 pipe-delimited fields."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        _run_hook(
            tmp_path,
            hook_input={"session_id": "abc-123"},
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        fields = [f.strip() for f in line.split("|")]
        assert len(fields) == 4, f"Expected 4 pipe-separated fields, got: {line!r}"

    def test_session_field_contains_session_id(self, tmp_path, capsys):
        """session= field contains the session_id from hook_input."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"
        session_id = "my-unique-session-id-xyz"

        _run_hook(
            tmp_path,
            hook_input={"session_id": session_id},
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        assert f"session={session_id}" in line, (
            f"Expected 'session={session_id}' in log line, got: {line!r}"
        )

    def test_session_field_is_unknown_when_no_session_id(self, tmp_path, capsys):
        """session=unknown when hook_input has no session_id key."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        _run_hook(
            tmp_path,
            hook_input={},  # no session_id
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        assert "session=unknown" in line, (
            f"Expected 'session=unknown' when no session_id, got: {line!r}"
        )

    def test_timestamp_field_is_iso_utc(self, tmp_path, capsys):
        """First field is an ISO 8601 UTC timestamp (contains 'T' and 'Z')."""
        import re

        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        _run_hook(
            tmp_path,
            hook_input={"session_id": "ts-test"},
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        timestamp = line.split("|")[0].strip()
        # Must match ISO 8601 UTC pattern: YYYY-MM-DDTHH:MM:SS...Z
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", timestamp), (
            f"Expected ISO 8601 timestamp, got: {timestamp!r}"
        )


# ---------------------------------------------------------------------------
# Tests: role field
# ---------------------------------------------------------------------------


class TestRoleField:
    """role= field reflects the detected session role."""

    def test_role_is_dispatcher_for_dispatcher_session(self, tmp_path, capsys):
        """role=dispatcher when the session is identified as the dispatcher."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        dispatcher_uuid = "dispatcher-uuid-for-role-test"
        _run_hook(
            tmp_path,
            hook_input={"session_id": dispatcher_uuid},
            dispatcher_uuid=dispatcher_uuid,  # matches → is_dispatcher() returns True
            is_dispatcher_session=True,
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        assert "role=dispatcher" in line, (
            f"Expected 'role=dispatcher' for dispatcher session, got: {line!r}"
        )

    def test_role_is_subagent_for_non_dispatcher_session(self, tmp_path, capsys):
        """role=subagent when no startup flag is present (session is a subagent).

        Issue #1908: dispatcher detection uses PID-based startup flag only.
        Subagents are sessions that were NOT launched via claude-persistent.sh,
        so no startup flag is present.
        """
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        # No startup flag written — this session is a subagent.
        _run_hook(
            tmp_path,
            hook_input={"session_id": "subagent-uuid-different"},
            is_dispatcher_session=False,
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        assert "role=subagent" in line, (
            f"Expected 'role=subagent' for subagent session (no startup flag), got: {line!r}"
        )


# ---------------------------------------------------------------------------
# Tests: injected files list
# ---------------------------------------------------------------------------


class TestInjectedFilesList:
    """injected= field lists only files that exist and were actually read."""

    def test_injected_contains_system_bootup_file_name(self, tmp_path, capsys):
        """injected= includes the system bootup file name for subagent sessions."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        # Subagent session — no startup flag written (issue #1908: flag is sole detection)
        _run_hook(
            tmp_path,
            hook_input={"session_id": "subagent-session-id"},
            is_dispatcher_session=False,
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        assert "sys.subagent.bootup.md" in line, (
            f"Expected 'sys.subagent.bootup.md' in injected list, got: {line!r}"
        )

    def test_injected_contains_dispatcher_bootup_for_dispatcher_session(
        self, tmp_path, capsys
    ):
        """injected= includes sys.dispatcher.bootup.md for dispatcher sessions."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        dispatcher_uuid = "disp-uuid-log-test"
        _run_hook(
            tmp_path,
            hook_input={"session_id": dispatcher_uuid},
            dispatcher_uuid=dispatcher_uuid,
            is_dispatcher_session=True,
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        assert "sys.dispatcher.bootup.md" in line, (
            f"Expected 'sys.dispatcher.bootup.md' in injected list, got: {line!r}"
        )

    def test_injected_is_empty_list_when_no_files_injected(self, tmp_path, capsys):
        """injected=[] when the system bootup file does not exist."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        import uuid as _uuid
        unique_name = f"inject_log_empty_{_uuid.uuid4().hex}"
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        lobster_workspace = tmp_path / "lobster-workspace"
        (lobster_workspace / "logs").mkdir(parents=True, exist_ok=True)
        (lobster_workspace / "data").mkdir(parents=True, exist_ok=True)

        # Bootup files do NOT exist
        absent_dispatcher = claude_dir / "sys.dispatcher.bootup.md"
        absent_subagent = claude_dir / "sys.subagent.bootup.md"

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(lobster_workspace),
                "LOBSTER_MAIN_SESSION": "0",
            }
        ):
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.DISPATCHER_BOOTUP = absent_dispatcher
            mod.SUBAGENT_BOOTUP = absent_subagent
            mod.COMPACT_PENDING_SENTINEL = tmp_path / "no-sentinel"
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = log_path

            stdin_data = json.dumps({"session_id": "session-with-no-files"})
            with patch("sys.stdin", io.StringIO(stdin_data)):
                with pytest.raises(SystemExit):
                    mod.main()

        line = log_path.read_text().strip()
        assert "injected=[]" in line, (
            f"Expected 'injected=[]' when no bootup files exist, got: {line!r}"
        )

    def test_optional_user_bootup_file_included_when_exists(self, tmp_path, capsys):
        """injected= includes user base bootup file when it exists."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        import uuid as _uuid
        unique_name = f"inject_log_user_{_uuid.uuid4().hex}"
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        subagent_bootup = claude_dir / "sys.subagent.bootup.md"
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
        subagent_bootup.write_text("# SUBAGENT BOOTUP\n")

        # Write user base bootup file
        user_config_dir = tmp_path / "user-config" / "agents"
        user_config_dir.mkdir(parents=True, exist_ok=True)
        user_base = user_config_dir / "user.base.bootup.md"
        user_base.write_text("# USER BASE BOOTUP\n")

        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        lobster_workspace = tmp_path / "lobster-workspace"
        (lobster_workspace / "logs").mkdir(parents=True, exist_ok=True)
        (lobster_workspace / "data").mkdir(parents=True, exist_ok=True)

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(lobster_workspace),
                "LOBSTER_MAIN_SESSION": "0",
            }
        ):
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.COMPACT_PENDING_SENTINEL = tmp_path / "no-sentinel"
            mod.USER_CONFIG_DIR = user_config_dir
            mod.USER_BASE_BOOTUP = user_base
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = log_path

            stdin_data = json.dumps({"session_id": "subagent-with-user-config"})
            with patch("sys.stdin", io.StringIO(stdin_data)):
                with pytest.raises(SystemExit):
                    mod.main()

        line = log_path.read_text().strip()
        assert "user.base.bootup.md" in line, (
            f"Expected 'user.base.bootup.md' in injected list when file exists, got: {line!r}"
        )

    def test_optional_user_bootup_file_excluded_when_absent(self, tmp_path, capsys):
        """injected= does NOT include user base bootup file when it does not exist."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        _run_hook(
            tmp_path,
            hook_input={"session_id": "no-user-config-session"},
            log_path=log_path,
        )

        line = log_path.read_text().strip()
        assert "user.base.bootup.md" not in line, (
            f"user.base.bootup.md must not appear in log when file is absent, got: {line!r}"
        )


# ---------------------------------------------------------------------------
# Tests: stdout is unchanged (no logging pollution)
# ---------------------------------------------------------------------------


class TestStdoutUnchanged:
    """Adding a log line must not alter stdout output (bootup content unchanged)."""

    def test_dispatcher_stdout_unchanged_by_logging(self, tmp_path, capsys):
        """Adding log does not change what the dispatcher session sees in stdout."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        dispatcher_uuid = "stdout-test-dispatcher-uuid"
        _run_hook(
            tmp_path,
            hook_input={"session_id": dispatcher_uuid},
            dispatcher_uuid=dispatcher_uuid,
            is_dispatcher_session=True,
            log_path=log_path,
        )

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out
        assert "context-injection.log" not in captured.out
        assert "injected=" not in captured.out
        assert "session=" not in captured.out

    def test_subagent_stdout_unchanged_by_logging(self, tmp_path, capsys):
        """Adding log does not change what a subagent session sees in stdout."""
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "context-injection.log"

        # Subagent session — no startup flag (issue #1908: flag is sole detection)
        _run_hook(
            tmp_path,
            hook_input={"session_id": "subagent-stdout-test"},
            is_dispatcher_session=False,
            log_path=log_path,
        )

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out
        assert "context-injection.log" not in captured.out
        assert "injected=" not in captured.out
