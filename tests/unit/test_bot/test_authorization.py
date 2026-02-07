"""
Tests for Telegram Bot Authorization

Tests user authorization and access control.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import os


class TestIsAuthorized:
    """Tests for is_authorized function."""

    def test_authorized_user_returns_true(self):
        """Test that authorized user returns True."""
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "123456,789012",
            },
        ):
            # Need to reimport to pick up env change
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            assert bot_module.is_authorized(123456) is True
            assert bot_module.is_authorized(789012) is True

    def test_unauthorized_user_returns_false(self):
        """Test that unauthorized user returns False."""
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

            assert bot_module.is_authorized(999999) is False

    def test_single_user_authorization(self):
        """Test authorization with single allowed user."""
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

            assert bot_module.is_authorized(123456) is True
            assert bot_module.is_authorized(654321) is False


class TestStartCommand:
    """Tests for /start command handler."""

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
    async def test_authorized_user_gets_welcome(self, mock_update, mock_context):
        """Test that authorized user gets welcome message."""
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

            await bot_module.start_command(mock_update, mock_context)

            mock_update.message.reply_text.assert_called_once()
            call_args = mock_update.message.reply_text.call_args[0][0]
            assert "Hey" in call_args or "Hello" in call_args or "Lobster" in call_args

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_rejected(self, mock_update, mock_context):
        """Test that unauthorized user is rejected."""
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

            await bot_module.start_command(mock_update, mock_context)

            mock_update.message.reply_text.assert_called_once()
            call_args = mock_update.message.reply_text.call_args[0][0]
            assert "Unauthorized" in call_args
