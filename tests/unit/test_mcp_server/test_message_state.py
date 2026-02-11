"""
Tests for Message State Machine and Retry Logic

Tests mark_processing, mark_processed (updated), mark_failed,
stale recovery, and retry recovery.
"""

import json
import os
import time
import pytest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone


class TestMarkProcessing:
    """Tests for mark_processing tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        return inbox, processing

    def test_moves_file_to_processing(self, setup_dirs, message_generator):
        """Test that message file is moved from inbox to processing."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            result = asyncio.run(handle_mark_processing({"message_id": msg_id}))

            assert "claimed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (processing / f"{msg_id}.json").exists()

    def test_not_found_returns_error(self, setup_dirs):
        """Test that non-existent message returns error."""
        inbox, processing = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            result = asyncio.run(
                handle_mark_processing({"message_id": "nonexistent_id"})
            )

            assert "not found" in result[0].text.lower()

    def test_requires_message_id(self, setup_dirs):
        """Test that message_id is required."""
        inbox, processing = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            result = asyncio.run(handle_mark_processing({}))

            assert "Error" in result[0].text


class TestMarkProcessedUpdated:
    """Tests for updated mark_processed that checks processing/ first."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        processed = temp_messages_dir / "processed"
        return inbox, processing, processed

    def test_finds_in_processing_first(self, setup_dirs, message_generator):
        """Test that mark_processed checks processing/ before inbox/."""
        inbox, processing, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        # Put the message in processing/ (not inbox/)
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()
            assert not (processing / f"{msg_id}.json").exists()
            assert (processed / f"{msg_id}.json").exists()

    def test_falls_back_to_inbox(self, setup_dirs, message_generator):
        """Test that mark_processed falls back to inbox/ if not in processing/."""
        inbox, processing, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (processed / f"{msg_id}.json").exists()

    def test_not_found_returns_error(self, setup_dirs):
        """Test that non-existent message returns error."""
        inbox, processing, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(
                handle_mark_processed({"message_id": "nonexistent_id"})
            )

            assert "not found" in result[0].text.lower()


class TestMarkFailed:
    """Tests for mark_failed tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        failed = temp_messages_dir / "failed"
        return inbox, processing, failed

    def test_moves_to_failed_with_retry_metadata(self, setup_dirs, message_generator):
        """Test that message is moved to failed/ with retry metadata."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "test error",
            }))

            assert "retry" in result[0].text.lower()
            assert not (processing / f"{msg_id}.json").exists()
            assert (failed / f"{msg_id}.json").exists()

            # Check retry metadata
            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            assert failed_msg["_retry_count"] == 1
            assert failed_msg["_last_error"] == "test error"
            assert "_retry_at" in failed_msg
            assert "_last_failed_at" in failed_msg

    def test_increments_retry_count(self, setup_dirs, message_generator):
        """Test that retry count is incremented on subsequent failures."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 1
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "another error",
            }))

            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            assert failed_msg["_retry_count"] == 2

    def test_permanent_failure_after_max_retries(self, setup_dirs, message_generator):
        """Test that message is permanently failed after max retries."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 3  # Already at max
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "final error",
                "max_retries": 3,
            }))

            assert "permanently failed" in result[0].text.lower()
            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            assert failed_msg["_permanently_failed"] is True

    def test_exponential_backoff(self, setup_dirs, message_generator):
        """Test that backoff increases exponentially."""
        inbox, processing, failed = setup_dirs

        # First failure: backoff should be 60s
        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "err",
            }))

            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            now = datetime.now(timezone.utc).timestamp()
            # First retry: 60s backoff
            assert abs(failed_msg["_retry_at"] - (now + 60)) < 5

        # Second failure: backoff should be 120s
        (failed / f"{msg_id}.json").rename(processing / f"{msg_id}.json")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "err",
            }))

            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            now = datetime.now(timezone.utc).timestamp()
            # Second retry: 120s backoff
            assert abs(failed_msg["_retry_at"] - (now + 120)) < 5

    def test_requires_message_id(self, setup_dirs):
        """Test that message_id is required."""
        inbox, processing, failed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({}))

            assert "Error" in result[0].text

    def test_finds_in_inbox_fallback(self, setup_dirs, message_generator):
        """Test that mark_failed can find messages in inbox/ too."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "err",
            }))

            assert "retry" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (failed / f"{msg_id}.json").exists()


class TestStaleRecovery:
    """Tests for stale processing recovery."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        return inbox, processing

    def test_recovers_stale_messages(self, setup_dirs, message_generator):
        """Test that old messages in processing/ are moved back to inbox/."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))

        # Set mtime to 10 minutes ago
        old_time = time.time() - 600
        os.utime(msg_file, (old_time, old_time))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing(max_age_seconds=300)

            assert not (processing / f"{msg_id}.json").exists()
            assert (inbox / f"{msg_id}.json").exists()

    def test_leaves_recent_messages(self, setup_dirs, message_generator):
        """Test that recent messages in processing/ are left alone."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))
        # File was just created, so mtime is now

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing(max_age_seconds=300)

            # Should still be in processing
            assert (processing / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()


class TestRetryRecovery:
    """Tests for retry recovery from failed/."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        failed = temp_messages_dir / "failed"
        return inbox, failed

    def test_recovers_retryable_messages_past_retry_at(self, setup_dirs, message_generator):
        """Test that messages past their retry_at time are moved to inbox/."""
        inbox, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 1
        msg["_retry_at"] = time.time() - 10  # 10 seconds ago
        msg_id = msg["id"]
        (failed / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

            assert not (failed / f"{msg_id}.json").exists()
            assert (inbox / f"{msg_id}.json").exists()

    def test_leaves_messages_before_retry_at(self, setup_dirs, message_generator):
        """Test that messages before retry_at stay in failed/."""
        inbox, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 1
        msg["_retry_at"] = time.time() + 3600  # 1 hour from now
        msg_id = msg["id"]
        (failed / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

            assert (failed / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()

    def test_permanently_failed_messages_stay(self, setup_dirs, message_generator):
        """Test that permanently failed messages stay in failed/."""
        inbox, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_permanently_failed"] = True
        msg["_retry_count"] = 4
        msg["_retry_at"] = time.time() - 3600  # Long past
        msg_id = msg["id"]
        (failed / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

            assert (failed / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()
