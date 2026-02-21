"""
Tests for MCP Server Message Tools

Tests check_inbox, send_reply, mark_processed, list_sources, get_stats
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# We'll test the handlers directly by importing them
# and patching the directory constants


class TestCheckInbox:
    """Tests for check_inbox tool."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def test_empty_inbox_returns_no_messages(self, inbox_dir: Path):
        """Test that empty inbox returns appropriate message."""
        # Patch the module-level constants
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            assert len(result) == 1
            assert "No new messages" in result[0].text

    def test_returns_messages_from_inbox(
        self, inbox_dir: Path, message_generator
    ):
        """Test that messages in inbox are returned."""
        # Create some messages
        for i in range(3):
            msg = message_generator.generate_text_message(text=f"Test message {i}")
            (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            assert len(result) == 1
            assert "3 new message" in result[0].text
            assert "Test message" in result[0].text

    def test_respects_limit_parameter(self, inbox_dir: Path, message_generator):
        """Test that limit parameter restricts returned messages."""
        # Create 10 messages
        for i in range(10):
            msg = message_generator.generate_text_message()
            (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({"limit": 3}))

            assert "3 new message" in result[0].text

    def test_filters_by_source(self, inbox_dir: Path, message_generator):
        """Test that source filter works correctly."""
        # Create messages from different sources
        for source in ["telegram", "telegram", "sms"]:
            msg = message_generator.generate_text_message(source=source)
            (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({"source": "telegram"}))

            assert "2 new message" in result[0].text

    def test_handles_corrupted_file(self, inbox_dir: Path, message_generator):
        """Test that corrupted files are skipped gracefully."""
        # Create valid message
        msg = message_generator.generate_text_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        # Create corrupted file
        (inbox_dir / "corrupted.json").write_text("not valid json {{{")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            # Should return the valid message without error
            assert "1 new message" in result[0].text

    def test_voice_message_indicator(self, inbox_dir: Path, message_generator):
        """Test that voice messages are indicated."""
        msg = message_generator.generate_voice_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            assert "Voice message needs transcription" in result[0].text


class TestSendReply:
    """Tests for send_reply tool."""

    @pytest.fixture
    def outbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get outbox directory."""
        return temp_messages_dir / "outbox"

    def test_creates_reply_file(self, outbox_dir: Path):
        """Test that reply file is created in outbox."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                    "text": "Hello, this is a reply!",
                    "source": "telegram",
                })
            )

            assert "Reply queued" in result[0].text

            # Check file was created
            files = list(outbox_dir.glob("*.json"))
            assert len(files) == 1

            content = json.loads(files[0].read_text())
            assert content["chat_id"] == 123456
            assert content["text"] == "Hello, this is a reply!"
            assert content["source"] == "telegram"

    def test_requires_chat_id(self, outbox_dir: Path):
        """Test that chat_id is required."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "text": "Hello!",
                })
            )

            assert "Error" in result[0].text
            assert "required" in result[0].text

    def test_requires_text(self, outbox_dir: Path):
        """Test that text is required."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                })
            )

            assert "Error" in result[0].text

    def test_handles_unicode_text(self, outbox_dir: Path):
        """Test that Unicode text is handled correctly."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            unicode_text = "Hello! \U0001f600 \u4e2d\u6587 \u0420\u0443\u0441\u0441\u043a\u0438\u0439"
            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                    "text": unicode_text,
                })
            )

            assert "Reply queued" in result[0].text

            files = list(outbox_dir.glob("*.json"))
            content = json.loads(files[0].read_text())
            assert content["text"] == unicode_text

    def test_default_source_is_telegram(self, outbox_dir: Path):
        """Test that default source is telegram."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                    "text": "Hello!",
                })
            )

            files = list(outbox_dir.glob("*.json"))
            content = json.loads(files[0].read_text())
            assert content["source"] == "telegram"


class TestMarkProcessed:
    """Tests for mark_processed tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        """Set up inbox and processed directories."""
        inbox = temp_messages_dir / "inbox"
        processed = temp_messages_dir / "processed"
        return inbox, processed

    def test_moves_file_to_processed(self, setup_dirs, message_generator):
        """Test that message file is moved to processed directory (with force)."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id, "force": True}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (processed / f"{msg_id}.json").exists()

    def test_finds_by_partial_id(self, setup_dirs, message_generator):
        """Test that message can be found by partial ID match."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        # Use just part of the ID
        partial_id = msg_id.split("_")[0]

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": partial_id, "force": True}))

            assert "processed" in result[0].text.lower()

    def test_not_found_returns_error(self, setup_dirs):
        """Test that non-existent message returns error."""
        inbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(
                handle_mark_processed({"message_id": "nonexistent_id"})
            )

            assert "not found" in result[0].text.lower()

    def test_requires_message_id(self, setup_dirs):
        """Test that message_id is required."""
        inbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({}))

            assert "Error" in result[0].text


    def test_blocks_human_message_without_reply(self, setup_dirs, message_generator):
        """Test that marking a human message without reply returns a warning."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message(
            source="telegram", chat_id=123456,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "No reply sent" in result[0].text
            # File should NOT have been moved
            assert (inbox / f"{msg_id}.json").exists()
            assert not (processed / f"{msg_id}.json").exists()

    def test_allows_human_message_with_force(self, setup_dirs, message_generator):
        """Test that force=True bypasses the reply guard."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message(
            source="telegram", chat_id=123456,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id, "force": True}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()

    def test_allows_system_message_without_reply(self, setup_dirs, message_generator):
        """Test that system/internal messages are not guarded."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message(
            source="internal", chat_id=0,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()

    def test_allows_after_reply_sent(self, setup_dirs, message_generator):
        """Test that mark_processed works after send_reply was called."""
        inbox, processed = setup_dirs
        import time as _time

        msg = message_generator.generate_text_message(
            source="telegram", chat_id=999,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed, _track_reply

            # Simulate that a reply was sent to this chat_id
            _track_reply(999)

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()


class TestListSources:
    """Tests for list_sources tool."""

    def test_returns_sources_list(self):
        """Test that sources list is returned."""
        import asyncio
        from src.mcp.inbox_server import handle_list_sources

        result = asyncio.run(handle_list_sources({}))

        assert "Sources" in result[0].text
        assert "Telegram" in result[0].text

    def test_shows_enabled_status(self):
        """Test that enabled status is shown."""
        import asyncio
        from src.mcp.inbox_server import handle_list_sources

        result = asyncio.run(handle_list_sources({}))

        assert "Enabled" in result[0].text or "enabled" in result[0].text.lower()


class TestGetStats:
    """Tests for get_stats tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path, message_generator):
        """Set up directories with messages."""
        inbox = temp_messages_dir / "inbox"
        outbox = temp_messages_dir / "outbox"
        processed = temp_messages_dir / "processed"

        # Add some messages to each
        for i in range(3):
            msg = message_generator.generate_text_message()
            (inbox / f"inbox_{i}.json").write_text(json.dumps(msg))

        for i in range(2):
            reply = {"chat_id": 123, "text": "Reply"}
            (outbox / f"outbox_{i}.json").write_text(json.dumps(reply))

        for i in range(5):
            msg = message_generator.generate_text_message()
            (processed / f"processed_{i}.json").write_text(json.dumps(msg))

        return inbox, outbox, processed

    def test_returns_message_counts(self, setup_dirs):
        """Test that message counts are returned."""
        inbox, outbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_get_stats

            result = asyncio.run(handle_get_stats({}))

            assert "3" in result[0].text  # inbox count
            assert "2" in result[0].text  # outbox count
            assert "5" in result[0].text  # processed count

    def test_shows_source_breakdown(self, setup_dirs):
        """Test that source breakdown is shown."""
        inbox, outbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_get_stats

            result = asyncio.run(handle_get_stats({}))

            assert "Statistics" in result[0].text
