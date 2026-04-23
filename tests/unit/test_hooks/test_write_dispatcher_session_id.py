"""
Unit tests for hooks/write-dispatcher-session-id.py — specifically the
stale-session detection using JSONL file mtime (issue #1768).

Tests validate:
- JSONL_MAX_IDLE_SECONDS constant is 300 seconds (5 minutes), not 3 hours
- _stored_session_is_alive() treats a JSONL file modified within 5 min as alive
- _stored_session_is_alive() treats a JSONL file modified > 5 min ago as dead
- _is_dispatcher_session() classifies a restart within 5 min correctly as a
  new dispatcher (the bug: 3h threshold caused stale sessions to not be cleared)
"""

import importlib.util
import os
import sys
import time
import types
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "write-dispatcher-session-id.py"

# The threshold value required by issue #1768: 5 minutes (not 3 hours)
REQUIRED_JSONL_MAX_IDLE_SECONDS = 5 * 60  # 300 seconds


def _make_session_role_stub(stored_session_id=None):
    """Return a minimal session_role stub."""
    stub = types.ModuleType("session_role")
    stub._read_dispatcher_session_id = lambda: stored_session_id
    stub.write_dispatcher_session_id = lambda sid: None
    return stub


def _make_session_store_stub():
    """Return a minimal agents.session_store stub."""
    stub = types.ModuleType("agents.session_store")
    stub.init_db = lambda: None
    stub._DEFAULT_DB_PATH = "/tmp/nonexistent.db"

    class FakeConn:
        def execute(self, *a, **kw):
            pass
        def commit(self):
            pass

    stub._get_connection = lambda path: FakeConn()
    return stub


def _load_hook(stored_session_id=None):
    """Load write-dispatcher-session-id.py as an isolated module.

    Installs stubs for 'session_role' and 'agents.session_store' only for the
    duration of the load, then restores whatever was previously in sys.modules.
    This prevents stub pollution across test files that load the real session_role.
    """
    # Save originals so we can restore them after loading
    saved = {
        k: sys.modules.get(k)
        for k in ("session_role", "agents", "agents.session_store")
    }
    # Clean up previous loads of this hook module from sys.modules
    for key in list(sys.modules.keys()):
        if "write_dispatcher_session_id" in key:
            del sys.modules[key]

    stub_session_role = _make_session_role_stub(stored_session_id)
    stub_agents = types.ModuleType("agents")
    stub_session_store = _make_session_store_stub()
    stub_agents.session_store = stub_session_store

    sys.modules["session_role"] = stub_session_role
    sys.modules["agents"] = stub_agents
    sys.modules["agents.session_store"] = stub_session_store

    try:
        spec = importlib.util.spec_from_file_location("write_dispatcher_session_id", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        # Restore original module entries (or remove if they weren't present)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return mod


class TestJSONLMaxIdleSecondsConstant:
    """The constant must be 300s (5 min), not 10800s (3h)."""

    def test_jsonl_max_idle_seconds_is_5_minutes(self):
        """Issue #1768: threshold must be 5 minutes, not 3 hours.

        The 3-hour threshold caused the hook to misclassify a restarted dispatcher
        (that restarted within 3h of last activity) as a subagent. This left stale
        'running' sessions uncleared. The fix reduces the threshold to 5 minutes.
        """
        mod = _load_hook()
        assert mod.JSONL_MAX_IDLE_SECONDS == REQUIRED_JSONL_MAX_IDLE_SECONDS, (
            f"JSONL_MAX_IDLE_SECONDS must be {REQUIRED_JSONL_MAX_IDLE_SECONDS}s (5 min), "
            f"got {mod.JSONL_MAX_IDLE_SECONDS}s "
            f"({'%.1f' % (mod.JSONL_MAX_IDLE_SECONDS / 3600)}h)"
        )

    def test_jsonl_max_idle_seconds_is_not_3_hours(self):
        """Explicitly verify the old (buggy) value is NOT in use."""
        mod = _load_hook()
        old_buggy_value = 3 * 60 * 60  # 10800
        assert mod.JSONL_MAX_IDLE_SECONDS != old_buggy_value, (
            "JSONL_MAX_IDLE_SECONDS is still 3 hours — this is the bug from issue #1768. "
            "It must be reduced to 5 minutes (300s)."
        )


class TestStoredSessionIsAlive:
    """Tests for _stored_session_is_alive() using the 5-minute threshold."""

    def _make_projects_dir_with_jsonl(self, tmp_path, session_id, mtime_offset_seconds):
        """Create a fake ~/.claude/projects dir and JSONL file with the given mtime."""
        projects_dir = tmp_path / ".claude" / "projects" / "workspace-slug"
        projects_dir.mkdir(parents=True)
        jsonl = projects_dir / f"{session_id}.jsonl"
        jsonl.write_text('{"role": "user"}\n')
        mtime = time.time() - mtime_offset_seconds
        os.utime(str(jsonl), (mtime, mtime))
        return tmp_path / ".claude" / "projects"

    def test_file_modified_1_min_ago_is_alive(self, tmp_path, monkeypatch):
        """A JSONL file modified 1 minute ago (within 5-min window) is alive."""
        session_id = "test-session-001"
        projects_dir = self._make_projects_dir_with_jsonl(tmp_path, session_id, 60)

        mod = _load_hook()
        # Patch the projects dir resolution
        monkeypatch.setattr(mod, "os", mod.os)

        # Patch Path inside the function to use our projects_dir
        original_init = mod.Path.__init__

        def patched_expanduser(path_str):
            if "~/.claude/projects" in str(path_str):
                return str(projects_dir)
            import os as _os
            return _os.path.expanduser(str(path_str))

        import os as real_os
        orig_expanduser = real_os.path.expanduser

        real_os.path.expanduser = patched_expanduser
        try:
            result = mod._stored_session_is_alive(session_id)
        finally:
            real_os.path.expanduser = orig_expanduser

        assert result is True

    def test_file_modified_10_min_ago_is_dead(self, tmp_path, monkeypatch):
        """A JSONL file modified 10 minutes ago (beyond 5-min window) is dead."""
        session_id = "test-session-002"
        projects_dir = self._make_projects_dir_with_jsonl(tmp_path, session_id, 10 * 60)

        mod = _load_hook()

        import os as real_os
        orig_expanduser = real_os.path.expanduser

        def patched_expanduser(path_str):
            if "~/.claude/projects" in str(path_str):
                return str(projects_dir)
            return orig_expanduser(str(path_str))

        real_os.path.expanduser = patched_expanduser
        try:
            result = mod._stored_session_is_alive(session_id)
        finally:
            real_os.path.expanduser = orig_expanduser

        assert result is False

    def test_absent_jsonl_returns_false(self, tmp_path):
        """No JSONL file for the session → session is dead (return False)."""
        session_id = "nonexistent-session-003"
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)
        # Note: no JSONL file for this session_id

        mod = _load_hook()

        import os as real_os
        orig_expanduser = real_os.path.expanduser

        def patched_expanduser(path_str):
            if "~/.claude/projects" in str(path_str):
                return str(projects_dir)
            return orig_expanduser(str(path_str))

        real_os.path.expanduser = patched_expanduser
        try:
            result = mod._stored_session_is_alive(session_id)
        finally:
            real_os.path.expanduser = orig_expanduser

        assert result is False

    def test_projects_dir_absent_returns_true_conservative(self, tmp_path):
        """If ~/.claude/projects doesn't exist, return True (conservative/assume alive)."""
        session_id = "some-session-004"
        nonexistent_dir = tmp_path / "nonexistent" / ".claude" / "projects"
        # Don't create this dir

        mod = _load_hook()

        import os as real_os
        orig_expanduser = real_os.path.expanduser

        def patched_expanduser(path_str):
            if "~/.claude/projects" in str(path_str):
                return str(nonexistent_dir)
            return orig_expanduser(str(path_str))

        real_os.path.expanduser = patched_expanduser
        try:
            result = mod._stored_session_is_alive(session_id)
        finally:
            real_os.path.expanduser = orig_expanduser

        assert result is True

    def test_boundary_just_under_5_min_is_alive(self, tmp_path):
        """JSONL modified 4m59s ago → alive (just within 5-min window)."""
        session_id = "boundary-session-005"
        projects_dir = self._make_projects_dir_with_jsonl(
            tmp_path, session_id, 5 * 60 - 1  # 4m59s ago
        )

        mod = _load_hook()

        import os as real_os
        orig_expanduser = real_os.path.expanduser

        def patched_expanduser(path_str):
            if "~/.claude/projects" in str(path_str):
                return str(projects_dir)
            return orig_expanduser(str(path_str))

        real_os.path.expanduser = patched_expanduser
        try:
            result = mod._stored_session_is_alive(session_id)
        finally:
            real_os.path.expanduser = orig_expanduser

        assert result is True

    def test_boundary_just_over_5_min_is_dead(self, tmp_path):
        """JSONL modified 5m1s ago → dead (just beyond 5-min window)."""
        session_id = "boundary-session-006"
        projects_dir = self._make_projects_dir_with_jsonl(
            tmp_path, session_id, 5 * 60 + 1  # 5m1s ago
        )

        mod = _load_hook()

        import os as real_os
        orig_expanduser = real_os.path.expanduser

        def patched_expanduser(path_str):
            if "~/.claude/projects" in str(path_str):
                return str(projects_dir)
            return orig_expanduser(str(path_str))

        real_os.path.expanduser = patched_expanduser
        try:
            result = mod._stored_session_is_alive(session_id)
        finally:
            real_os.path.expanduser = orig_expanduser

        assert result is False


class TestIsDispatcherSessionRestartWithin5Min:
    """Integration test: restart classification based on JSONL idle age.

    This is the core scenario from issue #1768: the dispatcher restarted at 07:17Z
    when the previous session's JSONL was last written at 07:14Z (3 min ago).

    With the old 3h threshold:
    - 3 min ago is alive → new session classified as subagent → marker file not
      updated → on-fresh-start.py skips --mark-failed → stale sessions remain.

    With the new 5-min threshold:
    - 6 min ago is dead → new session correctly classified as replacement dispatcher.
    - 3 min ago is still alive → classified as subagent (correct: old dispatcher
      is still alive, this is genuinely a subagent).
    """

    def _make_projects_dir_with_jsonl(self, tmp_path, session_id, mtime_offset_seconds):
        """Create fake projects dir and JSONL file with the given mtime."""
        projects_dir = tmp_path / ".claude" / "projects" / "ws"
        projects_dir.mkdir(parents=True)
        jsonl = projects_dir / f"{session_id}.jsonl"
        jsonl.write_text('{"role": "user"}\n')
        mtime = time.time() - mtime_offset_seconds
        os.utime(str(jsonl), (mtime, mtime))
        return tmp_path / ".claude" / "projects"

    def _run_is_dispatcher(self, old_session_id, new_session_id, mtime_offset_seconds, tmp_path):
        """Helper: run _is_dispatcher_session() with patched projects dir."""
        projects_dir = self._make_projects_dir_with_jsonl(
            tmp_path, old_session_id, mtime_offset_seconds
        )

        # Stub session_role to return the old session id as stored
        stub_session_role = _make_session_role_stub(stored_session_id=old_session_id)
        sys.modules["session_role"] = stub_session_role

        mod = _load_hook(stored_session_id=old_session_id)

        import os as real_os
        orig_expanduser = real_os.path.expanduser

        def patched_expanduser(path_str):
            if "~/.claude/projects" in str(path_str):
                return str(projects_dir)
            return orig_expanduser(str(path_str))

        real_os.path.expanduser = patched_expanduser
        try:
            result = mod._is_dispatcher_session(new_session_id)
        finally:
            real_os.path.expanduser = orig_expanduser

        return result

    def test_restart_after_6_min_idle_classified_as_dispatcher(self, tmp_path):
        """Restart after previous session went idle 6 min ago → new session is dispatcher.

        6 minutes > 5-minute threshold → stored session is dead → new session is
        the replacement dispatcher.
        """
        result = self._run_is_dispatcher(
            old_session_id="old-dispatcher-session-abc",
            new_session_id="new-dispatcher-session-xyz",
            mtime_offset_seconds=360,  # 6 minutes
            tmp_path=tmp_path,
        )
        assert result is True, (
            "After 6 min idle, new session should be classified as dispatcher "
            "(old session is dead). This is the bug fix for issue #1768."
        )

    def test_restart_after_2_min_idle_classified_as_subagent(self, tmp_path):
        """JSONL modified 2 min ago → session is still alive → new session is subagent.

        2 minutes < 5-minute threshold → stored session is still alive → new session
        is a subagent of the running dispatcher (correct behavior).
        """
        result = self._run_is_dispatcher(
            old_session_id="active-dispatcher-session-abc",
            new_session_id="subagent-session-xyz",
            mtime_offset_seconds=120,  # 2 minutes
            tmp_path=tmp_path,
        )
        assert result is False, (
            "Within 5-min window, new session should be classified as subagent "
            "(the old dispatcher is still alive)."
        )
