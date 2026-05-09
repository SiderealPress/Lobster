"""
Unit tests for reconciler default timeout change (issue #1922).

The global default timeout for missing output files was raised from 30 minutes
to 90 minutes. Long-running tasks such as Docker builds and complex
multi-step implementations were being killed prematurely at the 30-minute mark.

These tests verify the new defaults in isolation using pure functions that
mirror the threshold-computation logic in reconcile_agent_sessions().

Key behaviors tested:
  - The "missing output file" default is now 90 minutes (was 30)
  - The "running tool_use" default remains 120 minutes (unchanged)
  - Per-session timeout_minutes overrides still work (medium-term feature)
  - The floor for the missing-file branch is now 90 minutes (was 30)
  - A long-running legitimate task at 45 minutes is no longer killed
"""

import pytest


# ---------------------------------------------------------------------------
# Constants — mirror reconcile_agent_sessions() in inbox_server.py
# These must be kept in sync with the server-side constants.
# ---------------------------------------------------------------------------

# New defaults after issue #1922 fix
DEFAULT_DEAD_THRESHOLD_SECONDS = 90 * 60          # 90 minutes (raised from 30)
DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS = 120 * 60  # 120 minutes (unchanged)
GRACE_PERIOD_SECONDS = 30


# ---------------------------------------------------------------------------
# Pure helpers — mirror the threshold logic in reconcile_agent_sessions()
# ---------------------------------------------------------------------------


def compute_thresholds(timeout_minutes: int | None) -> tuple[int, int]:
    """Return (dead_threshold_running, dead_threshold_missing) for a session.

    Pure function — no side effects.
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
# Tests: the specific bug from issue #1922
# ---------------------------------------------------------------------------


class TestLongRunningTaskNotKilledPrematurely:
    """Verify the concrete issue: a long-running task is not killed at 30 min."""

    def test_docker_build_at_45_minutes_is_alive(self):
        """docker-telegram-test-setup at 45 min must not be killed (was killed before fix)."""
        assert not is_dead_missing(elapsed=45 * 60, timeout_minutes=None)

    def test_docker_build_at_60_minutes_is_alive(self):
        """Docker build at 60 minutes is within the new 90-minute default."""
        assert not is_dead_missing(elapsed=60 * 60, timeout_minutes=None)

    def test_task_at_89_minutes_is_alive(self):
        """Task at 89 minutes is just below the 90-minute default."""
        assert not is_dead_missing(elapsed=89 * 60, timeout_minutes=None)

    def test_task_at_91_minutes_is_dead(self):
        """Task at 91 minutes is past the 90-minute default — should be dead."""
        assert is_dead_missing(elapsed=91 * 60, timeout_minutes=None)

    def test_task_at_exactly_90_minutes_is_alive(self):
        """Boundary: exactly at 90-minute threshold is NOT yet dead (strict >)."""
        assert not is_dead_missing(elapsed=90 * 60, timeout_minutes=None)


class TestOldDefaultNoLongerKills:
    """Cases that were dead under the old 30-minute default are now alive."""

    def test_31_minutes_elapsed_no_longer_dead(self):
        """Under old default: 31 min → dead. Under new default: 31 min → alive."""
        assert not is_dead_missing(elapsed=31 * 60, timeout_minutes=None)

    def test_50_minutes_elapsed_no_longer_dead(self):
        """Under old default: 50 min → dead. Under new default: 50 min → alive."""
        assert not is_dead_missing(elapsed=50 * 60, timeout_minutes=None)

    def test_89_minutes_elapsed_no_longer_dead(self):
        """Under old default: 89 min → dead. Under new default: 89 min → alive."""
        assert not is_dead_missing(elapsed=89 * 60, timeout_minutes=None)


# ---------------------------------------------------------------------------
# Tests: new default values are correct
# ---------------------------------------------------------------------------


class TestNewDefaultValues:
    """The new default thresholds have the expected numeric values."""

    def test_default_missing_threshold_is_90_minutes(self):
        _, missing_threshold = compute_thresholds(timeout_minutes=None)
        assert missing_threshold == 90 * 60, (
            f"Expected 90 * 60 = {90 * 60}, got {missing_threshold}. "
            "The missing-file threshold was raised from 30 to 90 minutes in issue #1922."
        )

    def test_default_running_threshold_unchanged_at_120_minutes(self):
        running_threshold, _ = compute_thresholds(timeout_minutes=None)
        assert running_threshold == 120 * 60, (
            f"Expected 120 * 60 = {120 * 60}, got {running_threshold}. "
            "The running threshold should remain at 120 minutes."
        )

    def test_floor_for_missing_file_is_now_90_minutes(self):
        """When timeout_minutes is set but very small, floor is now 90 min."""
        _, missing_thresh = compute_thresholds(timeout_minutes=5)
        assert missing_thresh == DEFAULT_DEAD_THRESHOLD_SECONDS  # 90 min floor


# ---------------------------------------------------------------------------
# Tests: per-session timeout_minutes override still works (medium-term feature)
# ---------------------------------------------------------------------------


class TestPerSessionTimeoutOverride:
    """timeout_minutes registered at spawn time still overrides the default."""

    def test_long_running_agent_respects_registered_timeout(self):
        """Agent registered for 240 minutes at 180 minutes → alive."""
        assert not is_dead_running(elapsed=180 * 60, timeout_minutes=240)

    def test_long_running_agent_dead_after_registered_timeout(self):
        """Agent registered for 240 minutes at 241 minutes → dead."""
        assert is_dead_running(elapsed=241 * 60, timeout_minutes=240)

    def test_short_registered_timeout_still_kills_running(self):
        """Agent registered for 10 minutes at 11 minutes → dead."""
        assert is_dead_running(elapsed=11 * 60, timeout_minutes=10)

    def test_short_registered_timeout_floored_at_90_min_for_missing(self):
        """Agent with 10-minute timeout: missing floor is now 90 min.
        At 11 min: alive (below 90-minute floor).
        """
        assert not is_dead_missing(elapsed=11 * 60, timeout_minutes=10)

    def test_large_registered_timeout_not_floored(self):
        """Agent registered for 120 minutes: missing threshold uses registered value."""
        _, missing_thresh = compute_thresholds(timeout_minutes=120)
        assert missing_thresh == 120 * 60  # 120 min >= 90 min floor, no floor applied

    def test_registered_timeout_below_90_floored(self):
        """Agent registered for 60 minutes: missing threshold floored at 90 min."""
        _, missing_thresh = compute_thresholds(timeout_minutes=60)
        assert missing_thresh == DEFAULT_DEAD_THRESHOLD_SECONDS  # floor at 90 min

    def test_registered_timeout_above_90_not_floored(self):
        """Agent registered for 91 minutes: missing threshold is 91 min (no floor)."""
        _, missing_thresh = compute_thresholds(timeout_minutes=91)
        assert missing_thresh == 91 * 60


# ---------------------------------------------------------------------------
# Tests: grace period is unaffected
# ---------------------------------------------------------------------------


class TestGracePeriodUnchanged:
    """The GRACE_PERIOD_SECONDS (30 seconds) is not changed by this fix."""

    def test_within_grace_period_always_alive(self):
        assert not is_dead_missing(elapsed=10, timeout_minutes=None)

    def test_elapsed_zero_is_alive(self):
        assert not is_dead_missing(elapsed=0, timeout_minutes=None)
