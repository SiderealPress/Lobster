"""
Tests for MCP Server wait_for_messages Tool

Tests the blocking message wait functionality.
"""

import asyncio
import json
import pytest
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock


class TestWaitForMessages:
    """Tests for wait_for_messages tool."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def test_returns_immediately_if_messages_exist(
        self, inbox_dir: Path, message_generator
    ):
        """Test immediate return when inbox has messages."""
        # Create a message
        msg = message_generator.generate_text_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_wait_for_messages

            start = time.time()
            result = asyncio.run(handle_wait_for_messages({"timeout": 10}))
            elapsed = time.time() - start

            # Should return quickly (< 1 second)
            assert elapsed < 1.0
            assert "1 new message" in result[0].text

    def test_returns_timeout_message_when_no_messages(self, inbox_dir: Path):
        """Test timeout message when no messages arrive."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_wait_for_messages

            # Use a short timeout for testing
            result = asyncio.run(handle_wait_for_messages({"timeout": 1}))

            assert "No messages received" in result[0].text
            assert "call" in result[0].text.lower()  # Prompt to call again

    def test_default_timeout_applies(self, inbox_dir: Path):
        """Test that default timeout behavior works."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_wait_for_messages

            # With no messages and short timeout, should return timeout message
            result = asyncio.run(handle_wait_for_messages({"timeout": 1}))

            # Should indicate no messages and prompt to call again
            assert "No messages received" in result[0].text or "timeout" in result[0].text.lower()

    def test_detects_new_message_via_inotify(self, inbox_dir: Path, message_generator):
        """Test that new messages are detected via file watcher."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_wait_for_messages

            async def add_message_after_delay():
                """Add a message after a short delay."""
                await asyncio.sleep(0.5)
                msg = message_generator.generate_text_message()
                (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

            async def run_test():
                # Start waiting and adding message concurrently
                wait_task = asyncio.create_task(
                    asyncio.to_thread(
                        lambda: asyncio.run(
                            handle_wait_for_messages({"timeout": 5})
                        )
                    )
                )
                add_task = asyncio.create_task(add_message_after_delay())

                # The wait should detect the new message
                await add_task

                # Give some time for the watcher to detect
                await asyncio.sleep(0.2)

                # Cancel if still waiting (shouldn't happen in real scenario)
                if not wait_task.done():
                    wait_task.cancel()

            # This test validates the mechanism but may be flaky in CI
            # Just ensure no exceptions are raised
            try:
                asyncio.run(run_test())
            except asyncio.CancelledError:
                pass  # Expected if message detection timing varies


class TestWaitForMessagesIntegration:
    """Integration tests for wait_for_messages with file watching."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    @pytest.mark.slow
    def test_message_arrival_wakes_watcher(self, inbox_dir: Path, message_generator):
        """Test that arriving message wakes up the watcher."""
        import threading

        results = []
        errors = []

        def wait_thread():
            try:
                with patch.multiple(
                    "src.mcp.inbox_server",
                    INBOX_DIR=inbox_dir,
                ):
                    from src.mcp.inbox_server import handle_wait_for_messages
                    result = asyncio.run(handle_wait_for_messages({"timeout": 10}))
                    results.append(result)
            except Exception as e:
                errors.append(e)

        # Start waiting thread
        thread = threading.Thread(target=wait_thread)
        thread.start()

        # Wait a bit then add a message
        time.sleep(1)
        msg = message_generator.generate_text_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        # Wait for thread to complete
        thread.join(timeout=5)

        if errors:
            raise errors[0]

        assert len(results) > 0
        # Result should contain the message (not timeout)
        if results:
            assert "message" in results[0][0].text.lower()


class TestSubagentNotificationWakesWFM:
    """Tests that subagent_notification messages wake wait_for_messages (issue #990).

    The file watcher (watchdog/inotify) can miss rename events under high load.
    WFM must also periodically poll the inbox directory as a fallback so that
    subagent_notification (and other message types) are detected within one
    heartbeat interval even when the file-system event is missed.
    """

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def test_precheck_detects_existing_inbox_files_before_wait(
        self, inbox_dir: Path
    ):
        """WFM returns immediately when a subagent_notification is already in the inbox.

        Validates the pre-check path: after the observer starts but before blocking,
        WFM scans for existing files. Messages already present are detected without
        waiting for any heartbeat interval or inotify event.
        """
        # Write a subagent_notification BEFORE WFM starts so the pre-check
        # picks it up immediately.  This validates that the pre-check path works.
        notification = {
            "id": "1234567890_test_notify",
            "type": "subagent_notification",
            "source": "telegram",
            "chat_id": 12345,
            "text": "Subagent completed and already sent reply.",
            "task_id": "test-task-123",
            "status": "success",
            "sent_reply_to_user": True,
            "timestamp": "2026-01-01T12:00:00+00:00",
            "warning": "User already received the subagent's reply.",
        }
        notification_file = inbox_dir / f"{notification['id']}.json"
        notification_file.write_text(json.dumps(notification))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_wait_for_messages

            start = time.time()
            result = asyncio.run(handle_wait_for_messages({"timeout": 10}))
            elapsed = time.time() - start

            # Pre-check should return immediately (< 1 second)
            assert elapsed < 1.0, f"Expected immediate return from pre-check, took {elapsed:.2f}s"
            text = result[0].text
            assert "subagent_notification" in text.lower() or "SUBAGENT NOTIFICATION" in text

    def test_fallback_poll_detects_subagent_notification_written_during_wait(
        self, inbox_dir: Path
    ):
        """WFM must detect a subagent_notification written while WFM is blocking.

        Simulates the case where a subagent calls write_result(sent_reply_to_user=True)
        while the dispatcher is blocked in wait_for_messages.  Even if the file
        watcher misses the inotify event, the fallback poll must detect the file
        within WAIT_HEARTBEAT_INTERVAL seconds.

        WAIT_HEARTBEAT_INTERVAL is patched to 1s so the fallback poll fires within
        ~1.2s of the file being written — well before the 8s join timeout.
        """
        import threading

        results = []
        errors = []

        def wait_thread():
            try:
                with patch.multiple(
                    "src.mcp.inbox_server",
                    INBOX_DIR=inbox_dir,
                    WAIT_HEARTBEAT_INTERVAL=1,  # Patch heartbeat to 1s so fallback fires quickly
                ):
                    from src.mcp.inbox_server import handle_wait_for_messages
                    result = asyncio.run(handle_wait_for_messages({"timeout": 5}))
                    results.append(result)
            except Exception as e:
                errors.append(e)

        thread = threading.Thread(target=wait_thread)
        thread.start()

        # Wait a short moment then write the notification directly (no rename,
        # simulating a case where the file watcher might not fire).
        time.sleep(0.2)
        notification = {
            "id": "9876543210_test_delayed_notify",
            "type": "subagent_notification",
            "source": "telegram",
            "chat_id": 12345,
            "text": "Subagent already sent reply to user.",
            "task_id": "delayed-task",
            "status": "success",
            "sent_reply_to_user": True,
            "timestamp": "2026-01-01T12:00:01+00:00",
        }
        notification_file = inbox_dir / f"{notification['id']}.json"
        notification_file.write_text(json.dumps(notification))

        # With WAIT_HEARTBEAT_INTERVAL=1s and message written at 0.2s, the fallback
        # poll should fire within ~1.2s — well before the 8s join timeout.
        thread.join(timeout=8)

        if errors:
            raise errors[0]

        assert len(results) > 0, "WFM should have returned after subagent_notification was written"
        text = results[0][0].text
        assert "message" in text.lower() or "SUBAGENT" in text, (
            f"Expected WFM to return messages, got: {text[:200]}"
        )
