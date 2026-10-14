"""
Unit tests for the reconciler flood fix (issue #1432).

Bug: write_result called session_end() but never called set_notified(), leaving
notified_at=NULL permanently.  On every MCP restart the startup sweep re-enqueued
every completed session with notified_at IS NULL — causing the April 4 flood of 30
stale notifications.

Fix: handle_write_result now calls set_notified(task_id) immediately after
session_end() succeeds.

These tests cover the three session-completion paths:

  1. Normal path  — write_result is called:
       session_end() + set_notified() both fire inside write_result.
       Startup sweep sees notified_at IS NOT NULL → skips. No flood.

  2. Crash path — agent dies before write_result:
       write_result never called → session_end and set_notified never called by
       write_result.  Reconciler detects dead session → calls
       _enqueue_reconciler_notification → sets notified_at.  This existing path
       is unchanged; these tests verify write_result does not interfere.

  3. Edge case — set_notified raises after session_end succeeds:
       The exception is swallowed by the surrounding try/except so the inbox
       file write still completes.  The _inbox_already_has_agent guard prevents
       double-enqueue on next restart.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup: ensure src/mcp and src/agents are importable
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parents[4]
for _p in [str(_ROOT / "src" / "mcp"), str(_ROOT / "src" / "agents"),
           str(_ROOT / "src"), str(_ROOT / "src" / "utils")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import src.mcp.inbox_server  # noqa: F401 — pre-load so patch.multiple resolves it


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dirs(tmp_path: Path):
    """Return (inbox, outbox, sent, sent_replies) directories under tmp_path."""
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent"
    sent_replies = tmp_path / "sent-replies"
    for d in (inbox, outbox, sent, sent_replies):
        d.mkdir(parents=True, exist_ok=True)
    return inbox, outbox, sent, sent_replies


def _mock_session_store() -> MagicMock:
    """Return a mock _session_store with all relevant methods."""
    store = MagicMock()
    store.session_end.return_value = None
    store.set_notified.return_value = None
    return store


# ---------------------------------------------------------------------------
# Path 1: Normal path — write_result called successfully
# ---------------------------------------------------------------------------

class TestWriteResultSetsNotified:
    """write_result must call set_notified after session_end on the normal path."""

    def test_set_notified_called_after_session_end(self, tmp_path):
        """set_notified is called with the task_id after a successful write_result."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "issue-1432-test",
                "chat_id": 12345,
                "text": "PR is open.",
                "source": "telegram",
            }))

        mock_store.session_end.assert_called_once()
        mock_store.set_notified.assert_called_once_with("issue-1432-test")

    def test_set_notified_called_with_correct_task_id(self, tmp_path):
        """set_notified receives exactly the task_id passed to write_result."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "my-specific-task-abc",
                "chat_id": 99999,
                "text": "Done.",
                "source": "telegram",
            }))

        # set_notified must use the task_id, not some other identifier
        set_notified_call = mock_store.set_notified.call_args
        assert set_notified_call == call("my-specific-task-abc"), (
            f"set_notified called with wrong arg: {set_notified_call}"
        )

    def test_set_notified_called_after_session_end_not_before(self, tmp_path):
        """set_notified is called after session_end (ordering guarantee)."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)

        call_order = []
        mock_store = MagicMock()
        mock_store.session_end.side_effect = lambda **kw: call_order.append("session_end")
        mock_store.set_notified.side_effect = lambda *a: call_order.append("set_notified")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "ordering-test",
                "chat_id": 12345,
                "text": "Done.",
                "source": "telegram",
            }))

        assert call_order == ["session_end", "set_notified"], (
            f"Expected session_end before set_notified, got: {call_order}"
        )

    def test_inbox_file_written_even_when_session_end_raises(self, tmp_path):
        """If session_end raises, the inbox file is still written (fault tolerance).

        set_notified is not called when session_end raises (it's in the same try block).
        The inbox guard (_inbox_already_has_agent) protects against double-enqueue.
        """
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = MagicMock()
        mock_store.session_end.side_effect = RuntimeError("DB locked")
        mock_store.set_notified.return_value = None

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "session-end-raises",
                "chat_id": 12345,
                "text": "Done.",
                "source": "telegram",
            }))

        # Inbox file was still written despite session_end failing
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) == 1, "Inbox file must be written even when session_end raises"

        # set_notified was not called (exception aborted the try block)
        mock_store.set_notified.assert_not_called()


# ---------------------------------------------------------------------------
# Path 2: Crash path — session_end raises; set_notified must not be called
# ---------------------------------------------------------------------------

class TestCrashPathSetNotifiedNotCalled:
    """When session_end raises, set_notified must not be called.

    This preserves the crash path: reconciler detects dead session → calls
    set_notified via _enqueue_reconciler_notification. If write_result's
    set_notified ran despite session_end failing, we'd set notified_at for a
    session the reconciler hasn't yet processed — hiding the crash from the user.
    """

    def test_set_notified_skipped_when_session_end_raises(self, tmp_path):
        """set_notified is not called when session_end raises an exception."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = MagicMock()
        mock_store.session_end.side_effect = RuntimeError("simulated failure")
        mock_store.set_notified.return_value = None

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "crash-path-task",
                "chat_id": 12345,
                "text": "Done.",
                "source": "telegram",
            }))

        mock_store.set_notified.assert_not_called()


# ---------------------------------------------------------------------------
# Path 3: Edge case — set_notified raises after session_end succeeds
# ---------------------------------------------------------------------------

class TestEdgeCaseSetNotifiedRaises:
    """Even when set_notified raises, the inbox file must be written.

    This represents the tiny window where MCP crashes between session_end and
    set_notified.  The _inbox_already_has_agent guard in _startup_sweep prevents
    double-enqueue.  The write_result call must not surface an error to the caller.
    """

    def test_inbox_file_written_when_set_notified_raises(self, tmp_path):
        """Inbox file is written even if set_notified raises after session_end."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = MagicMock()
        mock_store.session_end.return_value = None
        mock_store.set_notified.side_effect = RuntimeError("set_notified DB error")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            # Should not raise
            result = asyncio.run(handle_write_result({
                "task_id": "edge-case-task",
                "chat_id": 12345,
                "text": "Done.",
                "source": "telegram",
            }))

        # The result must be a success (not an error propagated to caller)
        assert result is not None
        # Inbox file was still written
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) == 1, (
            "Inbox file must be written even if set_notified raises"
        )
        msg = json.loads(inbox_files[0].read_text())
        assert msg["task_id"] == "edge-case-task"


# ---------------------------------------------------------------------------
# Regression: startup sweep must skip sessions with notified_at set
# ---------------------------------------------------------------------------

class TestStartupSweepSkipsNotifiedSessions:
    """get_unnotified_completed must not return sessions where notified_at is set.

    This is a unit test of the session_store query logic — it verifies that
    after set_notified is called, the session no longer appears in the startup
    sweep candidate list.
    """

    def test_notified_session_not_returned_by_get_unnotified_completed(self, tmp_path):
        """A session with notified_at set is excluded from startup sweep results.

        Uses a real SQLite in-memory DB via session_store to verify the query,
        not just the mock.
        """
        from src.agents import session_store as ss

        db_path = tmp_path / "test.db"
        ss.init_db(db_path)

        # Register a session
        ss.session_start(
            id="test-agent-notified",
            task_id="test-task-notified",
            description="Test session",
            chat_id="12345",
            source="telegram",
            path=db_path,
        )

        # Complete it (status=completed, notified_at=NULL)
        ss.session_end(
            id_or_task_id="test-agent-notified",
            status="completed",
            stop_reason="end_turn",
            path=db_path,
        )

        # Before set_notified: session appears in unnotified list
        unnotified_before = ss.get_unnotified_completed(since_hours=24, path=db_path)
        task_ids_before = [s.get("task_id") for s in unnotified_before]
        assert "test-task-notified" in task_ids_before, (
            "Session must appear in unnotified list before set_notified is called"
        )

        # Call set_notified (the fix)
        ss.set_notified("test-agent-notified", path=db_path)

        # After set_notified: session must NOT appear in unnotified list
        unnotified_after = ss.get_unnotified_completed(since_hours=24, path=db_path)
        task_ids_after = [s.get("task_id") for s in unnotified_after]
        assert "test-task-notified" not in task_ids_after, (
            "Session must NOT appear in unnotified list after set_notified is called — "
            "this is the root cause of the April 4 flood"
        )

    def test_unnotified_session_still_returned_before_set_notified(self, tmp_path):
        """A completed session with notified_at=NULL appears in startup sweep.

        Verifies the before-fix state: if set_notified were never called (old
        behavior), the session would be re-enqueued on every restart.
        """
        from src.agents import session_store as ss

        db_path = tmp_path / "test-before.db"
        ss.init_db(db_path)

        ss.session_start(
            id="unnotified-agent",
            task_id="unnotified-task",
            description="Unnotified test session",
            chat_id="12345",
            source="telegram",
            path=db_path,
        )
        ss.session_end(
            id_or_task_id="unnotified-agent",
            status="completed",
            stop_reason="end_turn",
            path=db_path,
        )

        # set_notified is intentionally NOT called (simulates old behavior)
        unnotified = ss.get_unnotified_completed(since_hours=24, path=db_path)
        task_ids = [s.get("task_id") for s in unnotified]
        assert "unnotified-task" in task_ids, (
            "Completed session with notified_at=NULL must appear in startup sweep — "
            "this is the bug that caused the April 4 flood"
        )


# ---------------------------------------------------------------------------
# Reused task_id: write_result must only mark the session that is finishing
# ---------------------------------------------------------------------------

# A task_id that is legitimately reused across spawns (agent definitions with a
# hardcoded task_id, scheduled jobs with a stable id).
REUSED_TASK_ID = "fixed-id"
HISTORICAL_NOTIFIED_AT = "2000-01-01T00:00:00+00:00"


def _seed_session(store, db_path: Path, *, id: str, task_id: str, spawned_at: str,
                  status: str = "running", notified_at: str | None = None) -> None:
    """Insert a session row and pin its spawned_at / status / notified_at."""
    store.session_start(
        id=id,
        task_id=task_id,
        description="fixture session",
        chat_id="1000001",
        source="telegram",
        path=db_path,
    )
    conn = store._get_connection(db_path)
    conn.execute(
        "UPDATE agent_sessions SET spawned_at = ?, status = ?, notified_at = ? WHERE id = ?",
        (spawned_at, status, notified_at, id),
    )
    conn.commit()


def _row(store, db_path: Path, id: str) -> dict:
    conn = store._get_connection(db_path)
    return dict(conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (id,)).fetchone())


class TestReusedTaskIdScope:
    """A repeated task_id must not rewrite notified_at on historical rows."""

    def test_bare_task_id_does_not_rewrite_completed_row(self, tmp_path):
        """write_result(task_id=X) leaves an older, already-notified row with
        task_id=X untouched and marks only the newer running row."""
        import src.mcp.inbox_server as srv
        store = srv._session_store

        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        db_path = tmp_path / "agent_sessions.db"
        store.init_db(db_path)

        _seed_session(store, db_path, id="agent-fixture-old", task_id=REUSED_TASK_ID,
                      spawned_at="2000-01-01T00:00:00+00:00", status="completed",
                      notified_at=HISTORICAL_NOTIFIED_AT)
        _seed_session(store, db_path, id="agent-fixture-new", task_id=REUSED_TASK_ID,
                      spawned_at="2000-01-02T00:00:00+00:00", status="running")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
        ), patch.object(store, "_DEFAULT_DB_PATH", db_path):
            asyncio.run(srv.handle_write_result({
                "task_id": REUSED_TASK_ID,
                "chat_id": 1000001,
                "text": "Done.",
                "source": "telegram",
            }))

        old = _row(store, db_path, "agent-fixture-old")
        new = _row(store, db_path, "agent-fixture-new")
        assert old["notified_at"] == HISTORICAL_NOTIFIED_AT, (
            "write_result must not rewrite notified_at on a historical row sharing the task_id"
        )
        assert new["status"] == "completed"
        assert new["notified_at"] is not None, "the finishing session must be marked notified"

    def test_session_end_zero_rows_warns(self, tmp_path, caplog):
        """write_result for a task_id that matches no running row logs a WARNING
        naming the task_id instead of silently no-op'ing."""
        import logging
        import src.mcp.inbox_server as srv
        store = srv._session_store

        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        db_path = tmp_path / "agent_sessions.db"
        store.init_db(db_path)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
        ), patch.object(store, "_DEFAULT_DB_PATH", db_path), \
                caplog.at_level(logging.WARNING):
            asyncio.run(srv.handle_write_result({
                "task_id": "unmatched-fixture-id",
                "chat_id": 1000001,
                "text": "Done.",
                "source": "telegram",
            }))

        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and "unmatched-fixture-id" in r.getMessage()]
        assert warnings, "a WARNING naming the unmatched task_id must be logged"

