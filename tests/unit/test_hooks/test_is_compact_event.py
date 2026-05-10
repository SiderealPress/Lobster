#!/usr/bin/env python3
"""
Unit tests for the _is_compact_event() self-gate in hooks/on-compact.py.

The hook is registered with matcher="" so it fires on every SessionStart.
_is_compact_event() reads the CC payload and returns True only for compaction
events, allowing on-compact.py to exit early for non-compact sessions.

Tier 1 (primary):  data["source"] == "compact"  (CC-documented field)
Tier 2 (fallback): data["hook_name"] == "compact"  (observed in some CC versions)
Tier 3 (filesystem fallback): dispatcher-heartbeat contains a recent digit-only
    Unix timestamp -- used when CC omits both source and hook_name entirely.
"""

import importlib.util
import os
import time
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-compact.py"


class _PatchEnv:
    """Context manager to temporarily set/unset environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved: dict = {}

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


def _load_on_compact(heartbeat_path: str = "/tmp/lobster-test-heartbeat-nonexistent"):
    """Load on-compact.py as a module with safe overrides for state files."""
    overrides = {
        "LOBSTER_STATE_FILE_OVERRIDE": "/tmp/lobster-test-state.json",
        "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE": "/tmp/lobster-test-compaction.json",
        "LOBSTER_OUTBOX_DIR_OVERRIDE": "/tmp/lobster-test-outbox",
        "LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE": "/tmp/lobster-test-last-compact.ts",
        "LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE": heartbeat_path,
    }
    with _PatchEnv(overrides):
        spec = importlib.util.spec_from_file_location("on_compact", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class TestIsCompactEvent:
    """Tests for _is_compact_event() source-field self-gate (tiers 1 and 2)."""

    @pytest.fixture(scope="class")
    def mod(self):
        # Use a non-existent heartbeat file so tier 3 stays dormant
        return _load_on_compact("/tmp/lobster-test-heartbeat-nonexistent")

    def test_source_compact_returns_true(self, mod):
        """Primary path: source='compact' must return True."""
        assert mod._is_compact_event({"source": "compact"}) is True

    def test_source_startup_returns_false(self, mod):
        """Non-compact source value must return False."""
        assert mod._is_compact_event({"source": "startup"}) is False

    def test_source_resume_returns_false(self, mod):
        """source='resume' must not be treated as a compact event."""
        assert mod._is_compact_event({"source": "resume"}) is False

    def test_source_clear_returns_false(self, mod):
        """source='clear' must not be treated as a compact event."""
        assert mod._is_compact_event({"source": "clear"}) is False

    def test_empty_dict_returns_false(self, mod):
        """Empty payload (neither source nor hook_name) must return False when WFM file absent."""
        assert mod._is_compact_event({}) is False

    def test_hook_name_fallback_returns_true(self, mod):
        """Fallback: hook_name='compact' (no source field) must return True."""
        assert mod._is_compact_event({"hook_name": "compact"}) is True

    def test_hook_name_non_compact_returns_false(self, mod):
        """hook_name with a non-compact value must return False."""
        assert mod._is_compact_event({"hook_name": "startup"}) is False

    def test_source_takes_priority_over_hook_name(self, mod):
        """When source is present and non-compact, hook_name fallback is not used."""
        # source="startup" should cause False even when hook_name="compact"
        assert mod._is_compact_event({"source": "startup", "hook_name": "compact"}) is False

    def test_source_compact_with_other_fields(self, mod):
        """Real CC payloads include session_id and other fields -- must still return True."""
        payload = {
            "source": "compact",
            "session_id": "abc123",
            "transcript_path": "/home/lobster/.claude/projects/foo/bar.jsonl",
        }
        assert mod._is_compact_event(payload) is True


class TestIsCompactEventFilesystemFallback:
    """Tests for tier-3 filesystem fallback in _is_compact_event().

    When CC omits both source and hook_name from the SessionStart payload,
    _is_compact_event() falls through to _wfm_was_active(),
    which reads the dispatcher-heartbeat file.
    """

    def _make_heartbeat_file(self, tmp_path: Path, content: str) -> Path:
        """Write a dispatcher-heartbeat file with the given content and return its path."""
        heartbeat_file = tmp_path / "dispatcher-heartbeat"
        heartbeat_file.write_text(content)
        return heartbeat_file

    def _recent_ts(self) -> str:
        """Return a Unix timestamp from 60 seconds ago (within the 900s recency window)."""
        return str(int(time.time()) - 60)

    def test_recent_timestamp_both_fields_absent_returns_true(self, tmp_path):
        """Tier 3 positive: recent digit-only timestamp + both fields absent -> True."""
        hb_file = self._make_heartbeat_file(tmp_path, self._recent_ts() + "\n")
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({}) is True

    def test_recent_timestamp_no_newline_both_fields_absent_returns_true(self, tmp_path):
        """Recent digit-only timestamp without trailing newline must also return True."""
        hb_file = self._make_heartbeat_file(tmp_path, self._recent_ts())
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({}) is True

    def test_stale_timestamp_returns_false(self, tmp_path):
        """Tier 3 negative: stale timestamp (older than 900s) must return False."""
        stale_ts = str(int(time.time()) - 1800)  # 30 minutes ago — beyond recency window
        hb_file = self._make_heartbeat_file(tmp_path, stale_ts + "\n")
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({}) is False

    def test_exited_content_returns_false(self, tmp_path):
        """Tier 3 negative: 'exited' content means WFM returned cleanly -> False."""
        hb_file = self._make_heartbeat_file(tmp_path, "exited\n")
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({}) is False

    def test_exited_no_newline_returns_false(self, tmp_path):
        """'exited' without trailing newline must also return False."""
        hb_file = self._make_heartbeat_file(tmp_path, "exited")
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({}) is False

    def test_file_absent_returns_false(self, tmp_path):
        """Tier 3 negative: file does not exist -> conservatively return False."""
        nonexistent = str(tmp_path / "no-such-file")
        mod = _load_on_compact(nonexistent)
        assert mod._is_compact_event({}) is False

    def test_tier3_not_used_when_source_present(self, tmp_path):
        """Tier 3 must not activate when source field is present (even non-compact)."""
        hb_file = self._make_heartbeat_file(tmp_path, self._recent_ts() + "\n")
        mod = _load_on_compact(str(hb_file))
        # source="startup" must short-circuit to False without consulting filesystem
        assert mod._is_compact_event({"source": "startup"}) is False

    def test_tier3_not_used_when_hook_name_present(self, tmp_path):
        """Tier 3 must not activate when hook_name field is present."""
        hb_file = self._make_heartbeat_file(tmp_path, self._recent_ts() + "\n")
        mod = _load_on_compact(str(hb_file))
        # hook_name="startup" must return False via tier 2; tier 3 not reached
        assert mod._is_compact_event({"hook_name": "startup"}) is False

    def test_tier3_source_compact_still_wins(self, tmp_path):
        """source='compact' always returns True regardless of heartbeat file content."""
        hb_file = self._make_heartbeat_file(tmp_path, "exited\n")
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({"source": "compact"}) is True

    def test_non_digit_content_returns_false(self, tmp_path):
        """Unexpected non-digit, non-exited content must return False (conservative)."""
        hb_file = self._make_heartbeat_file(tmp_path, "not-a-timestamp\n")
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({}) is False

    def test_empty_file_returns_false(self, tmp_path):
        """Empty heartbeat file must return False (no timestamp present)."""
        hb_file = self._make_heartbeat_file(tmp_path, "")
        mod = _load_on_compact(str(hb_file))
        assert mod._is_compact_event({}) is False


class TestWfmWasActive:
    """Focused unit tests for _wfm_was_active() helper."""

    def _load_with_heartbeat(self, heartbeat_path: str):
        return _load_on_compact(heartbeat_path)

    def _recent_ts(self) -> str:
        """Return a Unix timestamp from 60 seconds ago (within the 900s recency window)."""
        return str(int(time.time()) - 60)

    def test_recent_timestamp_returns_true(self, tmp_path):
        """Recent Unix timestamp (within 900s) must return True."""
        f = tmp_path / "dispatcher-heartbeat"
        f.write_text(self._recent_ts() + "\n")
        mod = self._load_with_heartbeat(str(f))
        assert mod._wfm_was_active() is True

    def test_stale_timestamp_returns_false(self, tmp_path):
        """Stale Unix timestamp (older than 900s) must return False."""
        stale_ts = str(int(time.time()) - 1800)  # 30 minutes ago
        f = tmp_path / "dispatcher-heartbeat"
        f.write_text(stale_ts + "\n")
        mod = self._load_with_heartbeat(str(f))
        assert mod._wfm_was_active() is False

    def test_exited_returns_false(self, tmp_path):
        """'exited' content must return False."""
        f = tmp_path / "dispatcher-heartbeat"
        f.write_text("exited\n")
        mod = self._load_with_heartbeat(str(f))
        assert mod._wfm_was_active() is False

    def test_file_missing_returns_false(self, tmp_path):
        """Missing file must return False."""
        mod = self._load_with_heartbeat(str(tmp_path / "nonexistent"))
        assert mod._wfm_was_active() is False

    def test_empty_content_returns_false(self, tmp_path):
        """Empty file must return False."""
        f = tmp_path / "dispatcher-heartbeat"
        f.write_text("")
        mod = self._load_with_heartbeat(str(f))
        assert mod._wfm_was_active() is False

    def test_whitespace_only_returns_false(self, tmp_path):
        """Whitespace-only file must return False (empty after strip)."""
        f = tmp_path / "dispatcher-heartbeat"
        f.write_text("   \n")
        mod = self._load_with_heartbeat(str(f))
        assert mod._wfm_was_active() is False
