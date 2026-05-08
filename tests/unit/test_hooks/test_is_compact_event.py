"""
Unit tests for the _is_compact_event() self-gate in hooks/on-compact.py.

The hook is registered with matcher="" so it fires on every SessionStart.
_is_compact_event() reads the CC payload and returns True only for compaction
events, allowing on-compact.py to exit early for non-compact sessions.

Primary check:  data["source"] == "compact"  (CC-documented field)
Fallback check: data["hook_name"] == "compact"  (observed in some CC versions)
"""

import importlib.util
import os
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


def _load_on_compact():
    """Load on-compact.py as a module with safe overrides for state files."""
    overrides = {
        "LOBSTER_STATE_FILE_OVERRIDE": "/tmp/lobster-test-state.json",
        "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE": "/tmp/lobster-test-compaction.json",
        "LOBSTER_OUTBOX_DIR_OVERRIDE": "/tmp/lobster-test-outbox",
        "LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE": "/tmp/lobster-test-last-compact.ts",
    }
    with _PatchEnv(overrides):
        spec = importlib.util.spec_from_file_location("on_compact", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class TestIsCompactEvent:
    """Tests for _is_compact_event() source-field self-gate."""

    @pytest.fixture(scope="class")
    def mod(self):
        return _load_on_compact()

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
        """Empty payload (neither source nor hook_name) must return False."""
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
        """Real CC payloads include session_id and other fields — must still return True."""
        payload = {
            "source": "compact",
            "session_id": "abc123",
            "transcript_path": "/home/lobster/.claude/projects/foo/bar.jsonl",
        }
        assert mod._is_compact_event(payload) is True
