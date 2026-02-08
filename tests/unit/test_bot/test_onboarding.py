"""
Tests for Lobster Bot Onboarding Module

Tests user onboarding tracking, first-message detection,
and the /onboarding command handler.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


class TestOnboardingModule:
    """Tests for the onboarding.py module functions."""

    @pytest.fixture(autouse=True)
    def setup_temp_config(self, tmp_path):
        """Set up a temporary config directory for each test."""
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.onboarded_file = self.config_dir / "onboarded_users.json"

        with patch("src.bot.onboarding.CONFIG_DIR", self.config_dir), \
             patch("src.bot.onboarding.ONBOARDED_FILE", self.onboarded_file):
            yield

    def test_new_user_is_not_onboarded(self):
        """A user with no prior record should not be onboarded."""
        from src.bot.onboarding import is_user_onboarded

        with patch("src.bot.onboarding.ONBOARDED_FILE", self.onboarded_file):
            assert is_user_onboarded(123456) is False

    def test_mark_user_onboarded_creates_file(self):
        """Marking a user as onboarded should create the tracking file."""
        from src.bot.onboarding import mark_user_onboarded, is_user_onboarded

        with patch("src.bot.onboarding.ONBOARDED_FILE", self.onboarded_file):
            mark_user_onboarded(123456)
            assert self.onboarded_file.exists()
            assert is_user_onboarded(123456) is True

    def test_onboarded_user_is_recognized(self):
        """A previously onboarded user should be recognized."""
        from src.bot.onboarding import mark_user_onboarded, is_user_onboarded

        with patch("src.bot.onboarding.ONBOARDED_FILE", self.onboarded_file):
            mark_user_onboarded(999)
            assert is_user_onboarded(999) is True
            assert is_user_onboarded(888) is False

    def test_multiple_users_tracked(self):
        """Multiple users can be tracked independently."""
        from src.bot.onboarding import mark_user_onboarded, is_user_onboarded

        with patch("src.bot.onboarding.ONBOARDED_FILE", self.onboarded_file):
            mark_user_onboarded(111)
            mark_user_onboarded(222)

            assert is_user_onboarded(111) is True
            assert is_user_onboarded(222) is True
            assert is_user_onboarded(333) is False

    def test_onboarded_file_stores_timestamps(self):
        """The onboarded file should store user IDs with timestamps."""
        from src.bot.onboarding import mark_user_onboarded

        with patch("src.bot.onboarding.ONBOARDED_FILE", self.onboarded_file):
            mark_user_onboarded(123456)

            data = json.loads(self.onboarded_file.read_text())
            assert "123456" in data
            assert isinstance(data["123456"], str)  # ISO timestamp

    def test_corrupted_file_handled_gracefully(self):
        """A corrupted onboarded_users.json should not crash."""
        from src.bot.onboarding import is_user_onboarded

        self.onboarded_file.write_text("not valid json{{{")

        with patch("src.bot.onboarding.ONBOARDED_FILE", self.onboarded_file):
            # Should return False rather than raising
            assert is_user_onboarded(123456) is False

    def test_get_onboarding_message_contains_key_info(self):
        """The onboarding message should mention key capabilities."""
        from src.bot.onboarding import get_onboarding_message

        msg = get_onboarding_message("TestUser")

        assert "TestUser" in msg
        assert "Lobster" in msg
        assert "/onboarding" in msg
        assert "Tasks" in msg or "tasks" in msg


class TestOnboardingCommand:
    """Tests for the /onboarding command handler in the bot."""

    @pytest.fixture
    def mock_update(self):
        """Create mock Update object."""
        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_user.first_name = "TestUser"
        update.message.reply_text = AsyncMock()
        return update

    @pytest.fixture
    def mock_context(self):
        """Create mock Context object."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_onboarding_command_sends_message(self, mock_update, mock_context):
        """The /onboarding command should always send the onboarding message."""
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "123456",
            },
        ):
            import importlib
            import src.bot.onboarding as onboarding_module
            import src.bot.lobster_bot as bot_module

            # Patch onboarding to consider user already onboarded
            with patch.object(onboarding_module, "is_user_onboarded", return_value=True), \
                 patch.object(onboarding_module, "mark_user_onboarded"):
                importlib.reload(bot_module)

                await bot_module.onboarding_command(mock_update, mock_context)

                mock_update.message.reply_text.assert_called()
                call_args = mock_update.message.reply_text.call_args
                # The message should contain "Lobster" (from onboarding text)
                msg_text = call_args[0][0]
                assert "Lobster" in msg_text

    @pytest.mark.asyncio
    async def test_onboarding_rejects_unauthorized(self, mock_update, mock_context):
        """The /onboarding command should reject unauthorized users."""
        mock_update.effective_user.id = 999999  # Not authorized

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

            await bot_module.onboarding_command(mock_update, mock_context)

            call_args = mock_update.message.reply_text.call_args[0][0]
            assert "Unauthorized" in call_args


class TestFirstMessageDetection:
    """Tests for first-message onboarding in handle_message."""

    @pytest.fixture
    def mock_update(self, tmp_path):
        """Create mock Update object for a text message."""
        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_user.first_name = "TestUser"
        update.effective_user.username = "testuser"
        update.message.chat_id = 123456
        update.message.message_id = 42
        update.message.text = "Hello"
        update.message.voice = None
        update.message.photo = None
        update.message.document = None
        update.message.reply_text = AsyncMock()
        return update

    @pytest.fixture
    def mock_context(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_new_user_gets_onboarding_before_message(self, mock_update, mock_context, tmp_path):
        """A new user's first message should trigger onboarding."""
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "123456",
            },
        ):
            import importlib
            import src.bot.onboarding as onboarding_module
            import src.bot.lobster_bot as bot_module

            inbox_dir = tmp_path / "inbox"
            inbox_dir.mkdir()

            with patch.object(onboarding_module, "is_user_onboarded", return_value=False), \
                 patch.object(onboarding_module, "mark_user_onboarded") as mock_mark, \
                 patch.object(bot_module, "INBOX_DIR", inbox_dir):
                importlib.reload(bot_module)
                # Re-patch after reload
                bot_module.INBOX_DIR = inbox_dir

                with patch("src.bot.lobster_bot.is_user_onboarded", return_value=False), \
                     patch("src.bot.lobster_bot.mark_user_onboarded") as mock_mark2:
                    await bot_module.handle_message(mock_update, mock_context)

                    # Should have called reply_text at least twice:
                    # once for onboarding, once for "Message received"
                    assert mock_update.message.reply_text.call_count >= 2
