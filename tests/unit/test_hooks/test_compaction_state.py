"""
Unit tests for compaction-state.json writing in hooks/on-compact.py.

Tests that write_compaction_state() correctly persists last_compaction_ts
and that the timestamp deduplication logic (max of last_compaction_ts,
last_restart_ts, last_catchup_ts) can be computed correctly from the file.
"""

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-compact.py"


def _load_on_compact(state_file_override: str = None, compaction_state_override: str = None):
    """
    Load on-compact.py as a module, optionally overriding the state file paths
    via environment variables so tests do not touch the real system files.
    """
    env_patch = {}
    if state_file_override:
        env_patch["LOBSTER_STATE_FILE_OVERRIDE"] = state_file_override
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override

    with patch_env(env_patch):
        spec = importlib.util.spec_from_file_location("on_compact", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class patch_env:
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


class TestWriteCompactionState:
    """Tests for write_compaction_state() in on-compact.py."""

    def test_creates_compaction_state_file(self, tmp_path):
        """write_compaction_state creates the file when it does not exist."""
        state_file = tmp_path / "compaction-state.json"
        # dummy lobster-state.json so the hook does not need it
        ls_file = tmp_path / "lobster-state.json"
        ls_file.write_text(json.dumps({"mode": "active"}))

        mod = _load_on_compact(
            state_file_override=str(ls_file),
            compaction_state_override=str(state_file),
        )
        mod.write_compaction_state()

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert "last_compaction_ts" in data

    def test_last_compaction_ts_is_iso_utc(self, tmp_path):
        """last_compaction_ts must be a valid ISO 8601 UTC string ending in Z."""
        state_file = tmp_path / "compaction-state.json"
        ls_file = tmp_path / "lobster-state.json"
        ls_file.write_text(json.dumps({"mode": "active"}))

        mod = _load_on_compact(
            state_file_override=str(ls_file),
            compaction_state_override=str(state_file),
        )
        mod.write_compaction_state()

        data = json.loads(state_file.read_text())
        ts = data["last_compaction_ts"]
        assert isinstance(ts, str)
        assert ts.endswith("Z"), f"Expected UTC timestamp ending in Z, got: {ts!r}"
        # Must parse as ISO 8601
        normalised = ts.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalised)
        assert parsed.tzinfo is not None

    def test_preserves_existing_fields(self, tmp_path):
        """write_compaction_state preserves existing fields in the state file."""
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(
            json.dumps({"last_catchup_ts": "2026-01-01T00:00:00Z"}) + "\n"
        )
        ls_file = tmp_path / "lobster-state.json"
        ls_file.write_text(json.dumps({"mode": "active"}))

        mod = _load_on_compact(
            state_file_override=str(ls_file),
            compaction_state_override=str(state_file),
        )
        mod.write_compaction_state()

        data = json.loads(state_file.read_text())
        assert "last_compaction_ts" in data
        assert "last_catchup_ts" in data
        assert data["last_catchup_ts"] == "2026-01-01T00:00:00Z"

    def test_overwrites_existing_last_compaction_ts(self, tmp_path):
        """A second call to write_compaction_state updates the timestamp."""
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(
            json.dumps({"last_compaction_ts": "2026-01-01T00:00:00Z"}) + "\n"
        )
        ls_file = tmp_path / "lobster-state.json"
        ls_file.write_text(json.dumps({"mode": "active"}))

        mod = _load_on_compact(
            state_file_override=str(ls_file),
            compaction_state_override=str(state_file),
        )
        mod.write_compaction_state()

        data = json.loads(state_file.read_text())
        ts = data["last_compaction_ts"]
        assert ts != "2026-01-01T00:00:00Z", "Timestamp should have been updated"

    def test_silent_on_unwritable_path(self, tmp_path):
        """write_compaction_state must not raise if the path is unwritable."""
        # /proc is not a regular filesystem; writes will fail
        bad_path = Path("/proc/lobster-test/data/compaction-state.json")
        ls_file = tmp_path / "lobster-state.json"
        ls_file.write_text(json.dumps({"mode": "active"}))

        mod = _load_on_compact(
            state_file_override=str(ls_file),
            compaction_state_override=str(bad_path),
        )
        # Must not raise
        mod.write_compaction_state()


class TestCompactionWindowLogic:
    """Tests for timestamp deduplication logic used by catch-up agent.

    The catch-up agent computes max(last_compaction_ts, last_restart_ts,
    last_catchup_ts) to define its query window. These tests validate the
    pure comparison logic without needing any external dependencies.
    """

    def _max_ts(self, *ts_strings):
        """Pure helper: return max timestamp from a list of ISO 8601 strings."""
        results = []
        for ts in ts_strings:
            if not ts:
                continue
            normalised = ts.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalised)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                results.append(parsed)
            except ValueError:
                continue
        return max(results) if results else None

    def test_max_of_three_timestamps(self):
        """max() of compaction/restart/catchup selects the most recent."""
        result = self._max_ts(
            "2026-01-01T10:00:00Z",
            "2026-01-01T11:00:00Z",  # most recent
            "2026-01-01T09:00:00Z",
        )
        expected = datetime.fromisoformat("2026-01-01T11:00:00+00:00")
        assert result == expected

    def test_max_with_only_compaction_ts(self):
        """Works correctly when only last_compaction_ts is present."""
        result = self._max_ts("2026-03-01T12:00:00Z")
        expected = datetime.fromisoformat("2026-03-01T12:00:00+00:00")
        assert result == expected

    def test_max_with_empty_list_returns_none(self):
        """Returns None when no timestamps are provided."""
        result = self._max_ts()
        assert result is None

    def test_max_ignores_empty_strings(self):
        """Ignores missing/None/empty string timestamps."""
        result = self._max_ts("", "2026-03-01T12:00:00Z", "")
        expected = datetime.fromisoformat("2026-03-01T12:00:00+00:00")
        assert result == expected

    def test_compaction_later_than_catchup_is_selected(self):
        """When compaction happened after last catchup, compaction TS is used."""
        result = self._max_ts(
            "2026-03-01T10:00:00Z",  # last_catchup_ts (older)
            "2026-03-01T11:30:00Z",  # last_compaction_ts (newer — use this)
        )
        expected = datetime.fromisoformat("2026-03-01T11:30:00+00:00")
        assert result == expected
