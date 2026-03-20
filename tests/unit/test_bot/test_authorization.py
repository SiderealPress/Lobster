"""
Tests for Telegram Bot Authorization

Tests user authorization and access control.

Note: The is_authorized() function was removed from lobster_bot.py — authorization
is now performed inline via `user.id not in ALLOWED_USERS` in each handler, using
the module-level ALLOWED_USERS list.  Tests have been updated to:
  1. Verify the ALLOWED_USERS list is populated correctly from the env var.
  2. Test handler-level authorization behavior (the end effect is the same).
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import os


class TestAllowedUsersConfig:
    """Tests for ALLOWED_USERS configuration from environment."""

    def test_allowed_users_populated_from_env(self):
        """ALLOWED_USERS is built from TELEGRAM_ALLOWED_USERS env var."""
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "123456,789012",
            },
        ):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            assert 123456 in bot_module.ALLOWED_USERS
            assert 789012 in bot_module.ALLOWED_USERS

    def test_unauthorized_user_not_in_allowed_users(self):
        """A user not in TELEGRAM_ALLOWED_USERS is absent from ALLOWED_USERS."""
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

            assert 999999 not in bot_module.ALLOWED_USERS

    def test_single_user_in_allowed_users(self):
        """Single allowed user is correctly parsed."""
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

            assert 123456 in bot_module.ALLOWED_USERS
            assert 654321 not in bot_module.ALLOWED_USERS


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
            # The rejection message uses "not authorized" (case-insensitive match)
            assert "not authorized" in call_args.lower() or "unauthorized" in call_args.lower()
