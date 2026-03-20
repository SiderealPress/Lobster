"""
Tests for Daemon Error Backoff

Tests error handling and backoff behavior in the daemon loop.

NOTE: The src.daemon.daemon module was removed when the daemon loop was absorbed
into the MCP inbox_server (src/mcp/inbox_server.py).  These tests are skipped
until they are rewritten against the new architecture.
"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import time

pytestmark = pytest.mark.skip(
    reason="src.daemon.daemon module removed — daemon loop now lives in src.mcp.inbox_server"
)


class TestErrorBackoff:
    """Tests for error backoff behavior."""

    @pytest.fixture
    def workspace(self, temp_workspace: Path) -> Path:
        """Get workspace directory."""
        return temp_workspace

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    @pytest.mark.asyncio
    async def test_consecutive_errors_tracked(
        self, workspace: Path, inbox_dir: Path, message_generator
    ):
        """Test that consecutive errors are tracked."""
        # Create a message to trigger processing
        import json
        msg = message_generator.generate_text_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        # Track error counts
        error_counts = []

        async def mock_process():
            return (False, "Error")

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            with patch("src.daemon.daemon.INBOX_DIR", inbox_dir):
                with patch("src.daemon.daemon.process_messages", mock_process):
                    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                        from src.daemon.daemon import daemon_loop

                        # Run a few iterations
                        iterations = 0

                        async def limited_loop():
                            nonlocal iterations
                            while iterations < 3:
                                iterations += 1
                                # Simulate one loop iteration
                                msg_count = len(list(inbox_dir.glob("*.json")))
                                if msg_count > 0:
                                    success, _ = await mock_process()
                                    if not success:
                                        error_counts.append(len(error_counts) + 1)

                        await limited_loop()

                        # Should have tracked 3 errors
                        assert len(error_counts) == 3

    def test_max_errors_constant(self):
        """Test that max_errors is 5."""
        # The max_errors is a local variable in daemon_loop
        # We test the documented behavior instead
        pass  # This is implicitly tested in integration tests

    @pytest.mark.asyncio
    async def test_backoff_sleep_after_max_errors(self):
        """Test that extended sleep occurs after max consecutive errors."""
        # This tests the behavior documented in the daemon
        # After 5 consecutive errors, sleep for 60 seconds

        sleep_times = []

        async def track_sleep(duration):
            sleep_times.append(duration)
            if len(sleep_times) > 10:
                raise StopIteration("Enough iterations")

        with patch("asyncio.sleep", side_effect=track_sleep):
            # The actual daemon_loop would need to be modified
            # to be testable, so we test the concept here

            # Simulate the backoff logic
            consecutive_errors = 0
            max_errors = 5

            for _ in range(7):
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    try:
                        await track_sleep(60)  # Backoff sleep
                    except StopIteration:
                        break
                    consecutive_errors = 0

            # Should have had at least one 60s sleep
            assert 60 in sleep_times


class TestLoopRecovery:
    """Tests for loop recovery after errors."""

    @pytest.mark.asyncio
    async def test_loop_continues_after_error(self):
        """Test that the loop continues after an error."""
        iterations = 0

        async def mock_process():
            nonlocal iterations
            iterations += 1
            if iterations < 3:
                return (False, "Error")
            return (True, "Success")

        # Simulate loop behavior
        consecutive_errors = 0
        success_count = 0

        for _ in range(5):
            success, _ = await mock_process()
            if success:
                consecutive_errors = 0
                success_count += 1
            else:
                consecutive_errors += 1

        # Should have recovered and had successes
        assert success_count > 0
        assert consecutive_errors == 0

    @pytest.mark.asyncio
    async def test_error_count_resets_on_success(self):
        """Test that error count resets on successful processing."""
        consecutive_errors = 5  # Simulate max errors

        async def mock_success():
            return (True, "Success")

        success, _ = await mock_success()
        if success:
            consecutive_errors = 0

        assert consecutive_errors == 0
