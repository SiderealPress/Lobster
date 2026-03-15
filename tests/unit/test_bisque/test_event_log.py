"""Tests for bisque bounded event log with replay and dedup."""

from __future__ import annotations

import pytest

from bisque.event_log import EventLog


# =============================================================================
# Basic operations
# =============================================================================


class TestEventLogBasics:
    def test_empty_log(self):
        log = EventLog()
        assert len(log) == 0
        assert log.get_latest_id() is None

    def test_append_and_len(self):
        log = EventLog()
        log.append("evt-1", '{"type":"message"}')
        assert len(log) == 1

    def test_contains(self):
        log = EventLog()
        log.append("evt-1", '{"type":"message"}')
        assert log.contains("evt-1")
        assert not log.contains("evt-2")

    def test_get_latest_id(self):
        log = EventLog()
        log.append("evt-1", "frame1")
        log.append("evt-2", "frame2")
        assert log.get_latest_id() == "evt-2"

    def test_clear(self):
        log = EventLog()
        log.append("evt-1", "frame1")
        log.clear()
        assert len(log) == 0
        assert log.get_latest_id() is None


# =============================================================================
# Eviction at capacity
# =============================================================================


class TestEventLogEviction:
    def test_evict_at_capacity(self):
        log = EventLog(max_events=3)
        for i in range(5):
            log.append(f"evt-{i}", f"frame-{i}")
        assert len(log) == 3
        assert not log.contains("evt-0")
        assert not log.contains("evt-1")
        assert log.contains("evt-2")
        assert log.contains("evt-3")
        assert log.contains("evt-4")

    def test_max_events_1(self):
        log = EventLog(max_events=1)
        log.append("evt-1", "frame1")
        log.append("evt-2", "frame2")
        assert len(log) == 1
        assert log.get_latest_id() == "evt-2"
        assert not log.contains("evt-1")


# =============================================================================
# Replay
# =============================================================================


class TestEventLogReplay:
    def test_replay_after_returns_subsequent_events(self):
        log = EventLog()
        log.append("evt-1", "frame1")
        log.append("evt-2", "frame2")
        log.append("evt-3", "frame3")
        result = log.replay_after("evt-1")
        assert result == ["frame2", "frame3"]

    def test_replay_after_last_returns_empty(self):
        log = EventLog()
        log.append("evt-1", "frame1")
        log.append("evt-2", "frame2")
        result = log.replay_after("evt-2")
        assert result == []

    def test_replay_after_stale_returns_none(self):
        log = EventLog(max_events=2)
        log.append("evt-1", "frame1")
        log.append("evt-2", "frame2")
        log.append("evt-3", "frame3")
        # evt-1 was evicted
        result = log.replay_after("evt-1")
        assert result is None

    def test_replay_after_unknown_returns_none(self):
        log = EventLog()
        log.append("evt-1", "frame1")
        result = log.replay_after("evt-unknown")
        assert result is None

    def test_replay_after_first_event(self):
        log = EventLog()
        log.append("evt-1", "frame1")
        log.append("evt-2", "frame2")
        log.append("evt-3", "frame3")
        result = log.replay_after("evt-1")
        assert len(result) == 2

    def test_replay_empty_log(self):
        log = EventLog()
        result = log.replay_after("anything")
        assert result is None


# =============================================================================
# Ordering
# =============================================================================


class TestEventLogOrdering:
    def test_ordering_preserved(self):
        log = EventLog()
        ids = [f"evt-{i}" for i in range(10)]
        for eid in ids:
            log.append(eid, f"frame-{eid}")
        result = log.replay_after("evt-0")
        assert len(result) == 9
        for i, frame in enumerate(result):
            assert frame == f"frame-evt-{i + 1}"

    def test_latest_id_is_last_appended(self):
        log = EventLog()
        for i in range(20):
            log.append(f"evt-{i}", f"frame-{i}")
        assert log.get_latest_id() == "evt-19"


# =============================================================================
# Edge cases
# =============================================================================


class TestEventLogEdgeCases:
    def test_default_max_events(self):
        log = EventLog()
        # Default is 500
        for i in range(600):
            log.append(f"evt-{i}", f"frame-{i}")
        assert len(log) == 500
        assert log.contains("evt-100")
        assert not log.contains("evt-99")

    def test_empty_event_id(self):
        log = EventLog()
        log.append("", "frame")
        assert log.contains("")
        assert log.get_latest_id() == ""

    def test_replay_returns_new_list(self):
        """Replay should return a new list, not a view into internals."""
        log = EventLog()
        log.append("evt-1", "frame1")
        log.append("evt-2", "frame2")
        result = log.replay_after("evt-1")
        result.append("injected")
        # Original log unaffected
        assert len(log) == 2
