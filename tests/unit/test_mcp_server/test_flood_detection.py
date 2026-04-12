"""
Unit tests for inbox flood detection and auto-drain (issue #1420).

Tests the _is_reconciler_ghost() predicate and related flood detection
state functions without requiring a running inbox_server instance.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[3]

for _p in [str(_ROOT / "src" / "mcp"), str(_ROOT / "src" / "agents"),
           str(_ROOT / "src"), str(_ROOT / "src" / "utils")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Fixture: load the inbox_server module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def inbox_server(tmp_path_factory):
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


# ---------------------------------------------------------------------------
# Tests for _is_reconciler_ghost()
# ---------------------------------------------------------------------------

class TestIsReconcilerGhost:
    """_is_reconciler_ghost() identifies stale completion notices safely."""

    def test_reconciler_id_with_short_elapsed(self, inbox_server):
        """Classic reconciler ghost: id contains 'reconciler' + elapsed < 30s."""
        msg = {
            "id": "1234567890_reconciler_fix-something-123",
            "type": "subagent_result",
            "elapsed_seconds": 5,
            "sent_reply_to_user": False,
        }
        assert inbox_server._is_reconciler_ghost(msg)

    def test_reconciler_id_with_zero_elapsed(self, inbox_server):
        """Zero elapsed (startup flush) is also a ghost."""
        msg = {
            "id": "1234567890_reconciler_abc",
            "type": "subagent_result",
            "elapsed_seconds": 0,
            "sent_reply_to_user": False,
        }
        assert inbox_server._is_reconciler_ghost(msg)

    def test_reconciler_id_with_long_elapsed_is_not_ghost(self, inbox_server):
        """Long elapsed time means the agent actually ran — not a ghost."""
        msg = {
            "id": "1234567890_reconciler_abc",
            "type": "subagent_result",
            "elapsed_seconds": 600,  # 10 minutes
            "sent_reply_to_user": False,
        }
        assert not inbox_server._is_reconciler_ghost(msg)

    def test_non_subagent_type_is_not_ghost(self, inbox_server):
        """Only subagent_result and subagent_notification are ghost candidates."""
        msg = {
            "id": "1234567890_reconciler_abc",
            "type": "text",
            "elapsed_seconds": 0,
        }
        assert not inbox_server._is_reconciler_ghost(msg)

    def test_agent_failed_is_not_ghost(self, inbox_server):
        """agent_failed messages are not auto-drained."""
        msg = {
            "id": "1234567890_reconciler_abc",
            "type": "agent_failed",
            "elapsed_seconds": 0,
        }
        assert not inbox_server._is_reconciler_ghost(msg)

    def test_no_elapsed_no_sent_reply_is_not_ghost(self, inbox_server):
        """Missing elapsed + not yet replied → conservative: not a ghost."""
        msg = {
            "id": "1234567890_reconciler_abc",
            "type": "subagent_result",
            # No elapsed_seconds field
            "sent_reply_to_user": False,
        }
        assert not inbox_server._is_reconciler_ghost(msg)

    def test_no_elapsed_but_sent_reply_is_ghost_candidate(self, inbox_server):
        """No elapsed but sent_reply_to_user=True → safe to drain."""
        msg = {
            "id": "1234567890_reconciler_abc",
            "type": "subagent_notification",
            # No elapsed_seconds field
            "sent_reply_to_user": True,
        }
        assert inbox_server._is_reconciler_ghost(msg)

    def test_outside_startup_grace_is_not_ghost(self, inbox_server, monkeypatch):
        """Ghost detection is suppressed after startup grace period."""
        # Simulate a server that started well before the grace period
        old_start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(inbox_server, "_SERVER_START_TIME", old_start)
        msg = {
            "id": "1234567890_reconciler_abc",
            "type": "subagent_result",
            "elapsed_seconds": 5,
        }
        assert not inbox_server._is_reconciler_ghost(msg)

    def test_normal_subagent_result_not_ghost(self, inbox_server):
        """Normal subagent_result without 'reconciler' in id and long elapsed is safe."""
        msg = {
            "id": "1234567890_my-task-abc",
            "type": "subagent_result",
            "elapsed_seconds": 180,  # 3 minutes
            "sent_reply_to_user": False,
        }
        assert not inbox_server._is_reconciler_ghost(msg)


# ---------------------------------------------------------------------------
# Tests for flood window functions
# ---------------------------------------------------------------------------

class TestFloodWindow:
    """_flood_register_message() and _flood_count_type() track bursts."""

    def test_register_and_count(self, inbox_server):
        """Messages registered in window are counted."""
        # Reset window state
        with inbox_server._flood_window_lock:
            inbox_server._flood_window.clear()

        inbox_server._flood_register_message("subagent_result")
        inbox_server._flood_register_message("subagent_result")
        inbox_server._flood_register_message("text")

        assert inbox_server._flood_count_type("subagent_result") == 2
        assert inbox_server._flood_count_type("text") == 1
        assert inbox_server._flood_count_type("agent_failed") == 0

    def test_window_evicts_old_entries(self, inbox_server, monkeypatch):
        """Entries older than FLOOD_WINDOW_SECONDS are not counted."""
        with inbox_server._flood_window_lock:
            inbox_server._flood_window.clear()

        # Register a very old entry (outside window)
        old_ts = time.time() - inbox_server._FLOOD_WINDOW_SECONDS - 1
        with inbox_server._flood_window_lock:
            inbox_server._flood_window.append((old_ts, "subagent_result"))

        # Fresh entry
        inbox_server._flood_register_message("subagent_result")

        # Old entry should not be counted
        count = inbox_server._flood_count_type("subagent_result")
        assert count == 1  # only the fresh entry
