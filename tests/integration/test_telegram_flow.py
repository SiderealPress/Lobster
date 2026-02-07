"""
Tests for Telegram Integration Flow

Tests the Telegram -> inbox -> outbox -> Telegram flow.
"""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.integration
class TestTelegramToInbox:
    """Tests for Telegram message to inbox flow."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        """Set up directories."""
        return {
            "inbox": temp_messages_dir / "inbox",
            "outbox": temp_messages_dir / "outbox",
            "audio": temp_messages_dir / "audio",
        }

    @pytest.mark.asyncio
    async def test_text_message_written_to_inbox(self, setup_dirs):
        """Test that Telegram text messages are written to inbox."""
        inbox = setup_dirs["inbox"]

        # Simulate what the bot does
        msg_id = "test_12345_1"
        msg_data = {
            "id": msg_id,
            "source": "telegram",
            "chat_id": 123456,
            "user_id": 123456,
            "username": "testuser",
            "user_name": "Test User",
            "text": "Hello from Telegram!",
            "timestamp": "2024-01-01T00:00:00Z",
        }

        (inbox / f"{msg_id}.json").write_text(json.dumps(msg_data))

        # Verify
        files = list(inbox.glob("*.json"))
        assert len(files) == 1

        content = json.loads(files[0].read_text())
        assert content["source"] == "telegram"
        assert content["text"] == "Hello from Telegram!"

    @pytest.mark.asyncio
    async def test_voice_message_with_audio_file(self, setup_dirs):
        """Test that voice messages include audio file reference."""
        inbox = setup_dirs["inbox"]
        audio = setup_dirs["audio"]

        msg_id = "voice_12345_1"

        # Create fake audio file
        audio_file = audio / f"{msg_id}.ogg"
        audio_file.write_bytes(b"\x00" * 1000)

        msg_data = {
            "id": msg_id,
            "source": "telegram",
            "type": "voice",
            "chat_id": 123456,
            "user_id": 123456,
            "username": "testuser",
            "user_name": "Test User",
            "text": "[Voice message - pending transcription]",
            "audio_file": str(audio_file),
            "audio_duration": 10,
            "timestamp": "2024-01-01T00:00:00Z",
        }

        (inbox / f"{msg_id}.json").write_text(json.dumps(msg_data))

        # Verify
        content = json.loads((inbox / f"{msg_id}.json").read_text())
        assert content["type"] == "voice"
        assert Path(content["audio_file"]).exists()


@pytest.mark.integration
class TestOutboxToTelegram:
    """Tests for outbox to Telegram flow."""

    @pytest.fixture
    def outbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get outbox directory."""
        return temp_messages_dir / "outbox"

    @pytest.mark.asyncio
    async def test_outbox_file_format(self, outbox_dir: Path):
        """Test that outbox files have correct format."""
        reply_id = "reply_12345"
        reply_data = {
            "id": reply_id,
            "source": "telegram",
            "chat_id": 123456,
            "text": "This is a reply from Lobster",
            "timestamp": "2024-01-01T00:00:00Z",
        }

        (outbox_dir / f"{reply_id}.json").write_text(json.dumps(reply_data))

        # Verify format is correct for bot to process
        content = json.loads((outbox_dir / f"{reply_id}.json").read_text())

        # Required fields for bot
        assert "chat_id" in content
        assert "text" in content
        assert isinstance(content["chat_id"], int)
        assert isinstance(content["text"], str)

    @pytest.mark.asyncio
    async def test_bot_outbox_handler_processes_file(
        self, outbox_dir: Path
    ):
        """Test that OutboxHandler processes files correctly."""
        import os

        reply_data = {
            "id": "reply_test",
            "source": "telegram",
            "chat_id": 123456,
            "text": "Reply text",
            "timestamp": "2024-01-01T00:00:00Z",
        }

        reply_file = outbox_dir / "reply_test.json"
        reply_file.write_text(json.dumps(reply_data))

        # Mock the bot
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        # Set environment variables before importing the bot module
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "123456",
            },
        ):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            handler = bot_module.OutboxHandler()

            original_app = bot_module.bot_app
            bot_module.bot_app = MagicMock()
            bot_module.bot_app.bot = mock_bot

            loop = asyncio.new_event_loop()
            bot_module.main_loop = loop

            try:
                await handler.process_reply(str(reply_file))

                mock_bot.send_message.assert_called_once_with(
                    chat_id=123456, text="Reply text"
                )
            finally:
                bot_module.bot_app = original_app
                loop.close()


@pytest.mark.integration
class TestFullTelegramRoundtrip:
    """Tests for complete Telegram roundtrip."""

    @pytest.fixture
    def message_system(self, temp_messages_dir: Path):
        """Set up complete message system."""
        dirs = {
            "inbox": temp_messages_dir / "inbox",
            "outbox": temp_messages_dir / "outbox",
            "processed": temp_messages_dir / "processed",
        }

        # Initialize tasks.json
        (temp_messages_dir / "tasks.json").write_text(
            json.dumps({"tasks": [], "next_id": 1})
        )

        return dirs

    @pytest.mark.asyncio
    async def test_complete_roundtrip(self, message_system, message_generator):
        """Test complete: Telegram -> inbox -> MCP -> outbox -> (Telegram)."""
        inbox = message_system["inbox"]
        outbox = message_system["outbox"]
        processed = message_system["processed"]

        # Step 1: Simulate Telegram message arrival (bot writes to inbox)
        msg = message_generator.generate_text_message(
            text="What time is it?",
            chat_id=123456,
            user_name="TestUser",
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        # Step 2: MCP tools process the message
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            PROCESSED_DIR=processed,
        ):
            from src.mcp.inbox_server import (
                handle_check_inbox,
                handle_send_reply,
                handle_mark_processed,
            )

            # Read message
            result = await handle_check_inbox({})
            assert "What time is it?" in result[0].text

            # Send reply
            await handle_send_reply({
                "chat_id": 123456,
                "text": "It's 12:00 PM!",
            })

            # Mark processed
            await handle_mark_processed({"message_id": msg_id})

        # Step 3: Verify outbox has reply for bot to send
        outbox_files = list(outbox.glob("*.json"))
        assert len(outbox_files) == 1

        reply = json.loads(outbox_files[0].read_text())
        assert reply["chat_id"] == 123456
        assert reply["text"] == "It's 12:00 PM!"

        # Step 4: Verify inbox is empty, processed has message
        assert len(list(inbox.glob("*.json"))) == 0
        assert len(list(processed.glob("*.json"))) == 1
