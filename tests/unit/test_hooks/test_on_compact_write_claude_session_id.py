"""
Unit tests for Option 1 of issue #1375: on-compact.py writes the new
post-compact session UUID to the primary MCP Claude UUID state file
(dispatcher-claude-session-id) when _is_dispatcher_compact() confirms a
dispatcher compaction.

Previously, on-compact.py only updated the tertiary hook marker file
(~/messages/config/dispatcher-session-id) when the compaction fallback fired.
The primary file remained stale, causing inject-bootup-context.py to inject
subagent bootup context instead of dispatcher bootup after every compaction.

The fix adds a call to write_dispatcher_claude_session_id(new_session_id) so
the primary file is up-to-date before any subsequent hooks run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

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


def _dispatcher_hook_input(session_id: str) -> dict:
    return {"session_id": session_id}


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
# Tests verifying on-compact.py calls write_dispatcher_claude_session_id
# ---------------------------------------------------------------------------


class TestOnCompactWritesPrimarySessionFile:
    """on-compact.py compaction fallback behavior tests (issue #1908 simplified).

    After issue #1908, on-compact.py no longer writes the primary Claude UUID
    file (dispatcher-claude-session-id) — inject-bootup-context.py now uses the
    startup flag for SessionStart detection, so the primary file is no longer
    needed at compaction time.

    on-compact.py still writes the tertiary hook marker file
    (~/messages/config/dispatcher-session-id) for is_dispatcher_session()
    (PreToolUse hooks) to use during active processing.
    """

    def _setup_stored_session_jsonl(self, home_dir: Path, stored_uuid: str) -> None:
        """Create a fake stored-session JSONL so _stored_dispatcher_session_alive returns True."""
        projects_dir = home_dir / ".claude" / "projects" / "fake-project"
        projects_dir.mkdir(parents=True, exist_ok=True)
        (projects_dir / f"{stored_uuid}.jsonl").write_text('{"type":"text"}\n')

    def test_tertiary_file_written_primary_file_not_written(self, monkeypatch, tmp_path):
        """Compaction fallback: tertiary marker file updated, primary Claude UUID file NOT written.

        Issue #1908: inject-bootup-context.py uses startup flag (not UUID files),
        so on-compact.py no longer needs to update the primary Claude UUID file.
        It still writes the tertiary marker file for is_dispatcher_session() (PreToolUse hooks).
        """
        import importlib
        import session_role as _sr

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

        # Write stored (old) session UUID to the tertiary marker file.
        stored_uuid = "old-session-uuid-stored"
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text(stored_uuid)

        # Create the JSONL so _stored_dispatcher_session_alive() returns True.
        self._setup_stored_session_jsonl(tmp_path, stored_uuid)

        # New post-compact session ID.
        new_uuid = "new-post-compact-uuid-9999"

        # Minimal support files.
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text('{"mode":"active"}')
        compaction_state = tmp_path / "compaction-state.json"
        last_compact_ts = tmp_path / "last-compact.ts"

        # Reload session_role so path resolution picks up new HOME/WORKSPACE.
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
            with patch("urllib.request.urlopen"):  # suppress Telegram call
                spec = importlib.util.spec_from_file_location("on_compact_t", _HOOK_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Call compaction fallback directly.
                result = mod._is_dispatcher_compact({"session_id": new_uuid})

        assert result is True, "_is_dispatcher_compact should return True for dispatcher compaction"

        # Tertiary marker file should be updated with the new UUID.
        tertiary_file = config_dir / "dispatcher-session-id"
        assert tertiary_file.read_text().strip() == new_uuid, (
            "Tertiary hook marker file must be updated to the new session UUID "
            "so is_dispatcher_session() works in PreToolUse hooks after compaction"
        )

        # Primary Claude UUID file must NOT be written (issue #1908: startup flag handles this).
        primary_file = tmp_path / "data" / "dispatcher-claude-session-id"
        assert not primary_file.exists(), (
            "Primary Claude session file must NOT be written by on-compact.py "
            "(issue #1908: inject-bootup-context.py uses startup flag, not UUID files)"
        )

    def test_primary_file_not_written_for_subagent_compaction(self, monkeypatch, tmp_path):
        """Subagent compactions must not write the primary dispatcher session file."""
        import importlib
        import session_role as _sr

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

        # No stored dispatcher session — _stored_dispatcher_session_alive() returns False.
        # LOBSTER_MAIN_SESSION is set to something other than '1' to simulate subagent.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        subagent_uuid = "subagent-uuid-no-write"

        sr = importlib.reload(_sr)

        env_overrides = {
            "LOBSTER_WORKSPACE": str(tmp_path),
            "LOBSTER_MAIN_SESSION": "0",  # not the main session
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
