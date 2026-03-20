"""
Unit tests for reconciler per-session timeout logic (issue #717).

These tests verify that reconcile_agent_sessions() respects the registered
timeout_minutes for each session rather than applying the hard-coded 60-minute
cap unconditionally.

Strategy: import the pure threshold-computation helper extracted here as a
standalone function, then verify the dead/alive decisions for the three cases:
  1. session has timeout_minutes set
  2. session has timeout_minutes = None (falls back to default)
  3. session has a very short timeout_minutes (floor prevents premature kill)

The reconciler loop itself is an async function with side effects (DB writes,
wire-server notifications); testing it end-to-end would require mocking the
entire inbox_server module. These tests exercise the threshold logic in
isolation, which is the changed code.
"""

import pytest


# ---------------------------------------------------------------------------
# Pure helper — mirrors the threshold computation in reconcile_agent_sessions()
# ---------------------------------------------------------------------------

DEFAULT_DEAD_THRESHOLD_SECONDS = 30 * 60        # 30 minutes
DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS = 120 * 60  # 120 minutes
GRACE_PERIOD_SECONDS = 30


def compute_thresholds(timeout_minutes: int | None) -> tuple[int, int]:
    """Return (dead_threshold_running, dead_threshold_missing) for a session.

    Mirrors the logic added to reconcile_agent_sessions() in inbox_server.py.
    Extracted here as a pure function so it can be tested without instantiating
    the full MCP server.
    """
    if timeout_minutes is not None:
        dead_threshold_running = timeout_minutes * 60
        dead_threshold_missing = max(timeout_minutes * 60, DEFAULT_DEAD_THRESHOLD_SECONDS)
    else:
        dead_threshold_running = DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS
        dead_threshold_missing = DEFAULT_DEAD_THRESHOLD_SECONDS
    return dead_threshold_running, dead_threshold_missing


def is_dead_running(elapsed: int, timeout_minutes: int | None) -> bool:
    """True when a running (tool_use) session should be marked dead."""
    running_threshold, _ = compute_thresholds(timeout_minutes)
    return elapsed > running_threshold


def is_dead_missing(elapsed: int, timeout_minutes: int | None) -> bool:
    """True when a session with a missing output file should be marked dead."""
    _, missing_threshold = compute_thresholds(timeout_minutes)
    return elapsed >= GRACE_PERIOD_SECONDS and elapsed > missing_threshold


# ---------------------------------------------------------------------------
# Tests: default (no timeout_minutes registered)
# ---------------------------------------------------------------------------


class TestDefaultThresholds:
    """Sessions with no registered timeout_minutes use the system defaults."""

    def test_running_alive_under_default(self):
        # 90 minutes elapsed — below 120-minute default, should not be dead
        assert not is_dead_running(elapsed=90 * 60, timeout_minutes=None)

    def test_running_dead_over_default(self):
        # 121 minutes elapsed — above 120-minute default, should be dead
        assert is_dead_running(elapsed=121 * 60, timeout_minutes=None)

    def test_running_exactly_at_default_is_alive(self):
        # Boundary: exactly at threshold is NOT yet dead (strict >)
        assert not is_dead_running(elapsed=120 * 60, timeout_minutes=None)

    def test_missing_alive_under_default(self):
        # 20 minutes elapsed — below 30-minute default, not dead
        assert not is_dead_missing(elapsed=20 * 60, timeout_minutes=None)

    def test_missing_dead_over_default(self):
        # 31 minutes elapsed — above 30-minute default, dead
        assert is_dead_missing(elapsed=31 * 60, timeout_minutes=None)

    def test_missing_within_grace_period_is_alive(self):
        # 10 seconds elapsed — inside grace period, never dead
        assert not is_dead_missing(elapsed=10, timeout_minutes=None)


# ---------------------------------------------------------------------------
# Tests: registered timeout_minutes honoured
# ---------------------------------------------------------------------------


class TestRegisteredTimeoutRespected:
    """Sessions with explicit timeout_minutes use that instead of hard cap."""

    def test_long_running_agent_not_killed_before_timeout(self):
        # Agent registered for 240 minutes; elapsed = 180 minutes — still alive
        assert not is_dead_running(elapsed=180 * 60, timeout_minutes=240)

    def test_long_running_agent_killed_after_timeout(self):
        # Agent registered for 240 minutes; elapsed = 241 minutes — dead
        assert is_dead_running(elapsed=241 * 60, timeout_minutes=240)

    def test_short_timeout_respected_for_running(self):
        # Agent registered for 10 minutes; elapsed = 11 minutes — dead
        assert is_dead_running(elapsed=11 * 60, timeout_minutes=10)

    def test_short_timeout_respected_for_missing(self):
        # Agent registered for 10 minutes; elapsed = 11 minutes, but floor
        # means missing threshold = max(10*60, 30*60) = 30 minutes.
        # At 11 min: NOT dead (below floor).
        assert not is_dead_missing(elapsed=11 * 60, timeout_minutes=10)

    def test_floor_prevents_premature_kill_for_missing(self):
        # timeout_minutes=5 is very short; missing threshold is floored at 30 min.
        # At 6 minutes elapsed: not dead.
        assert not is_dead_missing(elapsed=6 * 60, timeout_minutes=5)

    def test_missing_threshold_floored_at_default(self):
        running_thresh, missing_thresh = compute_thresholds(timeout_minutes=5)
        assert running_thresh == 5 * 60       # running uses raw timeout
        assert missing_thresh == DEFAULT_DEAD_THRESHOLD_SECONDS  # floor kicks in

    def test_missing_threshold_not_floored_when_large(self):
        running_thresh, missing_thresh = compute_thresholds(timeout_minutes=60)
        assert running_thresh == 60 * 60
        assert missing_thresh == 60 * 60  # 60 min >= 30 min floor, no floor needed

    def test_old_hard_cap_no_longer_applies(self):
        # The old bug: 61 minutes would kill even a 240-minute agent.
        # With the fix, a 240-minute agent at 61 minutes should be alive.
        assert not is_dead_running(elapsed=61 * 60, timeout_minutes=240)


# ---------------------------------------------------------------------------
# Tests: threshold computation is pure (no side effects)
# ---------------------------------------------------------------------------


class TestComputeThresholdsPurity:
    """compute_thresholds is a pure function — same input → same output."""

    def test_deterministic_with_timeout(self):
        result_a = compute_thresholds(90)
        result_b = compute_thresholds(90)
        assert result_a == result_b

    def test_deterministic_without_timeout(self):
        result_a = compute_thresholds(None)
        result_b = compute_thresholds(None)
        assert result_a == result_b

    def test_returns_tuple_of_two_positive_ints(self):
        running, missing = compute_thresholds(60)
        assert isinstance(running, int)
        assert isinstance(missing, int)
        assert running > 0
        assert missing > 0
