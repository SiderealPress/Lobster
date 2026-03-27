"""
Unit tests for the stale-catchup compact-reminder injection in
hooks/on-fresh-start.py (issue #909 safety net).

Validates:
- _is_catchup_stale() correctly identifies stale / fresh / missing state
- _compact_reminder_already_queued() correctly detects existing reminders
- _inject_compact_reminder() writes the correct message or skips when one exists
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"


class _PatchEnv:
    """Context manager to temporarily set environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _load_on_fresh_start(compaction_state_override: str = None, inbox_dir: str = None):
    """Load on-fresh-start.py as a module, overriding file paths for isolation."""
    # We need to patch the module-level constants after load, so we patch env vars
    # that are read at import time and then re-patch constants after import.
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override

    with _PatchEnv(env_patch):
        spec = importlib.util.spec_from_file_location("on_fresh_start", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        # Avoid session_role import failing — stub it
        sys.modules.setdefault("session_role", _make_session_role_stub())
        spec.loader.exec_module(mod)

    # Override runtime-resolved paths on the loaded module
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    if inbox_dir:
        mod.INBOX_DIR = Path(inbox_dir)
    return mod


def _make_session_role_stub():
    """Return a minimal session_role stub module."""
    import types
    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: True
    return stub


class TestIsCatchupStale:
    """Tests for _is_catchup_stale()."""

    def test_returns_true_when_file_absent(self, tmp_path):
        mod = _load_on_fresh_start(
            compaction_state_override=str(tmp_path / "nonexistent.json"),
        )
        assert mod._is_catchup_stale() is True

    def test_returns_true_when_last_catchup_ts_absent(self, tmp_path):
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_compaction_ts": "2026-01-01T00:00:00Z"}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is True

    def test_returns_true_when_catchup_is_old(self, tmp_path):
        # Timestamp 2 hours ago is definitely stale
        two_hours_ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 7200),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": two_hours_ago}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is True

    def test_returns_false_when_catchup_is_recent(self, tmp_path):
        # Timestamp 1 minute ago — well within 30-min threshold
        one_min_ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 60),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": one_min_ago}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is False

    def test_threshold_boundary_just_over(self, tmp_path):
        # 31 minutes ago — should be stale
        threshold = 31 * 60
        old_ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - threshold),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": old_ts}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is True

    def test_threshold_boundary_just_under(self, tmp_path):
        # 29 minutes ago — should not be stale
        threshold = 29 * 60
        recent_ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - threshold),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": recent_ts}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is False


class TestCompactReminderAlreadyQueued:
    """Tests for _compact_reminder_already_queued()."""

    def test_returns_false_when_inbox_empty(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        assert mod._compact_reminder_already_queued() is False

    def test_returns_false_when_inbox_absent(self, tmp_path):
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = tmp_path / "nonexistent_inbox"
        assert mod._compact_reminder_already_queued() is False

    def test_returns_true_when_compact_reminder_exists(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        msg = {"id": "0_compact", "subtype": "compact-reminder", "text": "test"}
        (inbox / "0_compact.json").write_text(json.dumps(msg))

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        assert mod._compact_reminder_already_queued() is True

    def test_returns_false_when_only_regular_messages(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        msg = {"id": "123_msg", "subtype": "user_message", "text": "hi"}
        (inbox / "123_msg.json").write_text(json.dumps(msg))

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        assert mod._compact_reminder_already_queued() is False

    def test_ignores_malformed_json(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "broken.json").write_text("{not valid json")

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        # Should not raise, and should return False (no valid compact-reminder found)
        assert mod._compact_reminder_already_queued() is False


class TestInjectCompactReminder:
    """Tests for _inject_compact_reminder()."""

    def test_writes_compact_reminder_to_inbox(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        files = list(inbox.iterdir())
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["subtype"] == "compact-reminder"
        assert data["source"] == "system"
        assert data["chat_id"] == 0

    def test_injected_message_id_is_0_startup_compact(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].name == "0_startup_compact.json"

    def test_sorts_before_real_messages(self, tmp_path):
        """0_startup_compact.json must sort before epoch-ms user message filenames."""
        startup_name = "0_startup_compact.json"
        real_msg_name = "1773695000000_msg.json"
        # Lexicographic sort: "0_..." < epoch-ms names
        sorted_names = sorted([real_msg_name, startup_name])
        assert sorted_names[0] == startup_name

    def test_skips_when_reminder_already_queued(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        # Pre-populate with an existing compact-reminder
        existing = {"id": "0_compact", "subtype": "compact-reminder", "text": "existing"}
        (inbox / "0_compact.json").write_text(json.dumps(existing))

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        # Should not have added a second file
        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].name == "0_compact.json"

    def test_creates_inbox_dir_if_absent(self, tmp_path):
        inbox = tmp_path / "inbox_not_yet_created"
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        assert inbox.exists()
        files = list(inbox.iterdir())
        assert len(files) == 1

    def test_silent_on_permission_error(self, tmp_path):
        """_inject_compact_reminder must not raise on write failure."""
        # Point to a path that can't be created
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = Path("/proc/lobster_test_nonexistent/inbox")
        # Must not raise
        mod._inject_compact_reminder()

    def test_injected_text_contains_compact_reminder_instructions(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        files = list(inbox.iterdir())
        data = json.loads(files[0].read_text())
        text = data["text"]
        assert "compact_catchup" in text or "compact-catchup" in text or "catchup" in text.lower()
        assert "wait_for_messages" in text
