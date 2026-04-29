"""
Tests for dashboard collect_health() heartbeat logic.

Verifies that:
- collect_health() reads dispatcher-heartbeat (not claude-heartbeat) — the
  authoritative "dispatcher alive" signal written by thinking-heartbeat.py on
  every PostToolUse event.
- heartbeat_stale uses DISPATCHER_HEARTBEAT_STALE_SECONDS (1200s / 20 min), not
  the old 300s threshold that caused false-positive STALE during compaction phases.

Issue: #1822 — frozen-dispatcher threshold (5min) may misfire during long compaction phases.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

# We need the dashboard module on sys.path
import sys
import os

LOBSTER_SRC = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(LOBSTER_SRC))


def _make_fresh_heartbeat_file(tmp_path: Path, age_seconds: int) -> Path:
    """Create a heartbeat file with the given age (mtime set to now - age)."""
    hb_file = tmp_path / "dispatcher-heartbeat"
    hb_file.write_text(str(int(time.time() - age_seconds)) + "\n")
    # Touch the file to set mtime precisely
    target_mtime = time.time() - age_seconds
    os.utime(str(hb_file), (target_mtime, target_mtime))
    return hb_file


class TestCollectHealthHeartbeatFile:
    """collect_health() must read dispatcher-heartbeat, not claude-heartbeat."""

    def test_reads_dispatcher_heartbeat_file(self, tmp_path):
        """Heartbeat age is measured from the dispatcher-heartbeat file."""
        dispatcher_hb = _make_fresh_heartbeat_file(tmp_path, age_seconds=30)
        old_claude_hb = tmp_path / "claude-heartbeat"
        # claude-heartbeat is very stale (older than any threshold)
        old_claude_hb.write_text("0\n")
        os.utime(str(old_claude_hb), (1.0, 1.0))

        with patch("dashboard.collectors._WORKSPACE", tmp_path):
            # Make sure the logs subdir exists so Path construction works
            (tmp_path / "logs").mkdir(exist_ok=True)
            dispatcher_hb_in_logs = tmp_path / "logs" / "dispatcher-heartbeat"
            dispatcher_hb_in_logs.write_text(str(int(time.time() - 30)) + "\n")
            os.utime(str(dispatcher_hb_in_logs), (time.time() - 30, time.time() - 30))

            from dashboard import collectors
            # Patch psutil to avoid actual process scanning
            with patch.object(collectors.psutil, "process_iter", return_value=[]):
                result = collectors.collect_health()

        # Age should be ~30s (from dispatcher-heartbeat), not years (from claude-heartbeat)
        assert result["heartbeat_age_seconds"] is not None
        assert result["heartbeat_age_seconds"] < 60, (
            "heartbeat_age should reflect dispatcher-heartbeat (~30s), "
            f"got {result['heartbeat_age_seconds']}s — "
            "collect_health() is likely still reading claude-heartbeat"
        )

    def test_absent_dispatcher_heartbeat_returns_none(self, tmp_path):
        """When dispatcher-heartbeat is absent, heartbeat_age_seconds is None."""
        (tmp_path / "logs").mkdir(exist_ok=True)
        # No dispatcher-heartbeat file

        with patch("dashboard.collectors._WORKSPACE", tmp_path):
            from dashboard import collectors
            with patch.object(collectors.psutil, "process_iter", return_value=[]):
                result = collectors.collect_health()

        assert result["heartbeat_age_seconds"] is None
        assert result["heartbeat_stale"] is False


class TestCollectHealthStaleThreshold:
    """heartbeat_stale must use the 20-minute threshold, not the 5-minute one.

    The 5-minute threshold caused false-positive STALE during compaction phases,
    which can last 10+ minutes. The authoritative threshold is
    DISPATCHER_HEARTBEAT_STALE_SECONDS = 1200 from thinking-heartbeat.py.
    """

    # Aligned with DISPATCHER_HEARTBEAT_STALE_SECONDS in thinking-heartbeat.py
    DISPATCHER_HEARTBEAT_STALE_SECONDS = 1200

    def _collect_with_age(self, tmp_path: Path, age_seconds: int) -> dict:
        (tmp_path / "logs").mkdir(exist_ok=True)
        hb = tmp_path / "logs" / "dispatcher-heartbeat"
        hb.write_text(str(int(time.time() - age_seconds)) + "\n")
        os.utime(str(hb), (time.time() - age_seconds, time.time() - age_seconds))

        with patch("dashboard.collectors._WORKSPACE", tmp_path):
            from dashboard import collectors
            with patch.object(collectors.psutil, "process_iter", return_value=[]):
                return collectors.collect_health()

    def test_not_stale_during_compaction_window(self, tmp_path):
        """A 10-minute-old heartbeat must NOT be stale — compaction can last this long."""
        COMPACTION_MAX_DURATION_SECONDS = 10 * 60  # 10 minutes
        result = self._collect_with_age(tmp_path, age_seconds=COMPACTION_MAX_DURATION_SECONDS)

        assert result["heartbeat_stale"] is False, (
            f"heartbeat_stale should be False for a {COMPACTION_MAX_DURATION_SECONDS}s-old "
            "heartbeat (dispatcher may be in compaction). "
            f"Got heartbeat_stale=True — threshold is still too low "
            f"(old 300s threshold fires false positives during compaction)"
        )

    def test_not_stale_at_old_5min_threshold(self, tmp_path):
        """Heartbeat at 6 minutes old must NOT be stale.

        The old threshold (300s / 5min) would mark this stale. The new threshold
        (1200s / 20min) must not.
        """
        age_just_past_old_threshold = 360  # 6 minutes
        result = self._collect_with_age(tmp_path, age_seconds=age_just_past_old_threshold)

        assert result["heartbeat_stale"] is False, (
            f"heartbeat_stale should be False at {age_just_past_old_threshold}s "
            "(old 5-min threshold was too aggressive — compaction takes 10+ min). "
            "This is the false-positive that issue #1822 reports."
        )

    def test_stale_past_20min_threshold(self, tmp_path):
        """Heartbeat older than 20 minutes IS stale — dispatcher is genuinely dead."""
        age_past_threshold = self.DISPATCHER_HEARTBEAT_STALE_SECONDS + 60  # 1260s
        result = self._collect_with_age(tmp_path, age_seconds=age_past_threshold)

        assert result["heartbeat_stale"] is True, (
            f"heartbeat_stale should be True at {age_past_threshold}s "
            f"(exceeds DISPATCHER_HEARTBEAT_STALE_SECONDS={self.DISPATCHER_HEARTBEAT_STALE_SECONDS}s)"
        )

    def test_not_stale_just_below_20min_threshold(self, tmp_path):
        """Heartbeat just below 20 minutes is NOT stale."""
        age_just_below = self.DISPATCHER_HEARTBEAT_STALE_SECONDS - 60  # 1140s
        result = self._collect_with_age(tmp_path, age_seconds=age_just_below)

        assert result["heartbeat_stale"] is False, (
            f"heartbeat_stale should be False at {age_just_below}s "
            f"(below DISPATCHER_HEARTBEAT_STALE_SECONDS={self.DISPATCHER_HEARTBEAT_STALE_SECONDS}s)"
        )


class TestCollectHealthConstantAlignment:
    """HEARTBEAT_STALE_THRESHOLD in collectors.py must match thinking-heartbeat.py."""

    def test_heartbeat_stale_threshold_constant_exists(self):
        """collectors.py must export HEARTBEAT_STALE_THRESHOLD as a named constant."""
        from dashboard import collectors
        assert hasattr(collectors, "HEARTBEAT_STALE_THRESHOLD"), (
            "collectors.py should export HEARTBEAT_STALE_THRESHOLD "
            "so readers can verify it aligns with thinking-heartbeat.py's "
            "DISPATCHER_HEARTBEAT_STALE_SECONDS=1200"
        )

    def test_heartbeat_stale_threshold_equals_1200(self):
        """HEARTBEAT_STALE_THRESHOLD must equal 1200 (matching thinking-heartbeat.py)."""
        from dashboard import collectors
        assert collectors.HEARTBEAT_STALE_THRESHOLD == 1200, (
            f"Expected HEARTBEAT_STALE_THRESHOLD=1200 (aligned with "
            f"thinking-heartbeat.py DISPATCHER_HEARTBEAT_STALE_SECONDS=1200), "
            f"got {collectors.HEARTBEAT_STALE_THRESHOLD}"
        )
