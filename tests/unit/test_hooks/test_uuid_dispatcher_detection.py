"""
Tests for UUID-based dispatcher detection (issue #2071).

When the PID startup flag is absent (e.g. post-compact sessions where the flag
was already consumed on the first start), the hook falls back to comparing
hook_input["session_id"] with the UUID written to
~/lobster-workspace/data/dispatcher-claude-session-id by session_start().

Named constant for the file path:
  DISPATCHER_CLAUDE_SESSION_FILENAME = "dispatcher-claude-session-id"

Behavioral requirements (spec from issue #2071):
  - _is_uuid_match_dispatcher(session_id, uuid_file) returns True when
    session_id matches the UUID in the file (exact match, strip whitespace)
  - _is_uuid_match_dispatcher returns False when file is absent
  - _is_uuid_match_dispatcher returns False when UUIDs don't match
  - _is_uuid_match_dispatcher returns False on OSError
  - _is_uuid_match_dispatcher returns False when session_id is empty string
  - _is_uuid_match_dispatcher returns False when file is empty
  - Integration: main() injects sys.dispatcher.bootup.md when UUID matches
    (no PID flag present)
  - Integration: main() injects sys.subagent.bootup.md when UUID does not match
    (no PID flag present)
  - Integration: main() injects sys.dispatcher.bootup.md when PID flag is live
    (UUID file absent — original path unchanged)
  - Integration: main() injects sys.dispatcher.bootup.md when BOTH PID flag
    and UUID match — no double-injection
  - Integration: UUID detection does NOT consume the dispatcher UUID file
    (it is written once by session_start and should persist across turns)
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"

# Named constant matching the spec — file written by session_start().
DISPATCHER_CLAUDE_SESSION_FILENAME = "dispatcher-claude-session-id"

# Marker strings for verifying which bootup was injected.
DISPATCHER_BOOTUP_MARKER = "DISPATCHER BOOTUP CONTENT"
SUBAGENT_BOOTUP_MARKER = "SUBAGENT BOOTUP CONTENT"

# A realistic-looking UUID for tests.
DISPATCHER_UUID = "aabbccdd-1234-5678-abcd-000000000001"
OTHER_UUID = "11111111-0000-0000-0000-000000000002"


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


def _load_hook(workspace: Path) -> object:
    """Load inject-bootup-context.py module with an isolated LOBSTER_WORKSPACE."""
    import uuid

    unique_name = f"inject_uuid_test_{uuid.uuid4().hex}"
    with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _make_bootup_files(tmp_path: Path) -> tuple[Path, Path]:
    """Write minimal bootup stubs with distinct markers into a fake .claude dir."""
    claude_dir = tmp_path / "lobster" / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
    subagent_bootup = claude_dir / "sys.subagent.bootup.md"
    dispatcher_bootup.write_text(f"# {DISPATCHER_BOOTUP_MARKER}\n")
    subagent_bootup.write_text(f"# {SUBAGENT_BOOTUP_MARKER}\n")
    return dispatcher_bootup, subagent_bootup


def _write_uuid_file(data_dir: Path, uuid_value: str) -> Path:
    """Write a dispatcher-claude-session-id file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    uuid_file = data_dir / DISPATCHER_CLAUDE_SESSION_FILENAME
    uuid_file.write_text(uuid_value)
    return uuid_file


def _run_hook_no_pid_flag(
    tmp_path: Path,
    session_id: str,
    uuid_file_content: str | None = None,
) -> tuple[str, str]:
    """Run the hook with no PID startup flag present.

    Optionally writes a UUID file with the given content.
    Returns (stdout, stderr) strings.
    """
    workspace = tmp_path / "workspace"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "logs").mkdir(parents=True, exist_ok=True)

    uuid_file = data_dir / DISPATCHER_CLAUDE_SESSION_FILENAME
    if uuid_file_content is not None:
        uuid_file.write_text(uuid_file_content)

    dispatcher_bootup, subagent_bootup = _make_bootup_files(tmp_path)

    # PID flag is absent — no file created at startup_flag path.
    absent_flag = data_dir / "dispatcher-startup-flag"
    # Make sure it really does not exist.
    assert not absent_flag.exists()

    hook_input = json.dumps({"session_id": session_id})

    with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
        mod = _load_hook(workspace)

        mod.STARTUP_FLAG_FILE = absent_flag  # absent
        mod.DISPATCHER_BOOTUP = dispatcher_bootup
        mod.SUBAGENT_BOOTUP = subagent_bootup
        mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
        mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
        mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
        mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"
        mod.DISPATCHER_CLAUDE_SESSION_FILE = uuid_file

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with patch("sys.stdin", io.StringIO(hook_input)):
            with patch("sys.stdout", stdout_buf):
                with patch("sys.stderr", stderr_buf):
                    try:
                        mod.main()
                    except SystemExit:
                        pass

    return stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Unit tests for _is_uuid_match_dispatcher()
# ---------------------------------------------------------------------------


class TestIsUuidMatchDispatcher:
    """Pure function tests for _is_uuid_match_dispatcher(session_id, uuid_file)."""

    def test_returns_true_when_uuid_matches(self, tmp_path):
        """Exact UUID match → True."""
        data_dir = tmp_path / "data"
        uuid_file = _write_uuid_file(data_dir, DISPATCHER_UUID)

        mod = _load_hook(tmp_path)
        result = mod._is_uuid_match_dispatcher(DISPATCHER_UUID, uuid_file)

        assert result is True

    def test_returns_true_when_uuid_matches_with_trailing_newline(self, tmp_path):
        """UUID file with trailing newline → still matches after strip."""
        data_dir = tmp_path / "data"
        uuid_file = _write_uuid_file(data_dir, DISPATCHER_UUID + "\n")

        mod = _load_hook(tmp_path)
        result = mod._is_uuid_match_dispatcher(DISPATCHER_UUID, uuid_file)

        assert result is True

    def test_returns_false_when_uuid_file_absent(self, tmp_path):
        """UUID file absent → False (safe default)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        absent_file = data_dir / DISPATCHER_CLAUDE_SESSION_FILENAME

        mod = _load_hook(tmp_path)
        result = mod._is_uuid_match_dispatcher(DISPATCHER_UUID, absent_file)

        assert result is False

    def test_returns_false_when_uuid_does_not_match(self, tmp_path):
        """Different UUID in file → False."""
        data_dir = tmp_path / "data"
        uuid_file = _write_uuid_file(data_dir, OTHER_UUID)

        mod = _load_hook(tmp_path)
        result = mod._is_uuid_match_dispatcher(DISPATCHER_UUID, uuid_file)

        assert result is False

    def test_returns_false_when_session_id_empty(self, tmp_path):
        """Empty session_id → False (cannot match anything meaningfully)."""
        data_dir = tmp_path / "data"
        uuid_file = _write_uuid_file(data_dir, DISPATCHER_UUID)

        mod = _load_hook(tmp_path)
        result = mod._is_uuid_match_dispatcher("", uuid_file)

        assert result is False

    def test_returns_false_when_file_is_empty(self, tmp_path):
        """UUID file exists but is empty → False."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        uuid_file = data_dir / DISPATCHER_CLAUDE_SESSION_FILENAME
        uuid_file.write_text("")

        mod = _load_hook(tmp_path)
        result = mod._is_uuid_match_dispatcher(DISPATCHER_UUID, uuid_file)

        assert result is False

    def test_returns_false_on_oserror(self, tmp_path):
        """OSError reading file → False (conservative default)."""
        mod = _load_hook(tmp_path)

        mock_file = MagicMock(spec=Path)
        mock_file.exists.return_value = True
        mock_file.read_text.side_effect = OSError("permission denied")

        result = mod._is_uuid_match_dispatcher(DISPATCHER_UUID, mock_file)

        assert result is False

    def test_returns_false_when_session_id_is_unknown(self, tmp_path):
        """session_id='unknown' (fallback value when stdin fails) → False."""
        data_dir = tmp_path / "data"
        # Even if the file contains "unknown", it must not match
        uuid_file = _write_uuid_file(data_dir, "unknown")

        mod = _load_hook(tmp_path)
        result = mod._is_uuid_match_dispatcher("unknown", uuid_file)

        # "unknown" is the fallback sentinel — must never be treated as a real UUID
        assert result is False


# ---------------------------------------------------------------------------
# Integration tests: main() routing via UUID when PID flag is absent
# ---------------------------------------------------------------------------


class TestUuidDetectionIntegration:
    """main() injects dispatcher bootup when UUID matches, subagent when it doesn't."""

    def test_dispatcher_bootup_injected_when_uuid_matches(self, tmp_path):
        """UUID match → sys.dispatcher.bootup.md injected (no PID flag)."""
        stdout, _stderr = _run_hook_no_pid_flag(
            tmp_path,
            session_id=DISPATCHER_UUID,
            uuid_file_content=DISPATCHER_UUID,
        )

        assert DISPATCHER_BOOTUP_MARKER in stdout, (
            f"sys.dispatcher.bootup.md must be injected when UUID matches. "
            f"Got stdout:\n{stdout[:500]}"
        )
        assert SUBAGENT_BOOTUP_MARKER not in stdout, (
            "sys.subagent.bootup.md must NOT appear when UUID matches dispatcher"
        )

    def test_subagent_bootup_injected_when_uuid_absent(self, tmp_path):
        """UUID file absent → subagent path (no PID flag, no UUID file)."""
        stdout, _stderr = _run_hook_no_pid_flag(
            tmp_path,
            session_id=DISPATCHER_UUID,
            uuid_file_content=None,  # file not created
        )

        assert SUBAGENT_BOOTUP_MARKER in stdout, (
            "sys.subagent.bootup.md must be injected when UUID file is absent"
        )
        assert DISPATCHER_BOOTUP_MARKER not in stdout

    def test_subagent_bootup_injected_when_uuid_mismatches(self, tmp_path):
        """UUID file present but doesn't match session_id → subagent path."""
        stdout, _stderr = _run_hook_no_pid_flag(
            tmp_path,
            session_id=OTHER_UUID,
            uuid_file_content=DISPATCHER_UUID,  # different UUID in file
        )

        assert SUBAGENT_BOOTUP_MARKER in stdout, (
            "sys.subagent.bootup.md must be injected when UUID does not match"
        )
        assert DISPATCHER_BOOTUP_MARKER not in stdout

    def test_uuid_file_not_deleted_after_detection(self, tmp_path):
        """UUID file must persist after detection — session_start wrote it once."""
        workspace = tmp_path / "workspace"
        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        uuid_file = _write_uuid_file(data_dir, DISPATCHER_UUID)
        assert uuid_file.exists()

        dispatcher_bootup, subagent_bootup = _make_bootup_files(tmp_path)
        absent_flag = data_dir / "dispatcher-startup-flag"
        hook_input = json.dumps({"session_id": DISPATCHER_UUID})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
            mod = _load_hook(workspace)
            mod.STARTUP_FLAG_FILE = absent_flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"
            mod.DISPATCHER_CLAUDE_SESSION_FILE = uuid_file

            with patch("sys.stdin", io.StringIO(hook_input)):
                with patch("sys.stdout", io.StringIO()):
                    with patch("sys.stderr", io.StringIO()):
                        try:
                            mod.main()
                        except SystemExit:
                            pass

        # UUID file must still exist — it is not consumed by detection.
        assert uuid_file.exists(), (
            "dispatcher-claude-session-id must NOT be deleted after UUID-based detection"
        )
        assert uuid_file.read_text().strip() == DISPATCHER_UUID


class TestPidFlagStillWorks:
    """PID-based detection path is unchanged by the UUID addition."""

    def test_pid_flag_still_triggers_dispatcher_bootup(self, tmp_path):
        """Live PID flag → dispatcher bootup injected (UUID file absent)."""
        workspace = tmp_path / "workspace"
        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # Write live PID to startup flag.
        flag = data_dir / "dispatcher-startup-flag"
        flag.write_text(str(os.getpid()))

        # No UUID file.
        uuid_file = data_dir / DISPATCHER_CLAUDE_SESSION_FILENAME
        assert not uuid_file.exists()

        dispatcher_bootup, subagent_bootup = _make_bootup_files(tmp_path)
        hook_input = json.dumps({"session_id": "any-session-uuid"})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
            mod = _load_hook(workspace)
            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"
            mod.DISPATCHER_CLAUDE_SESSION_FILE = uuid_file  # absent

            stdout_buf = io.StringIO()
            with patch("sys.stdin", io.StringIO(hook_input)):
                with patch("sys.stdout", stdout_buf):
                    with patch("sys.stderr", io.StringIO()):
                        try:
                            mod.main()
                        except SystemExit:
                            pass

        stdout = stdout_buf.getvalue()
        assert DISPATCHER_BOOTUP_MARKER in stdout, (
            "PID flag path must still inject sys.dispatcher.bootup.md"
        )
        assert SUBAGENT_BOOTUP_MARKER not in stdout

    def test_both_pid_and_uuid_match_injects_dispatcher_bootup_once(self, tmp_path):
        """PID flag live AND UUID matches → dispatcher bootup injected exactly once."""
        workspace = tmp_path / "workspace"
        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        # Write live PID to startup flag.
        flag = data_dir / "dispatcher-startup-flag"
        flag.write_text(str(os.getpid()))

        # Also write matching UUID file.
        uuid_file = _write_uuid_file(data_dir, DISPATCHER_UUID)

        dispatcher_bootup, subagent_bootup = _make_bootup_files(tmp_path)
        hook_input = json.dumps({"session_id": DISPATCHER_UUID})

        with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
            mod = _load_hook(workspace)
            mod.STARTUP_FLAG_FILE = flag
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"
            mod.CONTEXT_INJECTION_LOG = workspace / "logs" / "context-injection.log"
            mod.DISPATCHER_CLAUDE_SESSION_FILE = uuid_file

            stdout_buf = io.StringIO()
            with patch("sys.stdin", io.StringIO(hook_input)):
                with patch("sys.stdout", stdout_buf):
                    with patch("sys.stderr", io.StringIO()):
                        try:
                            mod.main()
                        except SystemExit:
                            pass

        stdout = stdout_buf.getvalue()
        assert DISPATCHER_BOOTUP_MARKER in stdout
        assert SUBAGENT_BOOTUP_MARKER not in stdout
        # Verify no double-injection — marker appears exactly once.
        assert stdout.count(DISPATCHER_BOOTUP_MARKER) == 1, (
            f"Dispatcher bootup must be injected exactly once. "
            f"Found {stdout.count(DISPATCHER_BOOTUP_MARKER)} occurrences."
        )
