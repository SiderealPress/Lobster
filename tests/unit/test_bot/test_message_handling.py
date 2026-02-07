"""
Tests for Telegram Bot Message Handling

Tests text and voice message handling.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import os


class TestHandleMessage:
    """Tests for handle_message function."""

    @pytest.fixture
    def mock_update(self):
        """Create mock Update object for text message."""
        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_user.first_name = "TestUser"
        update.effective_user.username = "testuser"
        update.message.message_id = 1
        update.message.chat_id = 123456
        update.message.text = "Hello, Lobster!"
        update.message.voice = None
        update.message.reply_text = AsyncMock()
        return update

    @pytest.fixture
    def mock_context(self):
        """Create mock Context object."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_authorized_user_message_saved_to_inbox(
        self, mock_update, mock_context, temp_messages_dir
    ):
        """Test that authorized user's message is saved to inbox."""
        inbox = temp_messages_dir / "inbox"

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

            # Patch the INBOX_DIR
            with patch.object(bot_module, "INBOX_DIR", inbox):
                await bot_module.handle_message(mock_update, mock_context)

                # Check that a file was created in inbox
                files = list(inbox.glob("*.json"))
                assert len(files) == 1

                # Verify content
                content = json.loads(files[0].read_text())
                assert content["text"] == "Hello, Lobster!"
                assert content["user_id"] == 123456
                assert content["source"] == "telegram"

    @pytest.mark.asyncio
    async def test_unauthorized_user_message_ignored(
        self, mock_update, mock_context, temp_messages_dir
    ):
        """Test that unauthorized user's message is ignored."""
        mock_update.effective_user.id = 999999  # Not authorized
        inbox = temp_messages_dir / "inbox"

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

            with patch.object(bot_module, "INBOX_DIR", inbox):
                await bot_module.handle_message(mock_update, mock_context)

                # No file should be created
                files = list(inbox.glob("*.json"))
                assert len(files) == 0

    @pytest.mark.asyncio
    async def test_empty_text_is_ignored(
        self, mock_update, mock_context, temp_messages_dir
    ):
        """Test that empty text messages are ignored."""
        mock_update.message.text = None
        inbox = temp_messages_dir / "inbox"

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

            with patch.object(bot_module, "INBOX_DIR", inbox):
                await bot_module.handle_message(mock_update, mock_context)

                files = list(inbox.glob("*.json"))
                assert len(files) == 0

    @pytest.mark.asyncio
    async def test_sends_acknowledgment(
        self, mock_update, mock_context, temp_messages_dir
    ):
        """Test that acknowledgment is sent after receiving message."""
        inbox = temp_messages_dir / "inbox"

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

            with patch.object(bot_module, "INBOX_DIR", inbox):
                await bot_module.handle_message(mock_update, mock_context)

                mock_update.message.reply_text.assert_called_once()
                call_args = mock_update.message.reply_text.call_args[0][0]
                assert "received" in call_args.lower() or "processing" in call_args.lower()


class TestHandleVoiceMessage:
    """Tests for voice message handling."""

    @pytest.fixture
    def mock_voice_update(self):
        """Create mock Update object for voice message."""
        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_user.first_name = "TestUser"
        update.effective_user.username = "testuser"
        update.message.message_id = 1
        update.message.chat_id = 123456
        update.message.text = None
        update.message.voice = MagicMock()
        update.message.voice.file_id = "voice_file_123"
        update.message.voice.duration = 10
        update.message.voice.mime_type = "audio/ogg"
        update.message.reply_text = AsyncMock()
        return update

    @pytest.fixture
    def mock_context(self):
        """Create mock Context object with bot."""
        context = MagicMock()
        context.bot.get_file = AsyncMock()

        # Create mock file object
        mock_file = MagicMock()
        mock_file.download_to_drive = AsyncMock()
        context.bot.get_file.return_value = mock_file

        return context

    @pytest.mark.asyncio
    async def test_voice_message_downloaded_and_saved(
        self, mock_voice_update, mock_context, temp_messages_dir
    ):
        """Test that voice message is downloaded and metadata saved."""
        inbox = temp_messages_dir / "inbox"
        audio = temp_messages_dir / "audio"

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

            with patch.object(bot_module, "INBOX_DIR", inbox):
                with patch.object(bot_module, "AUDIO_DIR", audio):
                    msg_id = "test_123"
                    await bot_module.handle_voice_message(
                        mock_voice_update, mock_context, msg_id
                    )

                    # Check that file was created in inbox
                    files = list(inbox.glob("*.json"))
                    assert len(files) == 1

                    content = json.loads(files[0].read_text())
                    assert content["type"] == "voice"
                    assert content["audio_duration"] == 10
                    assert "audio_file" in content

    @pytest.mark.asyncio
    async def test_voice_message_sends_acknowledgment(
        self, mock_voice_update, mock_context, temp_messages_dir
    ):
        """Test that voice message acknowledgment is sent."""
        inbox = temp_messages_dir / "inbox"
        audio = temp_messages_dir / "audio"

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

            with patch.object(bot_module, "INBOX_DIR", inbox):
                with patch.object(bot_module, "AUDIO_DIR", audio):
                    await bot_module.handle_voice_message(
                        mock_voice_update, mock_context, "test_123"
                    )

                    mock_voice_update.message.reply_text.assert_called()
                    call_args = mock_voice_update.message.reply_text.call_args[0][0]
                    assert "voice" in call_args.lower() or "transcrib" in call_args.lower()
