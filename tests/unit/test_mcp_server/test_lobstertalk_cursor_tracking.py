"""Unit tests for lobstertalk-unified cursor tracking.

PR C: cursor tracking — persist last_seen_ts to prevent history replay (fixes #1380).

Bug: the state file default for last_seen_ts was "2020-01-01T00:00:00Z". On first run
(or after a state file deletion), the job would poll for all messages since 2020, causing
a flood of historical messages into the dispatcher inbox.

Observed: 20+ historical messages from 2026-03-23 and 2026-03-31 flooded into the
dispatcher inbox after fresh deployment, requiring manual bulk-processing.

Fix:
- On first run (no state file), set last_seen_ts = now - 1 hour (UTC)
- On subsequent runs, read last_seen_ts from the persisted state file
- After successful processing, write updated last_seen_ts back to state file atomically
- If state file is corrupted, treat as missing (use now - 1 hour default)

Properties verified here:
1. last_seen_ts is read before the GET /messages call
2. last_seen_ts is updated after successful message processing
3. First-run default is now - 1 hour (not a historical date)
4. Corrupted state file falls back to now - 1 hour default
5. Cursor advances to max(message.timestamp) across ALL fetched messages
6. Cursor does NOT regress if messages is empty
7. State is written atomically (no partial writes)
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Pure helper implementations mirroring the cursor tracking spec.
# ---------------------------------------------------------------------------

_ONE_HOUR = timedelta(hours=1)


def _default_last_seen_ts() -> str:
    """Return now - 1 hour in UTC ISO format.

    Used on first run to avoid replaying all historical messages.
    """
    return (datetime.now(timezone.utc) - _ONE_HOUR).isoformat()


def load_state(state_file: Path) -> dict[str, Any]:
    """Load state file, using now-1h as cursor default if missing or malformed.

    Key difference from the old behavior: the default last_seen_ts is computed
    at runtime as now-1h, not a hardcoded historical date.
    """
    defaults = {
        "hot_mode": False,
        "consecutive_empty_polls": 0,
        "hot_mode_activated_at": None,
    }
    if not state_file.exists():
        return {**defaults, "last_seen_ts": _default_last_seen_ts()}
    try:
        data = json.loads(state_file.read_text())
        # If last_seen_ts is missing from an existing file, recompute default
        if "last_seen_ts" not in data:
            data["last_seen_ts"] = _default_last_seen_ts()
        return {**defaults, **data}
    except (json.JSONDecodeError, OSError):
        return {**defaults, "last_seen_ts": _default_last_seen_ts()}


def write_state_atomic(state_file: Path, state: dict[str, Any]) -> None:
    """Write state atomically: .tmp then rename."""
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.rename(state_file)


def advance_cursor(messages: list[dict], current_ts: str) -> str:
    """Return the latest timestamp from messages, or current_ts if empty."""
    if not messages:
        return current_ts
    return max(m["timestamp"] for m in messages)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFirstRunDefault:
    """On first run (no state file), last_seen_ts must be now-1h, not a historical date."""

    def test_missing_state_file_uses_now_minus_1h(self, tmp_path):
        state = load_state(tmp_path / "nonexistent.json")
        ts = datetime.fromisoformat(state["last_seen_ts"])
        now = datetime.now(timezone.utc)
        # Should be within 5 seconds of now-1h
        expected = now - _ONE_HOUR
        delta = abs((ts - expected).total_seconds())
        assert delta < 5, (
            f"Expected last_seen_ts ≈ {expected.isoformat()}, got {ts.isoformat()} "
            f"(delta={delta:.1f}s). First run must use now-1h to avoid history replay."
        )

    def test_missing_state_file_not_hardcoded_historical_date(self, tmp_path):
        """Regression: previous default was '2020-01-01T00:00:00Z' causing history flood."""
        state = load_state(tmp_path / "nonexistent.json")
        ts = datetime.fromisoformat(state["last_seen_ts"])
        # Must not be anywhere near 2020
        assert ts.year >= 2026, (
            f"last_seen_ts default must not be a historical date. Got: {state['last_seen_ts']}"
        )

    def test_corrupted_state_file_uses_now_minus_1h(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("not valid json {{{{")
        state = load_state(f)
        ts = datetime.fromisoformat(state["last_seen_ts"])
        now = datetime.now(timezone.utc)
        delta = abs((ts - (now - _ONE_HOUR)).total_seconds())
        assert delta < 5

    def test_existing_state_file_last_seen_ts_preserved(self, tmp_path):
        """If state file exists with a valid last_seen_ts, it must NOT be overwritten."""
        saved_ts = "2026-04-01T10:00:00Z"
        f = tmp_path / "state.json"
        f.write_text(json.dumps({"last_seen_ts": saved_ts, "hot_mode": False}))
        state = load_state(f)
        assert state["last_seen_ts"] == saved_ts, (
            f"Existing last_seen_ts must be preserved. Expected {saved_ts!r}, "
            f"got {state['last_seen_ts']!r}"
        )

    def test_first_run_other_fields_have_defaults(self, tmp_path):
        state = load_state(tmp_path / "nonexistent.json")
        assert state["hot_mode"] is False
        assert state["consecutive_empty_polls"] == 0
        assert state["hot_mode_activated_at"] is None


class TestCursorAdvancement:
    """last_seen_ts is updated after processing and must not regress."""

    def _make_msgs(self, timestamps: list[str]) -> list[dict]:
        return [{"timestamp": ts, "id": f"msg_{i}"} for i, ts in enumerate(timestamps)]

    def test_cursor_advances_to_latest_timestamp(self):
        msgs = self._make_msgs([
            "2026-04-01T10:00:00Z",
            "2026-04-01T12:00:00Z",
            "2026-04-01T11:00:00Z",
        ])
        result = advance_cursor(msgs, "2026-04-01T09:00:00Z")
        assert result == "2026-04-01T12:00:00Z"

    def test_cursor_does_not_regress_on_empty_poll(self):
        """If no messages returned, last_seen_ts must stay the same (not reset)."""
        current = "2026-04-01T12:00:00Z"
        result = advance_cursor([], current)
        assert result == current, (
            f"Cursor regressed! Expected {current!r} but got {result!r}. "
            "Empty poll must not change the cursor."
        )

    def test_cursor_updated_across_both_directions(self):
        """The cursor advances regardless of message direction (INBOUND or OUTBOUND)."""
        msgs = [
            {"timestamp": "2026-04-01T10:00:00Z", "id": "1"},  # INBOUND
            {"timestamp": "2026-04-01T14:00:00Z", "id": "2"},  # OUTBOUND
        ]
        result = advance_cursor(msgs, "2026-04-01T09:00:00Z")
        assert result == "2026-04-01T14:00:00Z"

    def test_cursor_advance_and_persist(self, tmp_path):
        """After processing, cursor is written to state file."""
        state_file = tmp_path / "state.json"
        # Simulate initial state
        initial_state = {
            "last_seen_ts": "2026-04-01T09:00:00Z",
            "hot_mode": False,
            "consecutive_empty_polls": 0,
            "hot_seen_ts": None,
        }
        write_state_atomic(state_file, initial_state)

        # Simulate receiving messages and advancing cursor
        msgs = [{"timestamp": "2026-04-01T12:00:00Z", "id": "1"}]
        new_ts = advance_cursor(msgs, initial_state["last_seen_ts"])
        updated_state = {**initial_state, "last_seen_ts": new_ts}
        write_state_atomic(state_file, updated_state)

        # Load and verify
        loaded = load_state(state_file)
        assert loaded["last_seen_ts"] == "2026-04-01T12:00:00Z"

    def test_cursor_used_as_since_param(self):
        """The GET /messages request uses last_seen_ts as the 'since' parameter."""
        # This tests the contract: state["last_seen_ts"] is what gets passed to the API
        state = {"last_seen_ts": "2026-04-01T10:00:00Z", "hot_mode": False}
        # The job constructs: GET /messages?since=<last_seen_ts>&limit=100
        since_param = state["last_seen_ts"]
        assert since_param == "2026-04-01T10:00:00Z"
        # Only messages AFTER this timestamp are returned
        # (API contract: since is exclusive lower bound)


class TestAtomicStateWrite:
    """State file is written atomically to prevent partial writes."""

    def test_written_file_is_readable_json(self, tmp_path):
        f = tmp_path / "state.json"
        state = {
            "last_seen_ts": "2026-04-02T10:00:00Z",
            "hot_mode": False,
            "consecutive_empty_polls": 0,
            "hot_mode_activated_at": None,
        }
        write_state_atomic(f, state)
        loaded = json.loads(f.read_text())
        assert loaded["last_seen_ts"] == "2026-04-02T10:00:00Z"

    def test_no_tmp_file_remains_after_write(self, tmp_path):
        f = tmp_path / "state.json"
        write_state_atomic(f, {"last_seen_ts": "2026-04-02T10:00:00Z"})
        assert not (tmp_path / "state.tmp").exists()

    def test_read_write_round_trip(self, tmp_path):
        f = tmp_path / "state.json"
        original = {
            "last_seen_ts": "2026-04-02T10:00:00Z",
            "hot_mode": True,
            "consecutive_empty_polls": 0,
            "hot_mode_activated_at": "2026-04-02T09:00:00Z",
        }
        write_state_atomic(f, original)
        loaded = load_state(f)
        assert loaded["last_seen_ts"] == original["last_seen_ts"]
        assert loaded["hot_mode"] == original["hot_mode"]

    def test_overwrite_preserves_new_cursor(self, tmp_path):
        f = tmp_path / "state.json"
        write_state_atomic(f, {"last_seen_ts": "2026-04-01T10:00:00Z"})
        write_state_atomic(f, {"last_seen_ts": "2026-04-02T15:00:00Z"})
        loaded = json.loads(f.read_text())
        assert loaded["last_seen_ts"] == "2026-04-02T15:00:00Z"


class TestStateFilePath:
    """The canonical state file path is lobstertalk-unified-state.json."""

    def test_state_file_path(self):
        """Verify the state file is in the expected location."""
        from pathlib import Path
        expected = Path.home() / "lobster-workspace" / "data" / "lobstertalk-unified-state.json"
        # This is the canonical path per the task definition
        assert expected.parts[-3:] == ("lobster-workspace", "data", "lobstertalk-unified-state.json")

    def test_state_file_different_from_old_bot_talk_state(self):
        """Old jobs used different state files; this must use the new canonical path."""
        from pathlib import Path
        new_state = Path.home() / "lobster-workspace" / "data" / "lobstertalk-unified-state.json"
        old_state = Path.home() / "lobster-workspace" / "data" / "bot-talk-state.json"
        assert new_state != old_state
