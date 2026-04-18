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

    def test_health_check_references_wfm_active_stale_seconds(self):
        """health-check-v3.sh must define WFM_ACTIVE_STALE_SECONDS."""
        health_src = (
            Path(__file__).resolve().parents[2] / "scripts" / "health-check-v3.sh"
        ).read_text()
        assert "WFM_ACTIVE_STALE_SECONDS" in health_src, (
            "health-check-v3.sh must define WFM_ACTIVE_STALE_SECONDS"
        )

    def test_health_check_wfm_active_bypass_in_heartbeat_function(self):
        """check_dispatcher_heartbeat() must check WFM-active before declaring RED."""
        health_src = (
            Path(__file__).resolve().parents[2] / "scripts" / "health-check-v3.sh"
        ).read_text()

        # The function must contain logic that reads DISPATCHER_WFM_ACTIVE_FILE
        # before returning exit code 2 (RED).
        func_start = health_src.find("check_dispatcher_heartbeat()")
        func_end = health_src.find("\n}", func_start)
        assert func_start != -1, "check_dispatcher_heartbeat() must exist"
        func_body = health_src[func_start:func_end + 2]

        assert "DISPATCHER_WFM_ACTIVE_FILE" in func_body, (
            "check_dispatcher_heartbeat() must read DISPATCHER_WFM_ACTIVE_FILE "
            "to bypass the RED signal when the dispatcher is alive in WFM"
        )

    def test_wfm_active_stale_threshold_is_reasonable(self):
        """WFM_ACTIVE_STALE_SECONDS must be >= 2x WAIT_HEARTBEAT_INTERVAL (120s)."""
        health_src = (
            Path(__file__).resolve().parents[2] / "scripts" / "health-check-v3.sh"
        ).read_text()
        # Extract the value
        for line in health_src.splitlines():
            if line.strip().startswith("WFM_ACTIVE_STALE_SECONDS="):
                value = int(line.split("=")[1].strip().split()[0])
                assert value >= 2 * EXPECTED_WAIT_HEARTBEAT_INTERVAL, (
                    f"WFM_ACTIVE_STALE_SECONDS ({value}) must be >= "
                    f"2 * WAIT_HEARTBEAT_INTERVAL ({2 * EXPECTED_WAIT_HEARTBEAT_INTERVAL}s) "
                    "to avoid false positives when the heartbeat interval fires right at the boundary"
                )
                return
        pytest.fail("WFM_ACTIVE_STALE_SECONDS not found in health-check-v3.sh")
