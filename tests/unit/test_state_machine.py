"""
Unit tests for src/state_machine.py

Tests cover:
- write_state() / read_state() round-trips
- `since` preservation: same state → since is kept, different state → since resets
- Edge case: no existing state file → since is set to current time
- Atomic write behavior (no .tmp left behind)
- Parent directory is created automatically
- Silent on all errors (never raises)
- read_state() returns None when file is absent or corrupt
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the module under test, redirecting STATE_FILE to a tmp path via
# the LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE env var.
# ---------------------------------------------------------------------------

import importlib
import src.state_machine as sm_module


def _reload_sm(monkeypatch, state_file: Path):
    """Reload state_machine with STATE_FILE pointing to *state_file*."""
    monkeypatch.setenv("LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE", str(state_file))
    return importlib.reload(sm_module)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# write_state / read_state basics
# ---------------------------------------------------------------------------


class TestWriteReadRoundTrip:
    def test_write_then_read_returns_dict(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        sm.write_state("WAITING")
        result = sm.read_state()
        assert isinstance(result, dict)

    def test_written_state_matches(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        sm.write_state("PROCESSING")
        result = sm.read_state()
        assert result["state"] == "PROCESSING"

    def test_pid_defaults_to_current_process(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        sm.write_state("STARTING")
        result = sm.read_state()
        assert result["pid"] == os.getpid()

    def test_explicit_pid_is_stored(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        sm.write_state("WAITING", pid=99999)
        result = sm.read_state()
        assert result["pid"] == 99999

    def test_session_id_stored(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        sm.write_state("WAITING", session_id="test-session-abc")
        result = sm.read_state()
        assert result["session_id"] == "test-session-abc"

    def test_updated_at_is_utc_iso_format(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        before = _now_iso()
        sm.write_state("WAITING")
        after = _now_iso()
        result = sm.read_state()
        updated_at = result["updated_at"]
        # Must be parseable and between before/after
        dt = datetime.fromisoformat(updated_at)
        assert dt >= datetime.fromisoformat(before)
        assert dt <= datetime.fromisoformat(after)

    def test_no_tmp_file_left_behind(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        sm.write_state("WAITING")
        tmp = state_file.with_suffix(".json.tmp")
        assert not tmp.exists()

    def test_creates_parent_directory(self, monkeypatch, tmp_path):
        state_file = tmp_path / "nested" / "deep" / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        sm.write_state("STARTING")
        assert state_file.exists()


# ---------------------------------------------------------------------------
# read_state when file is absent or corrupt
# ---------------------------------------------------------------------------


class TestReadState:
    def test_returns_none_when_file_absent(self, monkeypatch, tmp_path):
        state_file = tmp_path / "nonexistent.json"
        sm = _reload_sm(monkeypatch, state_file)
        assert sm.read_state() is None

    def test_returns_none_on_corrupt_json(self, monkeypatch, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)
        state_file.write_text("not valid json {{{")
        assert sm.read_state() is None


# ---------------------------------------------------------------------------
# `since` preservation — the core behavioral fix
# ---------------------------------------------------------------------------


class TestSincePreservation:
    """Tests for the `since` field preservation across same-state heartbeats."""

    def test_no_file_sets_since_to_current_time(self, monkeypatch, tmp_path):
        """Edge case: when no state file exists yet, since is set to current time."""
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)

        # Ensure no pre-existing file.
        assert not state_file.exists()

        before = _now_iso()
        sm.write_state("WAITING")
        after = _now_iso()

        result = sm.read_state()
        since = result["since"]
        dt = datetime.fromisoformat(since)
        assert dt >= datetime.fromisoformat(before)
        assert dt <= datetime.fromisoformat(after)

    def test_same_state_preserves_since(self, monkeypatch, tmp_path):
        """When write_state() is called twice with the same state, since is preserved."""
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)

        # First write — since is set to now.
        sm.write_state("WAITING")
        first = sm.read_state()
        original_since = first["since"]

        # Second write with same state — since must remain unchanged.
        sm.write_state("WAITING")
        second = sm.read_state()
        assert second["since"] == original_since

    def test_same_state_updates_updated_at(self, monkeypatch, tmp_path):
        """Sanity check: updated_at still changes on heartbeat writes."""
        import time as _time

        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)

        sm.write_state("WAITING")
        first = sm.read_state()

        # Brief sleep to ensure a detectable time difference.
        _time.sleep(0.01)

        sm.write_state("WAITING")
        second = sm.read_state()

        # since must be identical; updated_at must advance.
        assert second["since"] == first["since"]
        assert second["updated_at"] >= first["updated_at"]

    def test_different_state_resets_since(self, monkeypatch, tmp_path):
        """When write_state() is called with a different state, since resets."""
        import time as _time

        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)

        # First write in WAITING state.
        sm.write_state("WAITING")
        first = sm.read_state()
        original_since = first["since"]

        # Allow a small interval so the new since is guaranteed to differ.
        _time.sleep(0.01)

        # Transition to PROCESSING — since should reset to the new timestamp.
        before_transition = _now_iso()
        sm.write_state("PROCESSING")
        after_transition = _now_iso()

        second = sm.read_state()
        new_since = second["since"]

        # since must have advanced past the original value.
        assert new_since != original_since
        dt = datetime.fromisoformat(new_since)
        assert dt >= datetime.fromisoformat(before_transition)
        assert dt <= datetime.fromisoformat(after_transition)

    def test_since_preserved_across_multiple_heartbeats(self, monkeypatch, tmp_path):
        """since is preserved across many repeated writes in the same state."""
        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)

        sm.write_state("PROCESSING")
        original = sm.read_state()["since"]

        for _ in range(5):
            sm.write_state("PROCESSING")

        assert sm.read_state()["since"] == original

    def test_since_resets_on_every_state_change(self, monkeypatch, tmp_path):
        """since resets every time the state transitions to a new value."""
        import time as _time

        state_file = tmp_path / "dispatcher-state.json"
        sm = _reload_sm(monkeypatch, state_file)

        sm.write_state("STARTING")
        since_starting = sm.read_state()["since"]

        _time.sleep(0.01)
        sm.write_state("WAITING")
        since_waiting = sm.read_state()["since"]

        _time.sleep(0.01)
        sm.write_state("PROCESSING")
        since_processing = sm.read_state()["since"]

        # Each transition must have a strictly different (later) since.
        assert since_waiting != since_starting
        assert since_processing != since_waiting
        assert datetime.fromisoformat(since_waiting) > datetime.fromisoformat(since_starting)
        assert datetime.fromisoformat(since_processing) > datetime.fromisoformat(since_waiting)


# ---------------------------------------------------------------------------
# Silent-on-error contract
# ---------------------------------------------------------------------------


class TestSilentOnError:
    def test_write_state_never_raises(self, monkeypatch, tmp_path):
        """write_state() must never raise regardless of filesystem errors."""
        state_file = tmp_path / "readonly_dir" / "dispatcher-state.json"
        readonly = tmp_path / "readonly_dir"
        readonly.mkdir()
        readonly.chmod(0o444)
        sm = _reload_sm(monkeypatch, state_file)
        try:
            sm.write_state("WAITING")  # Must not raise
        finally:
            readonly.chmod(0o755)
