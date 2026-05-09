"""
Unit tests for session_role.write_dispatcher_claude_session_id and the
on-compact.py dispatcher-detection behavior (issue #1375, updated for #2046).

After issue #2046 simplification: _is_dispatcher_compact() uses source='compact'
as the sole post-compact signal.  It no longer writes the tertiary marker file
(dispatcher-session-id) — that file is not needed by the simplified detection logic.
The primary Claude UUID file (dispatcher-claude-session-id) was already not written
by on-compact.py after issue #1908.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-compact.py"

# Make session_role importable for assertions.
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PatchEnv:
    """Context manager to temporarily set/unset environment variables."""

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


def _load_on_compact(
    *,
    workspace: Path,
    state_file: Path | None = None,
    compaction_state_file: Path | None = None,
    last_compact_ts_file: Path | None = None,
):
    """Load on-compact.py with test-controlled file paths."""
    env = {
        "LOBSTER_WORKSPACE": str(workspace),
        "LOBSTER_MAIN_SESSION": "1",
    }
    if state_file:
        env["LOBSTER_STATE_FILE_OVERRIDE"] = str(state_file)
    if compaction_state_file:
        env["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = str(compaction_state_file)
    if last_compact_ts_file:
        env["LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE"] = str(last_compact_ts_file)

    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location("on_compact_test", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests for write_dispatcher_claude_session_id in session_role
# ---------------------------------------------------------------------------


class TestWriteDispatcherClaudeSessionId:
    """session_role.write_dispatcher_claude_session_id writes the primary file."""

    def test_writes_primary_file(self, monkeypatch, tmp_path):
        """write_dispatcher_claude_session_id writes the Claude UUID state file."""
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr)

        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        sr.write_dispatcher_claude_session_id("new-compact-uuid-001")

        written = (tmp_path / "data" / "dispatcher-claude-session-id").read_text().strip()
        assert written == "new-compact-uuid-001"

    def test_strips_whitespace(self, monkeypatch, tmp_path):
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        sr.write_dispatcher_claude_session_id("  padded-uuid  ")

        written = (tmp_path / "data" / "dispatcher-claude-session-id").read_text().strip()
        assert written == "padded-uuid"

    def test_creates_parent_directory(self, monkeypatch, tmp_path):
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr)
        # data/ does NOT exist — write_dispatcher_claude_session_id must create it.
        assert not (tmp_path / "data").exists()

        sr.write_dispatcher_claude_session_id("uuid-creates-dir")

        assert (tmp_path / "data" / "dispatcher-claude-session-id").exists()

    def test_silent_on_failure(self, monkeypatch, tmp_path):
        """Errors during write are silently swallowed — must not raise."""
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr)

        # Point workspace at an unwritable location.
        monkeypatch.setenv("LOBSTER_WORKSPACE", "/proc/lobster-no-write-test")
        sr = importlib.reload(_sr)

        # Must not raise.
        sr.write_dispatcher_claude_session_id("any-uuid")

    def test_is_dispatcher_session_passes_after_write(self, monkeypatch, tmp_path):
        """After write_dispatcher_claude_session_id, is_dispatcher_session() returns True.

        NOTE: is_dispatcher() was simplified in issue #1908 to use startup flag only
        and no longer checks state files.  is_dispatcher_session() is the function
        that checks state files (for PreToolUse hook context).
        """
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        # Also redirect HOME so tertiary marker file lives in tmp_path.
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        new_uuid = "post-compact-uuid-1111-2222-3333"
        sr.write_dispatcher_claude_session_id(new_uuid)

        assert sr.is_dispatcher_session({"session_id": new_uuid}) is True

    def test_old_uuid_no_longer_matches_after_write(self, monkeypatch, tmp_path):
        """After updating the primary file, the old UUID falls through to process-tree.

        The old UUID does not match the updated primary file → state-file check
        returns False (mismatch), which is not authoritative.  The process-tree check
        is patched to return False (non-dispatcher process context) to isolate the
        state-file layer.  The new UUID does match the primary file → returns True
        immediately (no process-tree needed).
        """
        import importlib
        import session_role as _sr
        from unittest.mock import patch

        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        old_uuid = "old-dispatcher-uuid-0000"
        new_uuid = "new-compact-uuid-1111"
        (tmp_path / "data" / "dispatcher-claude-session-id").write_text(old_uuid)

        sr.write_dispatcher_claude_session_id(new_uuid)

        # Old UUID: primary mismatch → falls through to process-tree → False (patched).
        with patch.object(sr, "_is_dispatcher_by_process_tree", return_value=False):
            assert sr.is_dispatcher_session({"session_id": old_uuid}) is False
        # New UUID should pass primary check immediately (no process-tree needed).
        assert sr.is_dispatcher_session({"session_id": new_uuid}) is True


# ---------------------------------------------------------------------------
# Tests verifying on-compact.py does NOT write session files
# ---------------------------------------------------------------------------


class TestOnCompactSessionFileWrites:
    """on-compact.py must not write dispatcher session files.

    After issue #2046 simplification: _is_dispatcher_compact() uses source='compact'
    only.  There is no session ID file read/write in the simplified implementation.
    Both the primary (dispatcher-claude-session-id) and tertiary (dispatcher-session-id)
    files must remain unwritten by on-compact.py.
    """

    def test_no_session_files_written_for_dispatcher_compaction(self, monkeypatch, tmp_path):
        """Dispatcher compaction (source='compact'): no session files are written.

        After #2046 simplification, on-compact.py no longer needs to track session
        IDs — source='compact' alone is sufficient.
        """
        import importlib
        import session_role as _sr

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

        new_uuid = "new-post-compact-uuid-9999"

        state_file = tmp_path / "lobster-state.json"
        state_file.write_text('{"mode":"active"}')
        compaction_state = tmp_path / "compaction-state.json"
        last_compact_ts = tmp_path / "last-compact.ts"

        importlib.reload(_sr)

        env_overrides = {
            "LOBSTER_WORKSPACE": str(tmp_path),
            "LOBSTER_MAIN_SESSION": "1",
            "HOME": str(tmp_path),
            "LOBSTER_STATE_FILE_OVERRIDE": str(state_file),
            "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE": str(compaction_state),
            "LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE": str(last_compact_ts),
        }

        with _PatchEnv(env_overrides):
            _cached_sr = sys.modules.pop("session_role", None)
            try:
                spec = importlib.util.spec_from_file_location("on_compact_t", _HOOK_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            finally:
                if _cached_sr is not None:
                    sys.modules["session_role"] = _cached_sr
                else:
                    sys.modules.pop("session_role", None)
            result = mod._is_dispatcher_compact({"session_id": new_uuid, "source": "compact"})

        assert result is True, "_is_dispatcher_compact should return True for source='compact'"

        # Neither session file should be written by the simplified implementation.
        primary_file = tmp_path / "data" / "dispatcher-claude-session-id"
        assert not primary_file.exists(), (
            "Primary Claude session file must NOT be written by on-compact.py"
        )
        tertiary_file = tmp_path / "messages" / "config" / "dispatcher-session-id"
        assert not tertiary_file.exists(), (
            "Tertiary dispatcher session file must NOT be written by on-compact.py "
            "(source='compact' check is stateless)"
        )

    def test_no_session_files_written_for_subagent(self, monkeypatch, tmp_path):
        """Subagent compactions (no source='compact') must not write any session files."""
        import importlib
        import session_role as _sr

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

        subagent_uuid = "subagent-uuid-no-write"

        importlib.reload(_sr)

        env_overrides = {
            "LOBSTER_WORKSPACE": str(tmp_path),
            "LOBSTER_MAIN_SESSION": "0",
            "HOME": str(tmp_path),
        }

        with _PatchEnv(env_overrides):
            spec = importlib.util.spec_from_file_location("on_compact_sub", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            result = mod._is_dispatcher_compact({"session_id": subagent_uuid})

        assert result is False
        primary_file = tmp_path / "data" / "dispatcher-claude-session-id"
        assert not primary_file.exists(), (
            "Primary session file should NOT be written for subagent compaction"
        )
