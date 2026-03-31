"""
Tests for group management command handlers.

Tests the four command handlers added in Phase 3:
  /enable_group_bot, /whitelist, /unwhitelist, /list_groups

Each handler must:
  - Silently drop commands from non-ALLOWED_USERS (no reply)
  - Reply with an error for commands sent from non-DM chats (group chats)
    (only /enable_group_bot sends an error; the rest silently drop)
  - Delegate to the multiplayer_telegram_bot skill's command functions
  - Reply to the user with the result
"""

import json
import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env():
    return {
        "TELEGRAM_BOT_TOKEN": "test_token",
        "TELEGRAM_ALLOWED_USERS": "111111,222222",
    }


def _make_update(
    *,
    user_id: int = 111111,
    chat_type: str = "private",
    message_text: str = "/enable_group_bot -100123456",
) -> MagicMock:
    """Build a minimal mock Update object for command handler testing."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.type = chat_type
    update.message.text = message_text
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    return MagicMock()


def _minimal_whitelist(group_id: str = "-100123456") -> dict:
    return {
        "groups": {
            group_id: {
                "name": "Test Group",
                "enabled": True,
                "allowed_user_ids": [111111],
            }
        }
    }


# ---------------------------------------------------------------------------
# enable_group_bot_command
# ---------------------------------------------------------------------------

class TestEnableGroupBotCommand:
    """Tests for /enable_group_bot handler."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_is_silently_dropped(self):
        """Non-ALLOWED_USER gets no reply at all."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(user_id=999999)
            await bot_module.enable_group_bot_command(update, _make_context())

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_chat_gets_error_reply(self):
        """Command sent from a group chat returns a DM-only error message."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(chat_type="group")
            await bot_module.enable_group_bot_command(update, _make_context())

            update.message.reply_text.assert_called_once()
            reply = update.message.reply_text.call_args[0][0]
            assert "private" in reply.lower() or "DM" in reply

    @pytest.mark.asyncio
    async def test_valid_command_enables_group(self):
        """Valid command from allowed user in DM enables the group."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"groups": {}}, f)
            wl_path = f.name

        try:
            with patch.dict(os.environ, _make_env()):
                import importlib
                import src.bot.lobster_bot as bot_module
                importlib.reload(bot_module)

                update = _make_update(
                    message_text=f"/enable_group_bot -100123456 My Test Group"
                )

                # Patch handle_enable_group_bot to avoid real file I/O
                from multiplayer_telegram_bot.commands import CommandResult
                mock_result = CommandResult(
                    success=True,
                    reply="Group My Test Group (-100123456) is now enabled for Lobster bot access.",
                )

                with patch.object(
                    bot_module, "handle_enable_group_bot", return_value=mock_result
                ) as mock_cmd:
                    await bot_module.enable_group_bot_command(update, _make_context())

                    mock_cmd.assert_called_once_with(update.message.text)
                    update.message.reply_text.assert_called_once_with(mock_result.reply)
        finally:
            os.unlink(wl_path)

    @pytest.mark.asyncio
    async def test_invalid_group_id_returns_error(self):
        """Positive group ID produces an error reply (not a crash)."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(message_text="/enable_group_bot 12345")

            from multiplayer_telegram_bot.commands import CommandResult
            error_result = CommandResult(
                success=False,
                reply="Group ID must be negative (got 12345). Group IDs start with - or -100.",
            )

            with patch.object(
                bot_module, "handle_enable_group_bot", return_value=error_result
            ):
                await bot_module.enable_group_bot_command(update, _make_context())

            update.message.reply_text.assert_called_once_with(error_result.reply)

    @pytest.mark.asyncio
    async def test_skill_not_installed_returns_unavailable(self):
        """When _GROUP_COMMANDS_ENABLED is False, replies with unavailable message."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update()
            original = bot_module._GROUP_COMMANDS_ENABLED
            bot_module._GROUP_COMMANDS_ENABLED = False
            try:
                await bot_module.enable_group_bot_command(update, _make_context())
            finally:
                bot_module._GROUP_COMMANDS_ENABLED = original

            update.message.reply_text.assert_called_once()
            reply = update.message.reply_text.call_args[0][0]
            assert "not available" in reply.lower()


# ---------------------------------------------------------------------------
# whitelist_command
# ---------------------------------------------------------------------------

class TestWhitelistCommand:
    """Tests for /whitelist handler."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_is_silently_dropped(self):
        """Non-ALLOWED_USER gets no reply."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(user_id=999999, message_text="/whitelist 555 -100123456")
            await bot_module.whitelist_command(update, _make_context())

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_chat_is_silently_dropped(self):
        """Command from a group chat is silently dropped (no reply)."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(
                chat_type="group", message_text="/whitelist 555 -100123456"
            )
            await bot_module.whitelist_command(update, _make_context())

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_command_adds_user(self):
        """Valid /whitelist in DM delegates to handle_whitelist and replies."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(message_text="/whitelist 555555 -100123456")

            from multiplayer_telegram_bot.commands import CommandResult
            mock_result = CommandResult(
                success=True,
                reply="User 555555 added to whitelist for group -100123456.",
            )

            with patch.object(
                bot_module, "handle_whitelist", return_value=mock_result
            ) as mock_cmd:
                await bot_module.whitelist_command(update, _make_context())

                mock_cmd.assert_called_once_with(update.message.text)
                update.message.reply_text.assert_called_once_with(mock_result.reply)


# ---------------------------------------------------------------------------
# unwhitelist_command
# ---------------------------------------------------------------------------

class TestUnwhitelistCommand:
    """Tests for /unwhitelist handler."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_is_silently_dropped(self):
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(user_id=999999, message_text="/unwhitelist 555 -100123456")
            await bot_module.unwhitelist_command(update, _make_context())

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_chat_is_silently_dropped(self):
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(
                chat_type="group", message_text="/unwhitelist 555 -100123456"
            )
            await bot_module.unwhitelist_command(update, _make_context())

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_command_removes_user(self):
        """Valid /unwhitelist in DM delegates to handle_unwhitelist and replies."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(message_text="/unwhitelist 555555 -100123456")

            from multiplayer_telegram_bot.commands import CommandResult
            mock_result = CommandResult(
                success=True,
                reply="User 555555 removed from whitelist for group -100123456.",
            )

            with patch.object(
                bot_module, "handle_unwhitelist", return_value=mock_result
            ) as mock_cmd:
                await bot_module.unwhitelist_command(update, _make_context())

                mock_cmd.assert_called_once_with(update.message.text)
                update.message.reply_text.assert_called_once_with(mock_result.reply)


# ---------------------------------------------------------------------------
# list_groups_command
# ---------------------------------------------------------------------------

class TestListGroupsCommand:
    """Tests for /list_groups handler."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_is_silently_dropped(self):
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(user_id=999999, message_text="/list_groups")
            await bot_module.list_groups_command(update, _make_context())

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_chat_is_silently_dropped(self):
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(chat_type="group", message_text="/list_groups")
            await bot_module.list_groups_command(update, _make_context())

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_whitelist_returns_no_groups(self):
        """When whitelist has no groups, reply says 'No groups configured.'"""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(message_text="/list_groups")

            with patch.object(
                bot_module, "load_whitelist", return_value={"groups": {}}
            ):
                await bot_module.list_groups_command(update, _make_context())

            update.message.reply_text.assert_called_once_with("No groups configured.")

    @pytest.mark.asyncio
    async def test_lists_groups_with_whitelisted_users(self):
        """Reply includes group name, ID, status, and whitelisted user IDs."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(message_text="/list_groups")
            store = _minimal_whitelist()

            with patch.object(bot_module, "load_whitelist", return_value=store):
                await bot_module.list_groups_command(update, _make_context())

            update.message.reply_text.assert_called_once()
            reply = update.message.reply_text.call_args[0][0]
            assert "-100123456" in reply
            assert "Test Group" in reply
            assert "enabled" in reply
            assert "111111" in reply

    @pytest.mark.asyncio
    async def test_lists_disabled_group(self):
        """Disabled groups are labeled as disabled."""
        with patch.dict(os.environ, _make_env()):
            import importlib
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_update(message_text="/list_groups")
            store = {
                "groups": {
                    "-100999888": {
                        "name": "Inactive Group",
                        "enabled": False,
                        "allowed_user_ids": [],
                    }
                }
            }

            with patch.object(bot_module, "load_whitelist", return_value=store):
                await bot_module.list_groups_command(update, _make_context())

            reply = update.message.reply_text.call_args[0][0]
            assert "disabled" in reply
            assert "Inactive Group" in reply
            assert "No whitelisted users" in reply
