"""
Unit tests for _write_dispatcher_session_start() in inject-bootup-context.py.

Issue #2059: CC enforces a hard 7440s session lifetime. inject-bootup-context.py
now writes a plain Unix epoch timestamp to dispatcher-session-start.ts on every
dispatcher SessionStart so health-check-v3.sh can compute session age and send
SIGTERM before the hard limit.

Tests cover:
- _write_dispatcher_session_start() writes a valid Unix epoch to the file
- File is created atomically (written to .tmp then renamed)
- Parent directory is created if it does not exist
- LOBSTER_DISPATCHER_SESSION_START_FILE_OVERRIDE env var controls the output path
- Silent on write failure (must not crash the SessionStart hook)
- main() calls _write_dispatcher_session_start() for dispatcher sessions
- main() does NOT call _write_dispatcher_session_start() for subagent sessions
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_INJECT_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"

# Named constant matching the spec: the file that records session start time.
DISPATCHER_SESSION_START_FILENAME = "dispatcher-session-start.ts"

# Named constant matching the spec: env var that overrides the output path.
SESSION_START_FILE_OVERRIDE_ENV = "LOBSTER_DISPATCHER_SESSION_START_FILE_OVERRIDE"

# Named constant matching the spec: proactive restart threshold.
SESSION_AGE_LIMIT_SECONDS = 7200


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


def _load_inject_hook(*, workspace: Path, start_file_override: Path | None = None) -> object:
    """Load inject-bootup-context.py with controlled env vars."""
    import uuid

    env = {"LOBSTER_WORKSPACE": str(workspace)}
    if start_file_override is not None:
        env[SESSION_START_FILE_OVERRIDE_ENV] = str(start_file_override)

    unique_name = f"inject_bootup_session_start_{uuid.uuid4().hex}"
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location(unique_name, _INJECT_HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests for _write_dispatcher_session_start()
# ---------------------------------------------------------------------------


class TestWriteDispatcherSessionStart:
    """Unit tests for _write_dispatcher_session_start() in inject-bootup-context.py."""

    def test_writes_unix_epoch_integer_to_file(self, tmp_path):
        """Calling the function produces a file containing a valid Unix epoch integer."""
        start_file = tmp_path / "data" / DISPATCHER_SESSION_START_FILENAME
        mod = _load_inject_hook(workspace=tmp_path, start_file_override=start_file)

        before = int(time.time())
        mod._write_dispatcher_session_start()
        after = int(time.time())

        assert start_file.exists(), "session start file not written"
        content = start_file.read_text().strip()
        assert content.isdigit(), f"expected integer epoch, got: {content!r}"
        epoch = int(content)
        assert before <= epoch <= after, (
            f"epoch {epoch} out of expected range [{before}, {after}]"
        )

    def test_env_var_overrides_output_path(self, tmp_path):
        """LOBSTER_DISPATCHER_SESSION_START_FILE_OVERRIDE controls the output file."""
        custom_path = tmp_path / "custom" / "session.ts"
        mod = _load_inject_hook(workspace=tmp_path, start_file_override=custom_path)

        mod._write_dispatcher_session_start()

        assert custom_path.exists(), "custom path was not created"

    def test_creates_parent_directory_if_missing(self, tmp_path):
        """Parent directory of the start file is created automatically."""
        start_file = tmp_path / "deep" / "nested" / DISPATCHER_SESSION_START_FILENAME
        mod = _load_inject_hook(workspace=tmp_path, start_file_override=start_file)

        mod._write_dispatcher_session_start()

        assert start_file.exists(), "file not written when parent dir was missing"

    def test_atomic_write_no_partial_file_left_on_success(self, tmp_path):
        """After a successful write, no .tmp file remains."""
        start_file = tmp_path / "data" / DISPATCHER_SESSION_START_FILENAME
        mod = _load_inject_hook(workspace=tmp_path, start_file_override=start_file)

        mod._write_dispatcher_session_start()

        tmp_file = start_file.with_suffix(".tmp")
        assert not tmp_file.exists(), ".tmp file left behind after successful write"

    def test_silent_on_oserror(self, tmp_path):
        """OSError during write must not propagate — hook must not crash."""
        start_file = tmp_path / "data" / DISPATCHER_SESSION_START_FILENAME
        mod = _load_inject_hook(workspace=tmp_path, start_file_override=start_file)

        with patch("pathlib.Path.replace", side_effect=OSError("disk full")):
            # Must not raise
            mod._write_dispatcher_session_start()

    def test_overwrites_stale_timestamp_on_restart(self, tmp_path):
        """A new dispatcher session overwrites any stale timestamp from a prior session."""
        start_file = tmp_path / "data" / DISPATCHER_SESSION_START_FILENAME
        start_file.parent.mkdir(parents=True, exist_ok=True)
        start_file.write_text("1000000000\n")  # very old epoch

        mod = _load_inject_hook(workspace=tmp_path, start_file_override=start_file)
        mod._write_dispatcher_session_start()

        content = int(start_file.read_text().strip())
        assert content > 1000000000, "stale timestamp was not overwritten"


# ---------------------------------------------------------------------------
# Integration: main() calls _write_dispatcher_session_start() for dispatcher
# ---------------------------------------------------------------------------


class TestMainCallsSessionStartWrite:
    """Integration tests: main() calls _write_dispatcher_session_start() for dispatcher sessions."""

    def _make_hook_input(self, session_id: str = "test-session-123") -> str:
        return json.dumps({"session_id": session_id})

    def _make_dispatcher_startup_flag(self, data_dir: Path) -> Path:
        """Write a startup flag containing the current PID (live dispatcher detection)."""
        data_dir.mkdir(parents=True, exist_ok=True)
        flag = data_dir / "dispatcher-startup-flag"
        flag.write_text(f"{os.getpid()}\n")
        return flag

    def test_session_start_written_for_dispatcher(self, tmp_path):
        """main() writes the session start file when the startup flag marks a dispatcher."""
        start_file = tmp_path / "data" / DISPATCHER_SESSION_START_FILENAME
        self._make_dispatcher_startup_flag(tmp_path / "data")

        # Also need the startup-cause file to exist.
        cause_file = tmp_path / "data" / "last-startup-cause.json"
        cause_file.parent.mkdir(parents=True, exist_ok=True)
        cause_file.write_text(json.dumps({"cause": "restart", "ts": "2026-01-01T00:00:00Z"}))

        mod = _load_inject_hook(workspace=tmp_path, start_file_override=start_file)

        hook_input = self._make_hook_input()
        before = int(time.time())
        with (
            patch.object(mod, "_read_file_safe", return_value="# dispatcher bootup"),
            patch.object(mod, "_inject_if_exists", return_value=False),
            patch.object(mod, "_append_injection_log"),
            patch("builtins.print"),
            patch("sys.exit"),
            patch("sys.stdin", io.StringIO(hook_input)),
        ):
            mod.main()
        after = int(time.time())

        assert start_file.exists(), "session start file not written for dispatcher"
        epoch = int(start_file.read_text().strip())
        assert before <= epoch <= after

    def test_session_start_NOT_written_for_subagent(self, tmp_path):
        """main() does NOT write the session start file for subagent sessions."""
        start_file = tmp_path / "data" / DISPATCHER_SESSION_START_FILENAME
        # No startup flag → detected as subagent.
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        cause_file = tmp_path / "data" / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"cause": "restart", "ts": "2026-01-01T00:00:00Z"}))

        mod = _load_inject_hook(workspace=tmp_path, start_file_override=start_file)

        hook_input = self._make_hook_input()
        with (
            patch.object(mod, "_read_file_safe", return_value="# subagent bootup"),
            patch.object(mod, "_inject_if_exists", return_value=False),
            patch.object(mod, "_append_injection_log"),
            patch("builtins.print"),
            patch("sys.exit"),
            patch("sys.stdin", io.StringIO(hook_input)),
        ):
            mod.main()

        assert not start_file.exists(), "session start file must NOT be written for subagent"
