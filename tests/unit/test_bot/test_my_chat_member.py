"""
Tests for handle_my_chat_member

Covers:
- Whitelisted user adds bot: group whitelisted, no leave
- Non-whitelisted user adds bot: bot leaves, group NOT whitelisted
- Bot removed from group: group removed from whitelist
- Idempotent: adding same group twice is safe (enable_group is idempotent)
"""

import importlib
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


def _make_member_update(chat_id, chat_type, adder_id, new_status):
    """Build a minimal mock Update for a my_chat_member event."""
    update = MagicMock()
    event = MagicMock()
    event.new_chat_member.status = new_status
    event.chat.id = chat_id
    event.chat.type = chat_type
    event.chat.title = f"Group {chat_id}"
    event.from_user.id = adder_id
    update.my_chat_member = event
    return update


def _make_context(bot=None):
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.bot.leave_chat = AsyncMock()
    return ctx


class TestHandleMyChatMember:
    """Tests for handle_my_chat_member."""

    @pytest.mark.asyncio
    async def test_whitelisted_user_adds_bot_group_is_whitelisted(self, tmp_path):
        """Whitelisted adder triggers whitelist write; bot does not leave."""
        whitelist_file = tmp_path / "group-whitelist.json"

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "111,222",
            },
        ):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_member_update(
                chat_id=-100123,
                chat_type="supergroup",
                adder_id=111,
                new_status="member",
            )
            ctx = _make_context()

            with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
                 patch("multiplayer_telegram_bot.whitelist._default_whitelist_path", return_value=whitelist_file):
                await bot_module.handle_my_chat_member(update, ctx)

            # Bot must NOT leave
            ctx.bot.leave_chat.assert_not_called()

            # Group must be in whitelist
            import json
            store = json.loads(whitelist_file.read_text())
            key = str(-100123)
            assert key in store["groups"]
            assert store["groups"][key]["enabled"] is True
            # Both allowed users seeded
            assert 111 in store["groups"][key]["allowed_user_ids"]
            assert 222 in store["groups"][key]["allowed_user_ids"]

    @pytest.mark.asyncio
    async def test_non_whitelisted_user_adds_bot_leaves_and_not_whitelisted(self, tmp_path):
        """Non-whitelisted adder: bot leaves immediately and group is NOT written to whitelist."""
        whitelist_file = tmp_path / "group-whitelist.json"

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "111",
            },
        ):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_member_update(
                chat_id=-100456,
                chat_type="group",
                adder_id=999,  # not in ALLOWED_USERS
                new_status="member",
            )
            ctx = _make_context()

            with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
                 patch("multiplayer_telegram_bot.whitelist._default_whitelist_path", return_value=whitelist_file):
                await bot_module.handle_my_chat_member(update, ctx)

            # Bot must leave
            ctx.bot.leave_chat.assert_awaited_once_with(-100456)

            # Group must NOT be in whitelist
            assert not whitelist_file.exists()

    @pytest.mark.asyncio
    async def test_bot_removed_from_group_removes_from_whitelist(self, tmp_path):
        """Bot removed (status=left) removes the group entry from the whitelist."""
        import json
        from multiplayer_telegram_bot.whitelist import enable_group, save_whitelist

        whitelist_file = tmp_path / "group-whitelist.json"
        # Seed whitelist with the group already present
        store = enable_group(-100789, "My Group", {"groups": {}})
        with patch("multiplayer_telegram_bot.whitelist._default_whitelist_path", return_value=whitelist_file):
            save_whitelist(store)

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "111",
            },
        ):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_member_update(
                chat_id=-100789,
                chat_type="supergroup",
                adder_id=111,
                new_status="left",
            )
            ctx = _make_context()

            with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
                 patch("multiplayer_telegram_bot.whitelist._default_whitelist_path", return_value=whitelist_file):
                await bot_module.handle_my_chat_member(update, ctx)

        # Group must be gone from whitelist
        result = json.loads(whitelist_file.read_text())
        assert str(-100789) not in result["groups"]

    @pytest.mark.asyncio
    async def test_adding_same_group_twice_is_idempotent(self, tmp_path):
        """Adding bot to the same group twice produces the same whitelist state."""
        import json
        whitelist_file = tmp_path / "group-whitelist.json"

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test_token",
                "TELEGRAM_ALLOWED_USERS": "111,222",
            },
        ):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)

            update = _make_member_update(
                chat_id=-100321,
                chat_type="supergroup",
                adder_id=111,
                new_status="member",
            )
            ctx = _make_context()

            with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
                 patch("multiplayer_telegram_bot.whitelist._default_whitelist_path", return_value=whitelist_file):
                # First add
                await bot_module.handle_my_chat_member(update, ctx)
                store_after_first = json.loads(whitelist_file.read_text())

                # Second add (re-add same group)
                await bot_module.handle_my_chat_member(update, ctx)
                store_after_second = json.loads(whitelist_file.read_text())

        # Both stores must be identical
        assert store_after_first == store_after_second

        key = str(-100321)
        assert store_after_second["groups"][key]["enabled"] is True
        # No duplicate user IDs
        user_ids = store_after_second["groups"][key]["allowed_user_ids"]
        assert len(user_ids) == len(set(user_ids))
