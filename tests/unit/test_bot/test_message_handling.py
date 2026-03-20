"""
Tests for Telegram Bot Message Handling

Tests text and audio/voice message handling.

Changes from original:
- mock_update fixture now explicitly sets message.audio = None and
  message.photo = None and message.document = None so MagicMock's auto-attribute
  creation doesn't trick the `audio_obj = message.voice or message.audio` guard.
- TestHandleVoiceMessage renamed to TestHandleAudioMessage; tests now call
  handle_audio_message(update, context, msg_id, audio_obj) — the function that
  replaced handle_voice_message after the audio handling refactor.
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
        # Explicitly null out all media fields so the audio/photo/document guards
        # in handle_message don't fire when testing plain text handling.
        update.message.voice = None
        update.message.audio = None
        update.message.photo = None
        update.message.document = None
        update.message.reply_to_message = None
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


class TestHandleAudioMessage:
    """Tests for handle_audio_message function.

    The old handle_voice_message was folded into handle_audio_message, which
    now accepts both telegram.Voice and telegram.Audio objects via a shared
    audio_obj parameter.
    """

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
        update.message.caption = None
        # voice is non-None so is_voice = True in handle_audio_message
        update.message.voice = MagicMock()
        update.message.voice.file_id = "voice_file_123"
        update.message.voice.duration = 10
        update.message.voice.mime_type = "audio/ogg"
        # MagicMock.file_name would auto-create a truthy MagicMock (not use the None
        # default in getattr), making json.dumps fail. Explicitly set to None so that
        # getattr(audio_obj, "file_name", None) falls through to the .ogg fallback.
        update.message.voice.file_name = None
        update.message.audio = None
        update.message.reply_text = AsyncMock()
        # Prevent extract_reply_to_context from generating unserializable MagicMock values
        update.message.reply_to_message = None
        return update

    @pytest.fixture
    def mock_context(self):
        """Create mock Context object with bot."""
        context = MagicMock()

        # Create mock file object
        mock_file = MagicMock()
        mock_file.download_to_drive = AsyncMock()
        context.bot.get_file = AsyncMock(return_value=mock_file)

        return context

    @pytest.mark.asyncio
    async def test_voice_message_downloaded_and_saved(
        self, mock_voice_update, mock_context, temp_messages_dir
    ):
        """Test that voice message is downloaded and metadata saved to pending-transcription.

        Voice messages must NOT land in inbox/ directly. They are routed to
        pending-transcription/ so the transcription worker can enrich them with
        a 'transcription' field before moving them to inbox/.  Agents only ever
        see the transcribed message.
        """
        inbox = temp_messages_dir / "inbox"
        pending = temp_messages_dir / "pending-transcription"
        pending.mkdir(parents=True, exist_ok=True)
        audio = temp_messages_dir / "audio"
        audio.mkdir(parents=True, exist_ok=True)

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
                    with patch.object(bot_module, "PENDING_TRANSCRIPTION_DIR", pending):
                        msg_id = "test_123"
                        # handle_audio_message requires an explicit audio_obj argument
                        audio_obj = mock_voice_update.message.voice
                        await bot_module.handle_audio_message(
                            mock_voice_update, mock_context, msg_id, audio_obj
                        )

                        # Voice message must go to pending-transcription/, NOT inbox/
                        inbox_files = list(inbox.glob("*.json"))
                        assert len(inbox_files) == 0, (
                            "Voice message must not be written to inbox/ directly; "
                            "it should go to pending-transcription/ for auto-transcription"
                        )

                        pending_files = list(pending.glob("*.json"))
                        assert len(pending_files) == 1

                        content = json.loads(pending_files[0].read_text())
                        assert content["type"] == "voice"
                        assert content["audio_duration"] == 10
                        assert "audio_file" in content

    @pytest.mark.asyncio
    async def test_voice_message_sends_acknowledgment(
        self, mock_voice_update, mock_context, temp_messages_dir
    ):
        """Test that voice message acknowledgment is sent."""
        inbox = temp_messages_dir / "inbox"
        pending = temp_messages_dir / "pending-transcription"
        pending.mkdir(parents=True, exist_ok=True)
        audio = temp_messages_dir / "audio"
        audio.mkdir(parents=True, exist_ok=True)

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
                    with patch.object(bot_module, "PENDING_TRANSCRIPTION_DIR", pending):
                        audio_obj = mock_voice_update.message.voice
                        await bot_module.handle_audio_message(
                            mock_voice_update, mock_context, "test_123", audio_obj
                        )

                        mock_voice_update.message.reply_text.assert_called()
                        call_args = mock_voice_update.message.reply_text.call_args[0][0]
                        assert "voice" in call_args.lower() or "transcrib" in call_args.lower()
