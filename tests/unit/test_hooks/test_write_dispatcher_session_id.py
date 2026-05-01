"""
Unit tests for hooks/write-dispatcher-session-id.py — primary file write on dispatcher detection.

Issue #1903: When CC restarts but MCP keeps running, inject-bootup-context.py
receives subagent bootup instead of dispatcher bootup.

Root cause:
- Primary file (dispatcher-claude-session-id) holds the OLD dispatcher UUID
  from before the CC restart.
- write-dispatcher-session-id.py (Hook 1) correctly identifies the new session
  as the dispatcher (via the stored JSONL fallback) and writes the tertiary file.
- inject-bootup-context.py (Hook 2) checks the primary file:
  _check_state_file(primary, new_session_id) → False (stale UUID, not absent).
  _is_fresh_start_dispatcher() → also False (file exists).
  Both paths fail → subagent bootup injected.

Fix: write-dispatcher-session-id.py must also write the primary file
(dispatcher-claude-session-id) with the new session UUID when it determines
the session is the dispatcher, same as on-compact.py already does.

Tests verify:
- write_dispatcher_claude_session_id is called with the new session UUID when
  the session is identified as the dispatcher.
- The primary file actually contains the new UUID after the hook runs.
- The primary file is NOT written when the session is a subagent.
- Behaviour is correct for all three _is_dispatcher_session() paths:
  fresh start (no marker), same-session reattach, and stale-marker recovery.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "write-dispatcher-session-id.py"

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_hook(*, workspace: Path, home: Path) -> object:
    """Load write-dispatcher-session-id.py with test-controlled paths.

    Returns the loaded module. Uses a unique name to avoid sys.modules
    pollution between test calls.

    LOBSTER_WORKSPACE and HOME are set via patch.dict(os.environ) during module
    load, and callers set them via monkeypatch.setenv before calling mod.main(),
    so write_dispatcher_claude_session_id resolves the primary file path at call
    time using the test-controlled env values.
    """
    import uuid

    env = {
        "LOBSTER_WORKSPACE": str(workspace),
        "HOME": str(home),
        "LOBSTER_MAIN_SESSION": "1",
    }
    unique_name = f"write_dispatcher_session_id_{uuid.uuid4().hex}"
    with patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _make_hook_input(session_id: str) -> str:
    return json.dumps({"session_id": session_id})


def _create_stored_jsonl(home: Path, session_id: str, age_seconds: float = 7200) -> Path:
    """Create a fake session JSONL file aged `age_seconds` in the past.

    Used to simulate an idle/dead stored session (age > JSONL_MAX_IDLE_SECONDS)
    so the stale-marker recovery path triggers and the new session is identified
    as the replacement dispatcher.
    """
    projects_dir = home / ".claude" / "projects" / "fake-ws"
    projects_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = projects_dir / f"{session_id}.jsonl"
    jsonl_path.write_text('{"type":"text"}\n')
    mtime = time.time() - age_seconds
    os.utime(str(jsonl_path), (mtime, mtime))
    return jsonl_path


# ---------------------------------------------------------------------------
# Tests: primary file written when dispatcher is identified
# ---------------------------------------------------------------------------


class TestPrimaryFileWrittenForDispatcher:
    """write-dispatcher-session-id.py must write the primary Claude UUID state file
    (dispatcher-claude-session-id) whenever it determines the current session is
    the dispatcher. This ensures inject-bootup-context.py (Hook 2) finds the correct
    UUID in the primary file and injects dispatcher bootup, even when the MCP server
    kept running across a CC restart (leaving a stale UUID in the primary file).
    """

    def test_primary_file_written_on_fresh_start(self, tmp_path, monkeypatch):
        """No tertiary marker file → fresh dispatcher start → primary file written.

        This is the simplest case: the tertiary marker is absent, so
        _is_dispatcher_session() returns True immediately (fresh start).
        The primary file must be written with the new session UUID.
        """
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        # Reload session_role so DISPATCHER_SESSION_FILE + _get_mcp_claude_session_file()
        # both resolve to paths under tmp_path.
        sr = importlib.reload(_sr)
        monkeypatch.setattr(sr, "DISPATCHER_SESSION_FILE",
                            tmp_path / "messages" / "config" / "dispatcher-session-id")

        new_session_id = "fresh-dispatcher-uuid-1234"
        # Neither tertiary marker nor primary file exists (brand new install).
        (tmp_path / "messages" / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        hook_input = _make_hook_input(new_session_id)

        mod = _load_hook(workspace=tmp_path, home=tmp_path)
        # Point the hook's session_role reference at the reloaded module.
        mod.session_role = sr
        # Suppress DB registration to avoid real DB writes in tests.
        mod._register_dispatcher_session = lambda sid: None

        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit):
                mod.main()

        primary_file = tmp_path / "data" / "dispatcher-claude-session-id"
        assert primary_file.exists(), (
            "Primary file must be written when the hook identifies a fresh dispatcher start"
        )
        assert primary_file.read_text().strip() == new_session_id

    def test_primary_file_written_on_stale_marker_recovery(self, tmp_path, monkeypatch):
        """Issue #1903 regression test: stale primary UUID → CC restart → primary file updated.

        Simulates the exact failure mode:
        - CC restarted (new session UUID d47ecad9)
        - MCP kept running (old primary file still has dbcad92a)
        - Tertiary file has dbcad92a (same old UUID)
        - write-dispatcher-session-id.py checks: dbcad92a JSONL is old (dead) → new
          session is the replacement dispatcher
        - Primary file must be updated to d47ecad9 before inject-bootup-context.py runs
        """
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr)

        old_session_id = "dbcad92a-old-dispatcher-uuid"
        new_session_id = "d47ecad9-new-dispatcher-uuid"

        # Tertiary file has the stale (old) UUID.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text(old_session_id)
        monkeypatch.setattr(sr, "DISPATCHER_SESSION_FILE",
                            config_dir / "dispatcher-session-id")

        # Primary file also has the stale UUID (MCP kept running).
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        primary_file = data_dir / "dispatcher-claude-session-id"
        primary_file.write_text(old_session_id)

        # Old session JSONL is aged > JSONL_MAX_IDLE_SECONDS (4 hours old → dead).
        FOUR_HOURS = 4 * 60 * 60
        _create_stored_jsonl(tmp_path, old_session_id, age_seconds=FOUR_HOURS)

        hook_input = _make_hook_input(new_session_id)

        mod = _load_hook(workspace=tmp_path, home=tmp_path)
        mod.session_role = sr
        mod._register_dispatcher_session = lambda sid: None

        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit):
                mod.main()

        # Primary file must now contain the new session UUID.
        assert primary_file.read_text().strip() == new_session_id, (
            "Primary file must be updated to the new dispatcher UUID so that "
            "inject-bootup-context.py can identify this session as the dispatcher"
        )

    def test_primary_file_written_on_same_session_reattach(self, tmp_path, monkeypatch):
        """Session reattach (same UUID stored in tertiary) → primary file written.

        If the dispatcher session ID already matches the stored tertiary value,
        _is_dispatcher_session() returns True immediately (reattach).  The primary
        file must still be written (or refreshed) with the current UUID.
        """
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr)

        session_id = "same-dispatcher-uuid-reattach"

        # Tertiary has the matching UUID.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text(session_id)
        monkeypatch.setattr(sr, "DISPATCHER_SESSION_FILE",
                            config_dir / "dispatcher-session-id")

        # Primary does NOT exist (e.g. MCP restarted after CC reattached).
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        hook_input = _make_hook_input(session_id)

        mod = _load_hook(workspace=tmp_path, home=tmp_path)
        mod.session_role = sr
        mod._register_dispatcher_session = lambda sid: None

        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit):
                mod.main()

        primary_file = data_dir / "dispatcher-claude-session-id"
        assert primary_file.exists(), (
            "Primary file must be written even for same-session reattach"
        )
        assert primary_file.read_text().strip() == session_id


# ---------------------------------------------------------------------------
# Tests: primary file NOT written for subagent sessions
# ---------------------------------------------------------------------------


class TestPrimaryFileNotWrittenForSubagent:
    """The primary file must never be written for a subagent session.

    A subagent session has a different UUID and its stored dispatcher session
    is still alive (JSONL file recently modified).
    """

    def test_primary_file_not_written_for_subagent(self, tmp_path, monkeypatch):
        """Subagent session: stored dispatcher JSONL is alive → not written."""
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr)

        dispatcher_session_id = "running-dispatcher-uuid-5678"
        subagent_session_id = "subagent-uuid-9999"

        # Tertiary has the running dispatcher UUID.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text(dispatcher_session_id)
        monkeypatch.setattr(sr, "DISPATCHER_SESSION_FILE",
                            config_dir / "dispatcher-session-id")

        # Dispatcher JSONL is ALIVE (modified 30 seconds ago → well within 3-hour window).
        _create_stored_jsonl(tmp_path, dispatcher_session_id, age_seconds=30)

        # Primary file absent.
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Hook receives the SUBAGENT's session ID.
        hook_input = _make_hook_input(subagent_session_id)

        mod = _load_hook(workspace=tmp_path, home=tmp_path)
        mod.session_role = sr
        mod._register_dispatcher_session = lambda sid: None

        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit):
                mod.main()

        primary_file = data_dir / "dispatcher-claude-session-id"
        assert not primary_file.exists(), (
            "Primary file must NOT be written for a subagent session"
        )

    def test_primary_file_not_written_when_lobster_main_session_unset(
        self, tmp_path, monkeypatch
    ):
        """LOBSTER_MAIN_SESSION not set → hook exits immediately, no writes."""
        monkeypatch.delenv("LOBSTER_MAIN_SESSION", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        hook_input = _make_hook_input("any-session-id")

        mod = _load_hook(workspace=tmp_path, home=tmp_path)

        monkeypatch.delenv("LOBSTER_MAIN_SESSION", raising=False)
        with patch("sys.stdin", __import__("io").StringIO(hook_input)):
            with pytest.raises(SystemExit):
                mod.main()

        primary_file = tmp_path / "data" / "dispatcher-claude-session-id"
        assert not primary_file.exists(), (
            "Primary file must not be written for sessions outside Lobster's management"
        )


# ---------------------------------------------------------------------------
# Tests: inject-bootup-context.py correctly identifies dispatcher after hook runs
# ---------------------------------------------------------------------------


class TestInjectBootupContextAfterHookRuns:
    """End-to-end: after write-dispatcher-session-id.py runs, inject-bootup-context.py
    finds the correct primary file UUID and injects dispatcher bootup.

    This is the specific bug that issue #1903 reports: after writing the primary file
    in Hook 1, Hook 2 (inject-bootup-context.py) must correctly identify the session
    as the dispatcher via the primary file match path (not via the fresh-start fallback).
    """

    def test_primary_file_match_after_stale_recovery(self, tmp_path, monkeypatch):
        """After Hook 1 overwrites stale primary, is_dispatcher() returns True for new UUID."""
        import importlib
        import session_role as _sr

        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr)

        old_session_id = "dbcad92a-stale-old-uuid"
        new_session_id = "d47ecad9-fresh-new-uuid"

        # Simulate what the hook writes: new UUID in primary file.
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "dispatcher-claude-session-id").write_text(new_session_id)

        # Reload session_role to pick up new LOBSTER_WORKSPACE.
        sr = importlib.reload(_sr)

        # inject-bootup-context.py (Hook 2) calls is_dispatcher({session_id: new_uuid})
        assert sr.is_dispatcher({"session_id": new_session_id}) is True, (
            "After Hook 1 writes the primary file with new UUID, "
            "is_dispatcher() must return True for the new session"
        )
        assert sr.is_dispatcher({"session_id": old_session_id}) is False, (
            "The old stale UUID must not match after the primary file is updated"
        )
