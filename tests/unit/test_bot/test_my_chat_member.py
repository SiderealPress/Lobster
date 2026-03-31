"""
Tests for handle_my_chat_member — Phase 1 group chat support.

Covers:
- Bot added by a whitelisted user → whitelist write attempted
- Bot added by a non-whitelisted user → whitelist unchanged
- Bot removed from a group → log only, no writes
- No my_chat_member event → early return (no-op)
- Whitelist write failure → exception logged, no crash
- _GROUP_GATING_ENABLED=False → log warning, no whitelist write

Design note: all whitelist functions are patched at the bot module level because
they are imported there via a soft-import block. Tests control _GROUP_GATING_ENABLED
via patch.object, bypassing the ImportError path.
"""

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Ensure multiplayer-telegram-bot skill is importable in tests.
# Test file lives at tests/unit/test_bot/test_my_chat_member.py, so
# parents[3] = repo root, then descend into lobster-shop.
# ---------------------------------------------------------------------------
_SKILL_SRC = str(
    Path(__file__).resolve().parents[3]  # repo root
    / "lobster-shop" / "multiplayer-telegram-bot" / "src"
)
if _SKILL_SRC not in sys.path:
    sys.path.insert(0, _SKILL_SRC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chat(chat_id: int = -100123456, title: str = "Test Group", chat_type: str = "supergroup"):
    chat = MagicMock()
    chat.id = chat_id
    chat.title = title
    chat.type = chat_type
    return chat


def _make_user(user_id: int = 6645894734):
    user = MagicMock()
    user.id = user_id
    return user


def _make_chat_member_status(status: str):
    member = MagicMock()
    member.status = status
    return member


def _make_update(
    *,
    has_event: bool = True,
    new_status: str = "member",
    chat_id: int = -100123456,
    chat_type: str = "supergroup",
    adder_id: int = 6645894734,
    adder_none: bool = False,
):
    """Build a minimal fake Update object for my_chat_member."""
    update = MagicMock()
    if not has_event:
        update.my_chat_member = None
        return update

    event = MagicMock()
    event.new_chat_member = _make_chat_member_status(new_status)
    event.chat = _make_chat(chat_id=chat_id, chat_type=chat_type)
    event.from_user = None if adder_none else _make_user(adder_id)
    update.my_chat_member = event
    return update


# ---------------------------------------------------------------------------
# Module reload helper — keeps tests independent of each other
# ---------------------------------------------------------------------------

def _load_bot_module(allowed_users: str = "6645894734,5717728951"):
    """Reload lobster_bot with the given TELEGRAM_ALLOWED_USERS env var."""
    env = {
        "TELEGRAM_BOT_TOKEN": "fake_token_for_tests",
        "TELEGRAM_ALLOWED_USERS": allowed_users,
    }
    with patch.dict(os.environ, env):
        import src.bot.lobster_bot as bot_module
        importlib.reload(bot_module)
        return bot_module


# ---------------------------------------------------------------------------
# Shared mock factory for whitelist functions
# ---------------------------------------------------------------------------

def _whitelist_mocks(load_return=None, enable_return=None, add_return=None):
    """Return (load_mock, enable_mock, add_mock, save_mock) as MagicMocks."""
    empty = {"groups": {}}
    load_mock = MagicMock(return_value=load_return or empty)
    enable_mock = MagicMock(return_value=enable_return or empty)
    add_mock = MagicMock(return_value=add_return or empty)
    save_mock = MagicMock()
    return load_mock, enable_mock, add_mock, save_mock


def _patch_whitelist(bot_module, load_mock, enable_mock, add_mock, save_mock):
    """Context manager stack that patches whitelist functions on the bot module."""
    return (
        patch.object(bot_module, "load_whitelist", load_mock),
        patch.object(bot_module, "enable_group", enable_mock),
        patch.object(bot_module, "add_allowed_user", add_mock),
        patch.object(bot_module, "save_whitelist", save_mock),
    )


# ---------------------------------------------------------------------------
# Tests: no event present
# ---------------------------------------------------------------------------

class TestNoChatMemberEvent:
    @pytest.mark.asyncio
    async def test_no_event_returns_early(self):
        """When update.my_chat_member is None, handler returns immediately."""
        bot_module = _load_bot_module()
        update = _make_update(has_event=False)
        context = MagicMock()
        load_mock, enable_mock, add_mock, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", load_mock), \
                 patch.object(bot_module, "save_whitelist", save_mock):
                await bot_module.handle_my_chat_member(update, context)

        load_mock.assert_not_called()
        save_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: bot added by whitelisted user
# ---------------------------------------------------------------------------

class TestBotAddedByWhitelistedUser:
    @pytest.mark.asyncio
    async def test_save_called_for_whitelisted_adder(self):
        """Whitelisted adder → save_whitelist called once."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=6645894734)
        context = MagicMock()
        load_mock, enable_mock, add_mock, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", load_mock), \
                 patch.object(bot_module, "enable_group", enable_mock), \
                 patch.object(bot_module, "add_allowed_user", add_mock), \
                 patch.object(bot_module, "save_whitelist", save_mock):
                await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_enable_group_called_with_correct_chat_id(self):
        """enable_group is called with the incoming chat's ID."""
        bot_module = _load_bot_module("6645894734,5717728951")
        chat_id = -100999888
        update = _make_update(new_status="member", adder_id=6645894734, chat_id=chat_id)
        context = MagicMock()
        load_mock, enable_mock, add_mock, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", load_mock), \
                 patch.object(bot_module, "enable_group", enable_mock), \
                 patch.object(bot_module, "add_allowed_user", add_mock), \
                 patch.object(bot_module, "save_whitelist", save_mock):
                await bot_module.handle_my_chat_member(update, context)

        first_call_args = enable_mock.call_args[0]
        assert first_call_args[0] == chat_id

    @pytest.mark.asyncio
    async def test_all_allowed_users_seeded(self):
        """add_allowed_user is called once for each user in ALLOWED_USERS."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=6645894734)
        context = MagicMock()
        load_mock, enable_mock, add_mock, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", load_mock), \
                 patch.object(bot_module, "enable_group", enable_mock), \
                 patch.object(bot_module, "add_allowed_user", add_mock), \
                 patch.object(bot_module, "save_whitelist", save_mock):
                await bot_module.handle_my_chat_member(update, context)

        assert add_mock.call_count == 2
        added_user_ids = {c[0][0] for c in add_mock.call_args_list}
        assert 6645894734 in added_user_ids
        assert 5717728951 in added_user_ids

    @pytest.mark.asyncio
    async def test_administrator_status_also_triggers_whitelist(self):
        """status=administrator (bot promoted) also counts as bot-added."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="administrator", adder_id=6645894734)
        context = MagicMock()
        load_mock, enable_mock, add_mock, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", load_mock), \
                 patch.object(bot_module, "enable_group", enable_mock), \
                 patch.object(bot_module, "add_allowed_user", add_mock), \
                 patch.object(bot_module, "save_whitelist", save_mock):
                await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_type_also_auto_whitelisted(self):
        """chat.type=group (not only supergroup) triggers the whitelist write."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=6645894734, chat_type="group")
        context = MagicMock()
        load_mock, enable_mock, add_mock, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", load_mock), \
                 patch.object(bot_module, "enable_group", enable_mock), \
                 patch.object(bot_module, "add_allowed_user", add_mock), \
                 patch.object(bot_module, "save_whitelist", save_mock):
                await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_whitelist_written_to_disk_end_to_end(self, tmp_path):
        """Full I/O round-trip: whitelisted adder → group appears in the JSON file."""
        from multiplayer_telegram_bot.whitelist import (
            load_whitelist, save_whitelist, enable_group, add_allowed_user
        )

        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=6645894734, chat_id=-100123456)
        context = MagicMock()

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        wl_path = config_dir / "group-whitelist.json"

        # Wrap the real functions so they use tmp_path
        def _load():
            return load_whitelist(wl_path)
        def _save(store):
            return save_whitelist(store, wl_path)
        def _enable(chat_id, name, store):
            return enable_group(chat_id, name, store)
        def _add(uid, chat_id, store):
            return add_allowed_user(uid, chat_id, store)

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", _load), \
                 patch.object(bot_module, "enable_group", _enable), \
                 patch.object(bot_module, "add_allowed_user", _add), \
                 patch.object(bot_module, "save_whitelist", _save):
                await bot_module.handle_my_chat_member(update, context)

        data = json.loads(wl_path.read_text())
        group = data["groups"][str(-100123456)]
        assert group["enabled"] is True
        assert 6645894734 in group["allowed_user_ids"]
        assert 5717728951 in group["allowed_user_ids"]


# ---------------------------------------------------------------------------
# Tests: bot added by non-whitelisted user
# ---------------------------------------------------------------------------

class TestBotAddedByNonWhitelistedUser:
    @pytest.mark.asyncio
    async def test_whitelist_not_written(self):
        """Non-whitelisted adder → save_whitelist never called."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=9999999)
        context = MagicMock()
        _, _, _, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
             patch.object(bot_module, "save_whitelist", save_mock):
            await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_adder_is_none_does_not_whitelist(self):
        """from_user=None (anonymous admin) → whitelist not written."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_none=True)
        context = MagicMock()
        _, _, _, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
             patch.object(bot_module, "save_whitelist", save_mock):
            await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_private_chat_ignored(self):
        """chat.type=private is not a group — handler takes no action."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=6645894734, chat_type="private")
        context = MagicMock()
        _, _, _, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
             patch.object(bot_module, "save_whitelist", save_mock):
            await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: bot removed from group
# ---------------------------------------------------------------------------

class TestBotRemovedFromGroup:
    @pytest.mark.asyncio
    async def test_left_status_does_not_write_whitelist(self):
        """status=left → only log, no whitelist write."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="left")
        context = MagicMock()
        _, _, _, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
             patch.object(bot_module, "save_whitelist", save_mock):
            await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_kicked_status_does_not_write_whitelist(self):
        """status=kicked → only log, no whitelist write."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="kicked")
        context = MagicMock()
        _, _, _, save_mock = _whitelist_mocks()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True), \
             patch.object(bot_module, "save_whitelist", save_mock):
            await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _GROUP_GATING_ENABLED = False
# ---------------------------------------------------------------------------

class TestGatingDisabled:
    @pytest.mark.asyncio
    async def test_no_whitelist_write_when_gating_disabled(self):
        """When _GROUP_GATING_ENABLED=False, no whitelist write even for whitelisted adder."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=6645894734)
        context = MagicMock()
        # save_whitelist may not exist on the module when _GROUP_GATING_ENABLED=False
        # (soft-import failed); use create=True to inject a sentinel mock
        save_mock = MagicMock()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", False):
            with patch.object(bot_module, "save_whitelist", save_mock, create=True):
                await bot_module.handle_my_chat_member(update, context)

        save_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: whitelist write failure
# ---------------------------------------------------------------------------

class TestWhitelistWriteFailure:
    @pytest.mark.asyncio
    async def test_exception_logged_not_raised(self):
        """If save_whitelist raises, exception is caught and logged — handler does not crash."""
        bot_module = _load_bot_module("6645894734,5717728951")
        update = _make_update(new_status="member", adder_id=6645894734)
        context = MagicMock()

        empty_store = {"groups": {}}
        load_mock = MagicMock(return_value=empty_store)
        enable_mock = MagicMock(return_value=empty_store)
        add_mock = MagicMock(return_value=empty_store)
        save_mock = MagicMock(side_effect=OSError("disk full"))

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", True):
            with patch.object(bot_module, "load_whitelist", load_mock), \
                 patch.object(bot_module, "enable_group", enable_mock), \
                 patch.object(bot_module, "add_allowed_user", add_mock), \
                 patch.object(bot_module, "save_whitelist", save_mock):
                # Must not raise — exception is swallowed and logged
                await bot_module.handle_my_chat_member(update, context)
