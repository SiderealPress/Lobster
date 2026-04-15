"""
Unit tests for the reconciler startup sweep staleness filter (issue #1355).

When the MCP server restarts, it may find unnotified completed sessions from
the last 24 hours. If these sessions completed long before the server started
(i.e. they are "stale"), injecting them into the inbox would flood it with
irrelevant completion notices, blocking real user messages.

The staleness filter silently marks sessions that completed more than
_STARTUP_SWEEP_STALE_MINUTES before the server started as "notified" without
queuing them.

Strategy: extract the staleness check logic into standalone pure functions
and test all branching cases without instantiating the async reconciler.
"""

import pytest
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Pure helper — mirrors the staleness check in _startup_sweep()
# ---------------------------------------------------------------------------

STALE_MINUTES = 10  # mirrors _STARTUP_SWEEP_STALE_MINUTES


def is_session_stale(
    completed_at_str: str | None,
    server_start_time: datetime,
    stale_minutes: int = STALE_MINUTES,
) -> bool:
    """Return True if the session completed before the stale cutoff.

    Mirrors the staleness check in _startup_sweep():
        stale_cutoff = server_start_time - timedelta(minutes=stale_minutes)
        if completed_at < stale_cutoff: return True
    """
    if not completed_at_str:
        return False
    try:
        completed_at = datetime.fromisoformat(
            completed_at_str.replace("Z", "+00:00")
        )
        stale_cutoff = server_start_time - timedelta(minutes=stale_minutes)
        return completed_at < stale_cutoff
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStartupSweepStalenessFilter:
    """Tests for the staleness check applied during startup sweep."""

    def _server_start(self, minutes_ago: float = 0) -> datetime:
        """Return a fake server start time, optionally in the past."""
        return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)

    def test_session_completed_before_cutoff_is_stale(self):
        """Session completed 30 min ago is stale when server just started and threshold is 10 min."""
        server_start = self._server_start(minutes_ago=0)
        completed_at = (server_start - timedelta(minutes=30)).isoformat()
        assert is_session_stale(completed_at, server_start) is True

    def test_session_completed_just_after_cutoff_is_not_stale(self):
        """Session completed 5 min ago is NOT stale (within the 10-min window)."""
        server_start = self._server_start(minutes_ago=0)
        completed_at = (server_start - timedelta(minutes=5)).isoformat()
        assert is_session_stale(completed_at, server_start) is False

    def test_session_completed_exactly_at_cutoff_is_not_stale(self):
        """Session completed exactly at stale_cutoff is not stale (boundary exclusive)."""
        server_start = self._server_start(minutes_ago=0)
        completed_at = (server_start - timedelta(minutes=10)).isoformat()
        # completed_at == stale_cutoff: not strictly less than, so not stale
        assert is_session_stale(completed_at, server_start) is False

    def test_session_completed_after_server_start_is_not_stale(self):
        """Session that somehow completed after server start is not stale."""
        server_start = self._server_start(minutes_ago=0)
        completed_at = (server_start + timedelta(minutes=1)).isoformat()
        assert is_session_stale(completed_at, server_start) is False

    def test_missing_completed_at_is_not_stale(self):
        """Session with no completed_at timestamp is not filtered (safety default)."""
        server_start = self._server_start(minutes_ago=0)
        assert is_session_stale(None, server_start) is False
        assert is_session_stale("", server_start) is False

    def test_malformed_timestamp_is_not_stale(self):
        """Session with unparseable timestamp is not filtered (safety default)."""
        server_start = self._server_start(minutes_ago=0)
        assert is_session_stale("not-a-date", server_start) is False
        assert is_session_stale("2026-99-99T25:00:00Z", server_start) is False

    def test_z_suffix_timestamp_parsed_correctly(self):
        """ISO timestamp with Z suffix is parsed correctly."""
        server_start = self._server_start(minutes_ago=0)
        completed_at = (server_start - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert is_session_stale(completed_at, server_start) is True

    def test_plus_offset_timestamp_parsed_correctly(self):
        """ISO timestamp with +00:00 offset is parsed correctly."""
        server_start = self._server_start(minutes_ago=0)
        completed_at = (server_start - timedelta(minutes=20)).isoformat()
        assert is_session_stale(completed_at, server_start) is True

    def test_custom_stale_minutes(self):
        """Custom stale_minutes threshold is respected."""
        server_start = self._server_start(minutes_ago=0)
        # Completed 3 min ago
        completed_at = (server_start - timedelta(minutes=3)).isoformat()
        # With default 10 min threshold: not stale
        assert is_session_stale(completed_at, server_start, stale_minutes=10) is False
        # With 2 min threshold: stale
        assert is_session_stale(completed_at, server_start, stale_minutes=2) is True

    def test_many_stale_sessions(self):
        """All sessions from 24h ago are correctly identified as stale."""
        server_start = self._server_start(minutes_ago=0)
        stale_sessions = [
            (server_start - timedelta(hours=h)).isoformat()
            for h in [1, 3, 6, 12, 24]
        ]
        for ts in stale_sessions:
            assert is_session_stale(ts, server_start) is True, f"Expected stale: {ts}"

    def test_fresh_sessions_not_filtered(self):
        """Sessions from the last few minutes are not stale."""
        server_start = self._server_start(minutes_ago=0)
        fresh_sessions = [
            (server_start - timedelta(minutes=m)).isoformat()
            for m in [1, 3, 5, 9]
        ]
        for ts in fresh_sessions:
            assert is_session_stale(ts, server_start) is False, f"Expected not stale: {ts}"
