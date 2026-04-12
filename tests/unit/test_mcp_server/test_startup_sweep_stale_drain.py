"""
Unit tests for startup sweep stale-notification drain (issue #1355).

Problem: on fresh dispatcher restart, the reconciler's startup sweep re-enqueues
*all* unnotified completed sessions from the past 24 hours — even sessions that
completed tens of minutes ago when the dispatcher was offline. This floods the inbox
with stale completion notices that require multiple WFM cycles to drain, blocking
real user messages.

Fix: introduce STALE_NOTIFICATION_THRESHOLD_MINUTES. During the startup sweep,
sessions whose completed_at is older than this threshold are silently marked notified
(set_notified called) without being enqueued in the inbox. Only sessions that
completed very recently are re-enqueued (genuine crash-between-complete-and-notify race).

The threshold exists because:
- A genuine crash-and-restart cycle takes <60 seconds.
- Sessions older than 10 minutes could not possibly be in the crash window.
- Re-notifying them produces only wasted WFM cycles; the dispatcher cannot usefully
  act on a subagent result that arrived 30 minutes ago.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).parents[3]

for _p in [str(_ROOT / "src" / "mcp"), str(_ROOT / "src" / "agents"),
           str(_ROOT / "src"), str(_ROOT / "src" / "utils")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def inbox_server_module(tmp_path_factory):
    """Load inbox_server with minimal patching."""
    import os
    tmp = tmp_path_factory.mktemp("messages")
    os.environ.setdefault("LOBSTER_MESSAGES", str(tmp / "messages"))
    os.environ.setdefault("LOBSTER_WORKSPACE", str(tmp / "workspace"))
    try:
        if "inbox_server" in sys.modules:
            del sys.modules["inbox_server"]
        import inbox_server as _is
        return _is
    except Exception:
        pytest.skip("inbox_server not importable in this test environment")


def _make_session(agent_id: str, completed_minutes_ago: float) -> dict:
    """Build a minimal completed session dict."""
    completed_at = datetime.now(timezone.utc) - timedelta(minutes=completed_minutes_ago)
    return {
        "id": agent_id,
        "task_id": "task-123",
        "description": "some subagent",
        "chat_id": "8305714125",
        "source": "telegram",
        "status": "completed",
        "output_file": None,
        "input_summary": None,
        "elapsed_seconds": 30,
        "notified_at": None,
        "agent_type": "subagent",
        "completed_at": completed_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Pure-function tests: stale threshold guard
# ---------------------------------------------------------------------------

STALE_NOTIFICATION_THRESHOLD_MINUTES = 10  # must match inbox_server constant


def _is_stale_for_startup(session: dict, threshold_minutes: int) -> bool:
    """Pure function mirroring the staleness check in _startup_sweep.

    Returns True if the session completed more than threshold_minutes ago,
    meaning it should be silently marked notified rather than re-enqueued.
    """
    completed_at_str = session.get("completed_at")
    if not completed_at_str:
        # No completed_at recorded — cannot determine age, treat as fresh (re-enqueue)
        return False
    try:
        completed_at = datetime.fromisoformat(completed_at_str)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - completed_at
        return age.total_seconds() > threshold_minutes * 60
    except (ValueError, TypeError):
        return False  # unparseable timestamp — treat as fresh


class TestIsStaleForStartup:
    """Boundary tests for the stale-session guard used in _startup_sweep."""

    def test_session_completed_1_minute_ago_is_not_stale(self):
        session = _make_session("agent-fresh", completed_minutes_ago=1)
        assert _is_stale_for_startup(session, STALE_NOTIFICATION_THRESHOLD_MINUTES) is False

    def test_session_completed_just_under_threshold_is_not_stale(self):
        # 9 minutes 30 seconds — clearly under the 10-minute threshold
        session = _make_session("agent-under-threshold", completed_minutes_ago=9.5)
        assert _is_stale_for_startup(session, STALE_NOTIFICATION_THRESHOLD_MINUTES) is False

    def test_session_completed_11_minutes_ago_is_stale(self):
        session = _make_session("agent-stale", completed_minutes_ago=11)
        assert _is_stale_for_startup(session, STALE_NOTIFICATION_THRESHOLD_MINUTES) is True

    def test_session_completed_60_minutes_ago_is_stale(self):
        session = _make_session("agent-very-stale", completed_minutes_ago=60)
        assert _is_stale_for_startup(session, STALE_NOTIFICATION_THRESHOLD_MINUTES) is True

    def test_session_with_no_completed_at_is_not_stale(self):
        """Sessions without completed_at are treated as fresh — re-enqueue them."""
        session = dict(_make_session("agent-no-ts", 999), completed_at=None)
        assert _is_stale_for_startup(session, STALE_NOTIFICATION_THRESHOLD_MINUTES) is False

    def test_session_with_missing_completed_at_key_is_not_stale(self):
        """Sessions missing completed_at key entirely are treated as fresh."""
        session = {k: v for k, v in _make_session("agent-no-key", 999).items()
                   if k != "completed_at"}
        assert _is_stale_for_startup(session, STALE_NOTIFICATION_THRESHOLD_MINUTES) is False

    def test_session_with_unparseable_timestamp_is_not_stale(self):
        """Unparseable timestamps are treated as fresh rather than crashing."""
        session = dict(_make_session("agent-bad-ts", 999), completed_at="not-a-timestamp")
        assert _is_stale_for_startup(session, STALE_NOTIFICATION_THRESHOLD_MINUTES) is False


# ---------------------------------------------------------------------------
# Behavior tests: stale vs fresh sessions in _startup_sweep
# ---------------------------------------------------------------------------

class TestStartupSweepStaleSessionDrain:
    """_startup_sweep must silently drain stale sessions without enqueuing them."""

    @pytest.mark.asyncio
    async def test_stale_session_is_not_enqueued(self, inbox_server_module, tmp_path):
        """Sessions completed more than threshold minutes ago are silently drained."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        stale_session = _make_session("agent-stale-001", completed_minutes_ago=30)
        mock_store = MagicMock()
        mock_store.get_unnotified_completed.return_value = [stale_session]

        original = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            with patch.object(inbox_server_module, "_session_store", mock_store):
                with patch.object(
                    inbox_server_module, "_enqueue_reconciler_notification"
                ) as mock_enqueue:
                    await inbox_server_module._startup_sweep()

                    mock_enqueue.assert_not_called(), (
                        "Stale sessions (completed >threshold minutes ago) must not "
                        "be re-enqueued in the inbox — they produce only wasted WFM cycles"
                    )
        finally:
            inbox_server_module.INBOX_DIR = original

    @pytest.mark.asyncio
    async def test_stale_session_gets_set_notified(self, inbox_server_module, tmp_path):
        """Stale sessions must have set_notified() called to prevent re-queuing on next restart."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        stale_session = _make_session("agent-stale-002", completed_minutes_ago=30)
        mock_store = MagicMock()
        mock_store.get_unnotified_completed.return_value = [stale_session]

        original = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            with patch.object(inbox_server_module, "_session_store", mock_store):
                with patch.object(inbox_server_module, "_enqueue_reconciler_notification"):
                    await inbox_server_module._startup_sweep()

                    mock_store.set_notified.assert_called_once_with("agent-stale-002"), (
                        "set_notified must be called for stale sessions so they are not "
                        "re-queued on the next restart"
                    )
        finally:
            inbox_server_module.INBOX_DIR = original

    @pytest.mark.asyncio
    async def test_fresh_session_is_still_enqueued(self, inbox_server_module, tmp_path):
        """Sessions completed within the threshold window are re-enqueued normally."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        fresh_session = _make_session("agent-fresh-001", completed_minutes_ago=1)
        mock_store = MagicMock()
        mock_store.get_unnotified_completed.return_value = [fresh_session]

        original = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            with patch.object(inbox_server_module, "_session_store", mock_store):
                with patch.object(
                    inbox_server_module, "_enqueue_reconciler_notification"
                ) as mock_enqueue:
                    await inbox_server_module._startup_sweep()

                    assert mock_enqueue.call_count == 1, (
                        "Fresh sessions (completed within threshold) must still be "
                        "re-enqueued — they represent a genuine crash-before-notify race"
                    )
        finally:
            inbox_server_module.INBOX_DIR = original

    @pytest.mark.asyncio
    async def test_mixed_batch_drains_stale_enqueues_fresh(self, inbox_server_module, tmp_path):
        """30 stale sessions + 1 fresh session: only the fresh one is enqueued."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        stale_sessions = [
            _make_session(f"agent-stale-batch-{i}", completed_minutes_ago=30 + i)
            for i in range(30)
        ]
        fresh_session = _make_session("agent-fresh-batch", completed_minutes_ago=2)
        all_sessions = stale_sessions + [fresh_session]

        mock_store = MagicMock()
        mock_store.get_unnotified_completed.return_value = all_sessions

        original = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            with patch.object(inbox_server_module, "_session_store", mock_store):
                with patch.object(
                    inbox_server_module, "_enqueue_reconciler_notification"
                ) as mock_enqueue:
                    await inbox_server_module._startup_sweep()

                    assert mock_enqueue.call_count == 1, (
                        f"Only 1 fresh session should be enqueued out of 31 total; "
                        f"got {mock_enqueue.call_count} calls"
                    )
                    # All 30 stale sessions should have set_notified called
                    stale_agent_ids = {s["id"] for s in stale_sessions}
                    notified_ids = {
                        call.args[0] for call in mock_store.set_notified.call_args_list
                    }
                    assert stale_agent_ids == notified_ids, (
                        f"All stale sessions must have set_notified called; "
                        f"missing: {stale_agent_ids - notified_ids}"
                    )
        finally:
            inbox_server_module.INBOX_DIR = original

    @pytest.mark.asyncio
    async def test_stale_drain_count_logged(self, inbox_server_module, tmp_path, caplog):
        """The startup sweep logs how many stale sessions were drained for observability."""
        import logging
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        stale_sessions = [
            _make_session(f"agent-log-stale-{i}", completed_minutes_ago=20)
            for i in range(5)
        ]
        mock_store = MagicMock()
        mock_store.get_unnotified_completed.return_value = stale_sessions

        original = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            with patch.object(inbox_server_module, "_session_store", mock_store):
                with patch.object(inbox_server_module, "_enqueue_reconciler_notification"):
                    with caplog.at_level(logging.INFO):
                        await inbox_server_module._startup_sweep()
        finally:
            inbox_server_module.INBOX_DIR = original

        # Verify a log message mentions the stale drain
        log_text = " ".join(caplog.messages)
        assert "stale" in log_text.lower() or "drain" in log_text.lower() or "5" in log_text, (
            "Startup sweep should log the number of stale sessions drained; "
            f"got log messages: {caplog.messages}"
        )

    @pytest.mark.asyncio
    async def test_session_without_completed_at_is_treated_as_fresh(
        self, inbox_server_module, tmp_path
    ):
        """Sessions missing completed_at are re-enqueued (treated as fresh — safe default)."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        session_no_ts = {k: v for k, v in _make_session("agent-no-ts", 999).items()
                         if k != "completed_at"}
        mock_store = MagicMock()
        mock_store.get_unnotified_completed.return_value = [session_no_ts]

        original = inbox_server_module.INBOX_DIR
        inbox_server_module.INBOX_DIR = inbox_dir
        try:
            with patch.object(inbox_server_module, "_session_store", mock_store):
                with patch.object(
                    inbox_server_module, "_enqueue_reconciler_notification"
                ) as mock_enqueue:
                    await inbox_server_module._startup_sweep()

                    # No completed_at → treated as fresh → enqueued
                    assert mock_enqueue.call_count == 1, (
                        "Sessions without completed_at must be treated as fresh "
                        "and re-enqueued (safe default)"
                    )
        finally:
            inbox_server_module.INBOX_DIR = original


# ---------------------------------------------------------------------------
# Constant verification
# ---------------------------------------------------------------------------

class TestStartupSweepThresholdConstant:
    """Verify STALE_NOTIFICATION_THRESHOLD_MINUTES is defined and has the expected value."""

    def test_constant_exists_in_inbox_server(self, inbox_server_module):
        assert hasattr(inbox_server_module, "STALE_NOTIFICATION_THRESHOLD_MINUTES"), (
            "inbox_server must export STALE_NOTIFICATION_THRESHOLD_MINUTES "
            "(required by startup sweep stale-drain fix, issue #1355)"
        )

    def test_constant_is_10_minutes(self, inbox_server_module):
        val = inbox_server_module.STALE_NOTIFICATION_THRESHOLD_MINUTES
        assert val == 10, (
            f"STALE_NOTIFICATION_THRESHOLD_MINUTES should be 10 (minutes); got {val!r}. "
            "10 minutes is the spec value from issue #1355: sessions completed more than "
            "10 minutes ago cannot be in the genuine crash-before-notify window."
        )
