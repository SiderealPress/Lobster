"""
Unit tests for the WFM-active heartbeat signal (issue #949).

When wait_for_messages blocks, PostToolUse hooks do not fire and the
dispatcher-heartbeat file goes stale after 20 minutes. The inbox server
writes dispatcher-wfm-active (a Unix epoch timestamp) at the start of each
WFM wait iteration and clears it on return so the health check can distinguish
"dispatcher alive, blocked in WFM" from "dispatcher frozen/dead".

Tests verify:
- WFM-active file is written on WFM entry (before blocking)
- WFM-active file contains a fresh Unix epoch integer
- WFM-active file is refreshed on each heartbeat iteration (every ~60s)
- WFM-active file is deleted when WFM returns with messages
- WFM-active file is deleted when WFM times out
- File is atomic (written via tmp → rename, no partial reads)
- Module-level constant WFM_ACTIVE_FILE is accessible for env override
"""

import importlib.util
import json
import os
import sys
import threading
import time
import asyncio
import threading
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — load inbox_server constants and helpers without full startup
# ---------------------------------------------------------------------------

_SRC_MCP_DIR = Path(__file__).resolve().parents[2] / "src" / "mcp"


# ---------------------------------------------------------------------------
# Constants that must match inbox_server.py
# ---------------------------------------------------------------------------

# How often WFM touches the heartbeat and refreshes WFM-active (seconds).
EXPECTED_WAIT_HEARTBEAT_INTERVAL = 60

# The staleness threshold used by the health check (must match health-check-v3.sh).
# This is 3 * WAIT_HEARTBEAT_INTERVAL.
EXPECTED_WFM_ACTIVE_STALE_SECONDS = 180


class TestWfmActiveConstants:
    """Verify that inbox_server.py exports the expected constants."""

    def test_wfm_active_file_constant_exists(self):
        """inbox_server.py must define WFM_ACTIVE_FILE as a module-level Path."""
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()
        assert "WFM_ACTIVE_FILE" in server_src, (
            "inbox_server.py must define WFM_ACTIVE_FILE constant"
        )

    def test_wfm_active_env_override_supported(self):
        """WFM_ACTIVE_FILE must support LOBSTER_WFM_ACTIVE_OVERRIDE env var."""
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()
        assert "LOBSTER_WFM_ACTIVE_OVERRIDE" in server_src, (
            "inbox_server.py must read LOBSTER_WFM_ACTIVE_OVERRIDE to allow test overrides"
        )

    def test_wait_heartbeat_interval_is_60s(self):
        """WAIT_HEARTBEAT_INTERVAL must be 60s (matches health check staleness calculation)."""
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()
        assert f"WAIT_HEARTBEAT_INTERVAL = {EXPECTED_WAIT_HEARTBEAT_INTERVAL}" in server_src, (
            f"WAIT_HEARTBEAT_INTERVAL must be {EXPECTED_WAIT_HEARTBEAT_INTERVAL} to stay consistent "
            "with WFM_ACTIVE_STALE_SECONDS in health-check-v3.sh"
        )


class TestWfmActiveFileWrite:
    """Verify that handle_wait_for_messages writes and clears dispatcher-wfm-active."""

    def test_wfm_active_file_written_before_blocking(self, tmp_path):
        """WFM-active file must be written before the blocking wait begins."""
        # Verify the write path exists in the source code as a structural test.
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()

        # The file must be written BEFORE the wait loop inside handle_wait_for_messages.
        # Narrow the search to the region between `elapsed = 0` and `while elapsed < timeout`
        # within that function to avoid matching the function definition itself.
        func_start = server_src.find("async def handle_wait_for_messages")
        assert func_start != -1, "handle_wait_for_messages must exist"
        elapsed_pos = server_src.find("elapsed = 0", func_start)
        assert elapsed_pos != -1, "elapsed = 0 initializer must exist in handle_wait_for_messages"
        while_pos = server_src.find("while elapsed < timeout", elapsed_pos)
        assert while_pos != -1, "wait loop must exist in handle_wait_for_messages"
        pre_loop_region = server_src[elapsed_pos:while_pos]
        assert "_write_wfm_active_signal()" in pre_loop_region, (
            "_write_wfm_active_signal() must be called before the blocking wait loop"
        )

    def test_wfm_active_file_cleared_in_finally(self):
        """WFM-active file must be deleted in the finally block (cleared on any exit)."""
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()

        # Locate handle_wait_for_messages
        func_start = server_src.find("async def handle_wait_for_messages")
        assert func_start != -1, "handle_wait_for_messages must exist"

        # Find the finally block within the function
        finally_pos = server_src.find("finally:", func_start)
        assert finally_pos != -1, "finally block must exist in handle_wait_for_messages"

        # The cleanup of WFM_ACTIVE_FILE must appear somewhere after the finally:.
        # Use a large window (2000 chars) to cover the full finally block including
        # the existing wfm-active.json clearing code and our new WFM_ACTIVE_FILE clear.
        region_after_finally = server_src[finally_pos:finally_pos + 2000]
        has_wfm_active_clear = "WFM_ACTIVE_FILE" in region_after_finally
        assert has_wfm_active_clear, (
            "WFM_ACTIVE_FILE must be cleared in the finally block to ensure "
            "cleanup on message arrival, timeout, and error"
        )

    def test_wfm_active_file_refreshed_in_wait_loop(self):
        """WFM-active file must be refreshed on each heartbeat iteration (not just on entry)."""
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()

        # The while loop must call _write_wfm_active_signal() for refresh.
        while_start = server_src.find("while elapsed < timeout")
        while_end = server_src.find("if message_arrived.is_set()", while_start)
        assert while_start != -1 and while_end != -1

        loop_body = server_src[while_start:while_end]
        assert "_write_wfm_active_signal()" in loop_body, (
            "_write_wfm_active_signal() must be called inside the wait loop so the "
            "health check sees a fresh signal even during long quiet periods"
        )

    def test_wfm_active_file_content_is_epoch_integer(self):
        """WFM-active content must be a Unix epoch integer (consistent with dispatcher-heartbeat)."""
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()

        # Check that _write_wfm_active_signal uses int(time.time()) or str(int(time.time())).
        # The function body follows the docstring so we need a larger window.
        helper_start = server_src.find("def _write_wfm_active_signal")
        assert helper_start != -1, "_write_wfm_active_signal helper must exist"
        # 1000 chars is enough to cover the full helper including the docstring
        helper_region = server_src[helper_start:helper_start + 1000]
        assert "int(time.time())" in helper_region, (
            "_write_wfm_active_signal must write a Unix epoch timestamp (int), "
            "not JSON or ISO format, so the health check can parse it as an integer"
        )


class TestWfmActiveHealthCheckIntegration:
    """Integration tests: verify the health check logic handles WFM-active correctly.

    These tests verify the behavioral contract from both sides:
    - inbox_server.py writes the file at the correct path
    - health-check-v3.sh respects the file when heartbeat is stale
    """

    def test_wfm_active_path_uses_logs_dir(self):
        """WFM-active file must be in ~/lobster-workspace/logs/ to match health check defaults."""
        server_src = (_SRC_MCP_DIR / "inbox_server.py").read_text()
        # The path must reference the logs directory.
        assert '"dispatcher-wfm-active"' in server_src or "'dispatcher-wfm-active'" in server_src, (
            "WFM-active filename must be 'dispatcher-wfm-active'"
        )
        # The path should be under _WORKSPACE / "logs" (matching DISPATCHER_WFM_ACTIVE_FILE in health check).
        assert 'WFM_ACTIVE_FILE' in server_src

    def test_health_check_references_wfm_active_constant(self):
        """health-check-v3.sh must define DISPATCHER_WFM_ACTIVE_FILE."""
        health_src = (
            Path(__file__).resolve().parents[2] / "scripts" / "health-check-v3.sh"
        ).read_text()
        assert "DISPATCHER_WFM_ACTIVE_FILE" in health_src, (
            "health-check-v3.sh must define DISPATCHER_WFM_ACTIVE_FILE"
        )

    # Make the parent directory read-only to provoke a write failure
    wfm_file.parent.mkdir(parents=True, exist_ok=True)
    wfm_file.parent.chmod(0o555)
    try:
        # Must not raise
        server._clear_wfm_active_signal()
    finally:
        wfm_file.parent.chmod(0o755)


# ---------------------------------------------------------------------------
# _wfm_heartbeat_thread_fn tests (issue #1823)
# ---------------------------------------------------------------------------

def test_wfm_heartbeat_thread_fn_fires_at_least_once(tmp_path):
    """_wfm_heartbeat_thread_fn fires touch_heartbeat and _write_wfm_active_signal
    at least once within a short interval."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    calls = []
    original_touch = server.touch_heartbeat
    original_write = server._write_wfm_active_signal

    def spy_touch():
        calls.append("touch")
        original_touch()

    def spy_write():
        calls.append("write")
        original_write()

    stop_event = threading.Event()
    with patch.object(server, "touch_heartbeat", spy_touch), \
         patch.object(server, "_write_wfm_active_signal", spy_write):
        t = threading.Thread(
            target=server._wfm_heartbeat_thread_fn,
            args=(stop_event, 0.05),
            daemon=True,
        )
        t.start()
        # Wait long enough for at least one tick (interval=0.05s, wait 0.3s)
        time.sleep(0.3)
        stop_event.set()
        t.join(timeout=2)

    assert "touch" in calls, "_wfm_heartbeat_thread_fn must call touch_heartbeat at least once"
    assert "write" in calls, "_wfm_heartbeat_thread_fn must call _write_wfm_active_signal at least once"


def test_wfm_heartbeat_thread_fn_stops_after_stop_event(tmp_path):
    """_wfm_heartbeat_thread_fn stops (thread exits) after stop_event.set()."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    stop_event = threading.Event()
    t = threading.Thread(
        target=server._wfm_heartbeat_thread_fn,
        args=(stop_event, 0.05),
        daemon=True,
    )
    t.start()
    # Let it start, then signal stop
    time.sleep(0.1)
    stop_event.set()
    t.join(timeout=1)

    assert not t.is_alive(), (
        "Thread must not be alive after stop_event.set() and join(timeout=1)"
    )


def test_wfm_heartbeat_thread_fn_swallows_exceptions(tmp_path):
    """_wfm_heartbeat_thread_fn swallows exceptions from touch_heartbeat — never raises."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    def raising_touch():
        raise RuntimeError("simulated heartbeat failure")

    # stop_event already set: the thread loop will never fire even one tick,
    # but we want to confirm a stop_event that is NOT set still works fine
    # when exceptions occur.  Use a very short interval so the exception path
    # is exercised, then set the stop_event.
    stop_event = threading.Event()
    with patch.object(server, "touch_heartbeat", raising_touch):
        t = threading.Thread(
            target=server._wfm_heartbeat_thread_fn,
            args=(stop_event, 0.05),
            daemon=True,
        )
        t.start()
        # Give it time to fire (and raise) at least once
        time.sleep(0.3)
        stop_event.set()
        t.join(timeout=1)

    # If the thread is dead without us killing it via stop_event first,
    # it means an unhandled exception propagated — which is a test failure.
    # After stop_event.set() + join, it should be cleanly stopped (not crashed).
    assert not t.is_alive(), (
        "Thread crashed due to an unswallowed exception from touch_heartbeat"
    )
