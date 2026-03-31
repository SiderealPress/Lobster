"""
Unit tests for handle_my_chat_member in lobster_bot.py.

Covers the three main branches of the handler:
  (a) Bot added by whitelisted user  — group whitelisted + all ALLOWED_USERS seeded
  (b) Bot added by non-whitelisted user — bot leaves the group
  (c) Bot removed from group — handler exits cleanly (no error, no whitelist write)
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
import importlib


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chat_member_update(
    new_status: str,
    chat_id: int,
    chat_title: str,
    chat_type: str,
    adder_id: int | None,
) -> MagicMock:
    """Build a minimal mock Update that mimics a ChatMemberUpdated event.

    new_status: "member", "administrator", "left", or "kicked"
    adder_id: None to simulate an unknown inviter
    """
    update = MagicMock()

    event = MagicMock()
    event.new_chat_member.status = new_status
    event.chat.id = chat_id
    event.chat.title = chat_title
    event.chat.type = chat_type

    if adder_id is not None:
        event.from_user = MagicMock()
        event.from_user.id = adder_id
    else:
        event.from_user = None

    update.my_chat_member = event
    return update


def _make_context() -> MagicMock:
    """Build a mock PTB Context with an async bot."""
    ctx = MagicMock()
    ctx.bot.leave_chat = AsyncMock()
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandleMyChatMember:
    """Tests for handle_my_chat_member."""

    ENV = {
        "TELEGRAM_BOT_TOKEN": "test_token",
        "TELEGRAM_ALLOWED_USERS": "111,222",
    }

    def _reload_bot(self):
        """Reload lobster_bot with the whitelisted env and return the module."""
        import src.bot.lobster_bot as bot_module
        importlib.reload(bot_module)
        return bot_module

    @pytest.mark.asyncio
    async def test_whitelisted_adder_enables_group_and_seeds_users(self):
        """Bot added by whitelisted user: whitelist write with group enabled + users seeded."""
        with patch.dict(os.environ, self.ENV):
            bot = self._reload_bot()

        update = _make_chat_member_update(
            new_status="member",
            chat_id=-100999,
            chat_title="My Group",
            chat_type="group",
            adder_id=111,
        )
        ctx = _make_context()

        mock_store = {"groups": {}}
        enabled_store = {"groups": {"-100999": {"enabled": True, "title": "My Group", "users": {}}}}
        seeded_store = {
            "groups": {
                "-100999": {"enabled": True, "title": "My Group", "users": {"111": True, "222": True}}
            }
        }

        with patch.dict(os.environ, self.ENV):
            bot = self._reload_bot()
            with (
                patch.object(bot, "_GROUP_GATING_ENABLED", True),
                patch.object(bot, "load_whitelist", return_value=mock_store),
                patch.object(bot, "enable_group", return_value=enabled_store) as mock_enable,
                patch.object(bot, "add_allowed_user", return_value=seeded_store) as mock_add,
                patch.object(bot, "save_whitelist") as mock_save,
            ):
                await bot.handle_my_chat_member(update, ctx)

        mock_enable.assert_called_once_with(-100999, "My Group", mock_store)
        assert mock_add.call_count == 2  # once per ALLOWED_USERS entry
        mock_save.assert_called_once_with(seeded_store)
        ctx.bot.leave_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_whitelisted_adder_bot_leaves_group(self):
        """Bot added by non-whitelisted user: bot must call leave_chat."""
        with patch.dict(os.environ, self.ENV):
            bot = self._reload_bot()

        update = _make_chat_member_update(
            new_status="member",
            chat_id=-100777,
            chat_title="Stranger's Group",
            chat_type="supergroup",
            adder_id=999,  # not in ALLOWED_USERS
        )
        ctx = _make_context()

        with patch.dict(os.environ, self.ENV):
            bot = self._reload_bot()
            with (
                patch.object(bot, "_GROUP_GATING_ENABLED", True),
                patch.object(bot, "save_whitelist") as mock_save,
            ):
                await bot.handle_my_chat_member(update, ctx)

        ctx.bot.leave_chat.assert_awaited_once_with(-100777)
        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_removed_exits_cleanly(self):
        """Bot removed from group: handler returns without error or whitelist write."""
        with patch.dict(os.environ, self.ENV):
            bot = self._reload_bot()

        update = _make_chat_member_update(
            new_status="left",
            chat_id=-100555,
            chat_title="Some Group",
            chat_type="group",
            adder_id=111,
        )
        ctx = _make_context()

        with patch.dict(os.environ, self.ENV):
            bot = self._reload_bot()
            with (
                patch.object(bot, "_GROUP_GATING_ENABLED", True),
                patch.object(bot, "save_whitelist") as mock_save,
            ):
                await bot.handle_my_chat_member(update, ctx)

        ctx.bot.leave_chat.assert_not_called()
        mock_save.assert_not_called()
