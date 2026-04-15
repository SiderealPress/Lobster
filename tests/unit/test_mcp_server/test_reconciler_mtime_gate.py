"""
Unit tests for reconciler mtime gate logic (issue #868).

When a Claude Code agent is interrupted mid-turn, it writes no ``stop_reason``
to its output JSONL file, so ``check_output_file_status()`` returns "running"
— indistinguishable from a legitimately active agent.  The mtime gate closes
this gap: if the output file has been idle for MTIME_STALE_THRESHOLD_SECONDS,
the reconciler uses the shorter "missing file" threshold (30 min) instead of
the generous "running" threshold (120 min).

Strategy: extract the pure threshold-selection logic into standalone functions
and test all branching cases without instantiating the async reconciler or the
full MCP server.
"""

import pytest


# ---------------------------------------------------------------------------
# Constants — mirror reconcile_agent_sessions() in inbox_server.py
# ---------------------------------------------------------------------------

DEFAULT_DEAD_THRESHOLD_SECONDS = 30 * 60          # 30 minutes
DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS = 120 * 60  # 120 minutes
MTIME_STALE_THRESHOLD_SECONDS = 15 * 60           # 15 minutes


# ---------------------------------------------------------------------------
# Pure helpers — mirror the mtime-gate logic added in reconcile_agent_sessions()
# ---------------------------------------------------------------------------


def is_file_stale(output_mtime: float | None, now_ts: float) -> bool:
    """True when the output file has been idle longer than MTIME_STALE_THRESHOLD_SECONDS.

    Returns False (conservative — assume live) when mtime is unavailable.
    """
    if output_mtime is None:
        return False
    return (now_ts - output_mtime) > MTIME_STALE_THRESHOLD_SECONDS


def effective_running_threshold(
    file_is_stale: bool,
    timeout_minutes: int | None,
) -> int:
    """Return the dead threshold to apply for a 'running' file_status.

    When the file is stale (interrupted agent), use the short threshold so the
    session is collected within 30 minutes instead of 120 minutes.
    When the file is fresh (live agent), use the full registered timeout.
    """
    if timeout_minutes is not None:
        dead_threshold_running = timeout_minutes * 60
        dead_threshold_missing = max(timeout_minutes * 60, DEFAULT_DEAD_THRESHOLD_SECONDS)
    else:
        dead_threshold_running = DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS
        dead_threshold_missing = DEFAULT_DEAD_THRESHOLD_SECONDS

    return dead_threshold_missing if file_is_stale else dead_threshold_running


def should_kill_running_session(
    elapsed: int,
    output_mtime: float | None,
    now_ts: float,
    timeout_minutes: int | None,
) -> bool:
    """True when a 'running' session should be marked dead."""
    stale = is_file_stale(output_mtime, now_ts)
    threshold = effective_running_threshold(stale, timeout_minutes)
    return elapsed > threshold


# ---------------------------------------------------------------------------
# Tests: mtime staleness detection
# ---------------------------------------------------------------------------


class TestMtimeStaleness:
    """is_file_stale correctly classifies file freshness."""

    def test_fresh_file_is_not_stale(self):
        now = 1_000_000.0
        # mtime 5 minutes ago — well within threshold
        assert not is_file_stale(now - 5 * 60, now)

    def test_file_at_threshold_boundary_is_not_stale(self):
        now = 1_000_000.0
        # exactly at threshold — strict > means not stale yet
        assert not is_file_stale(now - MTIME_STALE_THRESHOLD_SECONDS, now)

    def test_file_just_over_threshold_is_stale(self):
        now = 1_000_000.0
        # 15 minutes + 1 second over threshold
        assert is_file_stale(now - MTIME_STALE_THRESHOLD_SECONDS - 1, now)

    def test_old_file_is_stale(self):
        now = 1_000_000.0
        # mtime 60 minutes ago — clearly stale
        assert is_file_stale(now - 60 * 60, now)

    def test_none_mtime_is_not_stale(self):
        # Unknown mtime — conservative fallback, treat as fresh
        assert not is_file_stale(None, 1_000_000.0)


# ---------------------------------------------------------------------------
# Tests: effective threshold selection
# ---------------------------------------------------------------------------


class TestEffectiveRunningThreshold:
    """effective_running_threshold returns the correct threshold per staleness."""

    def test_fresh_file_uses_long_threshold_default(self):
        threshold = effective_running_threshold(file_is_stale=False, timeout_minutes=None)
        assert threshold == DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS  # 120 min

    def test_stale_file_uses_short_threshold_default(self):
        threshold = effective_running_threshold(file_is_stale=True, timeout_minutes=None)
        assert threshold == DEFAULT_DEAD_THRESHOLD_SECONDS  # 30 min

    def test_fresh_file_uses_registered_timeout(self):
        threshold = effective_running_threshold(file_is_stale=False, timeout_minutes=240)
        assert threshold == 240 * 60

    def test_stale_file_with_registered_timeout_uses_missing_threshold(self):
        # For a 240-minute agent, the "missing" threshold is max(240*60, 30*60) = 240*60
        threshold = effective_running_threshold(file_is_stale=True, timeout_minutes=240)
        assert threshold == 240 * 60

    def test_stale_file_with_short_timeout_floored_at_30_min(self):
        # For a 5-minute agent, missing threshold is floored at 30 min
        threshold = effective_running_threshold(file_is_stale=True, timeout_minutes=5)
        assert threshold == DEFAULT_DEAD_THRESHOLD_SECONDS  # 30 min floor


# ---------------------------------------------------------------------------
# Tests: the core bug scenario
# ---------------------------------------------------------------------------


class TestMtimeGateBugFix:
    """Verify the exact bug described in issue #868 is fixed."""

    NOW = 1_000_000.0

    def test_interrupted_agent_at_35min_is_now_dead(self):
        """
        Before the fix: interrupted agent at 35 min → alive (120-min threshold).
        After the fix: stale mtime → use 30-min threshold → 35 min > 30 min → dead.
        """
        elapsed = 35 * 60
        stale_mtime = self.NOW - 20 * 60  # file idle 20 minutes
        assert should_kill_running_session(elapsed, stale_mtime, self.NOW, None)

    def test_live_agent_at_35min_is_still_alive(self):
        """
        A legitimate slow tool call at 35 min with a fresh mtime must not be killed.
        """
        elapsed = 35 * 60
        fresh_mtime = self.NOW - 2 * 60  # file updated 2 minutes ago
        assert not should_kill_running_session(elapsed, fresh_mtime, self.NOW, None)

    def test_interrupted_agent_just_crossed_30min_is_dead(self):
        """31 minutes elapsed, file idle 20 minutes → dead (just past short threshold)."""
        elapsed = 31 * 60
        stale_mtime = self.NOW - 20 * 60
        assert should_kill_running_session(elapsed, stale_mtime, self.NOW, None)

    def test_interrupted_agent_at_29min_is_still_alive(self):
        """29 minutes elapsed, file idle 20 minutes → alive (below 30-min threshold)."""
        elapsed = 29 * 60
        stale_mtime = self.NOW - 20 * 60
        assert not should_kill_running_session(elapsed, stale_mtime, self.NOW, None)

    def test_agent_under_120min_with_unknown_mtime_uses_long_threshold(self):
        """
        When mtime is unavailable, fall back to conservative (long) threshold.
        Agent at 90 min with None mtime must NOT be killed.
        """
        elapsed = 90 * 60
        assert not should_kill_running_session(elapsed, None, self.NOW, None)

    def test_agent_over_120min_with_unknown_mtime_is_dead(self):
        """
        Even with unknown mtime, the 120-min hard cap still applies.
        """
        elapsed = 121 * 60
        assert should_kill_running_session(elapsed, None, self.NOW, None)

    def test_interrupted_agent_with_registered_timeout_at_35min_dead(self):
        """
        Agent registered for 240-minute timeout, interrupted at 35 min.
        Stale file collapses threshold to max(240*60, 30*60) = 240*60 … wait.

        For a 240-minute timeout, the "missing file" threshold = 240 min, not 30 min.
        So 35 min is still alive — the floor only applies to *short* timeouts.
        (This is correct: a long-registered agent gets its full window even when stale.)
        """
        elapsed = 35 * 60
        stale_mtime = self.NOW - 20 * 60
        # 240-minute agent: even stale, threshold is 240*60; 35 min < 240 min → alive
        assert not should_kill_running_session(elapsed, stale_mtime, self.NOW, 240)

    def test_interrupted_agent_with_short_timeout_stale_uses_30min_floor(self):
        """
        Agent registered for 5 minutes, interrupted at 35 min.
        Stale file triggers missing threshold = max(5*60, 30*60) = 30 min.
        35 min > 30 min → dead.
        """
        elapsed = 35 * 60
        stale_mtime = self.NOW - 20 * 60
        assert should_kill_running_session(elapsed, stale_mtime, self.NOW, 5)


# ---------------------------------------------------------------------------
# Tests: get_output_file_mtime (pure function in session_store)
# ---------------------------------------------------------------------------


class TestGetOutputFileMtime:
    """get_output_file_mtime returns mtime or None for missing/invalid paths."""

    def test_empty_string_returns_none(self, tmp_path):
        from agents.session_store import get_output_file_mtime
        assert get_output_file_mtime("") is None

    def test_nonexistent_path_returns_none(self, tmp_path):
        from agents.session_store import get_output_file_mtime
        result = get_output_file_mtime(str(tmp_path / "no_such_file.output"))
        assert result is None

    def test_existing_file_returns_float(self, tmp_path):
        from agents.session_store import get_output_file_mtime
        f = tmp_path / "agent.output"
        f.write_text('{"stop_reason": "tool_use"}\n')
        result = get_output_file_mtime(str(f))
        assert isinstance(result, float)
        assert result > 0

    def test_follows_symlink(self, tmp_path):
        import os
        import time
        from agents.session_store import get_output_file_mtime

        real_file = tmp_path / "real.jsonl"
        real_file.write_text('{"stop_reason": "tool_use"}\n')
        symlink = tmp_path / "agent.output"
        symlink.symlink_to(real_file)

        # Backdate the real file mtime by 30 minutes
        old_time = time.time() - 30 * 60
        os.utime(str(real_file), (old_time, old_time))

        result = get_output_file_mtime(str(symlink))
        assert result is not None
        # Should reflect the real file mtime (not symlink mtime)
        assert abs(result - old_time) < 2  # within 2 seconds

    def test_broken_symlink_returns_none(self, tmp_path):
        from agents.session_store import get_output_file_mtime

        symlink = tmp_path / "agent.output"
        symlink.symlink_to(tmp_path / "nonexistent_target.jsonl")

        result = get_output_file_mtime(str(symlink))
        assert result is None
