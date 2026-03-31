"""
Tests for Phase 2 group gating in handle_message and handle_edited_message.

Covers:
- _check_group_gating helper: DM path, group/allow, group/drop, group/register
- _group_context_fields: returns empty dict for DMs, group fields for groups
- handle_message: DM allowed, DM blocked, group allowed (source="lobster-group"),
  group blocked (silently dropped), group registration DM
- handle_edited_message: same two-tier check
- Existing DM tests are unaffected (regressions)
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, call


# ---------------------------------------------------------------------------
# Helpers: build a minimal mock update for DMs and group messages
# ---------------------------------------------------------------------------

def _make_dm_update(user_id: int = 123456, text: str = "hello") -> MagicMock:
    """Return a mock Update whose chat.type == 'private'."""
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    user.first_name = "TestUser"
    user.username = "testuser"
    update.effective_user = user

    msg = MagicMock()
    msg.message_id = 1
    msg.chat_id = user_id
    msg.text = text
    msg.voice = None
    msg.audio = None
    msg.photo = None
    msg.document = None
    msg.reply_to_message = None
    msg.reply_text = AsyncMock()

    chat = MagicMock()
    chat.type = "private"
    chat.id = user_id
    chat.title = None
    msg.chat = chat

    update.message = msg
    return update


def _make_group_update(
    user_id: int = 111111,
    chat_id: int = -1001234567890,
    chat_type: str = "group",
    text: str = "group hello",
) -> MagicMock:
    """Return a mock Update whose chat.type is 'group' or 'supergroup'."""
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    user.first_name = "GroupUser"
    user.username = "groupuser"
    update.effective_user = user

    msg = MagicMock()
    msg.message_id = 2
    msg.chat_id = chat_id
    msg.text = text
    msg.voice = None
    msg.audio = None
    msg.photo = None
    msg.document = None
    msg.reply_to_message = None
    msg.reply_text = AsyncMock()

    chat = MagicMock()
    chat.type = chat_type
    chat.id = chat_id
    chat.title = "Test Group"
    msg.chat = chat

    update.message = msg
    return update


def _make_whitelist_store(
    chat_id: int = -1001234567890,
    enabled: bool = True,
    user_ids: list[int] | None = None,
) -> dict:
    """Return a minimal WhitelistStore dict."""
    return {
        "groups": {
            str(chat_id): {
                "name": "Test Group",
                "enabled": enabled,
                "allowed_user_ids": user_ids or [],
            }
        }
    }


# ---------------------------------------------------------------------------
# Pure unit tests: _group_context_fields
# ---------------------------------------------------------------------------

class TestGroupContextFields:
    """_group_context_fields is a pure helper — no async needed."""

    def _get_fn(self):
        import importlib
        import src.bot.lobster_bot as m
        importlib.reload(m)
        return m._group_context_fields

    def _make_chat(self, chat_type: str, chat_id: int = -1001, title: str | None = "G"):
        chat = MagicMock()
        chat.type = chat_type
        chat.id = chat_id
        chat.title = title
        return chat

    def test_dm_returns_empty_dict(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "1"
        }):
            fn = self._get_fn()
            assert fn(self._make_chat("private")) == {}

    def test_group_returns_group_fields(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "1"
        }):
            fn = self._get_fn()
            result = fn(self._make_chat("group", -999, "My Group"))
            assert result["group_chat_id"] == -999
            assert result["group_title"] == "My Group"

    def test_supergroup_returns_group_fields(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "1"
        }):
            fn = self._get_fn()
            result = fn(self._make_chat("supergroup", -888, "Super"))
            assert result["group_chat_id"] == -888
            assert result["group_title"] == "Super"

    def test_channel_returns_empty_dict(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "1"
        }):
            fn = self._get_fn()
            assert fn(self._make_chat("channel")) == {}


# ---------------------------------------------------------------------------
# handle_message — DM path (existing behaviour must be unchanged)
# ---------------------------------------------------------------------------

class TestHandleMessageDM:
    """DM messages go through the ALLOWED_USERS check, unchanged from pre-Phase-2."""

    @pytest.mark.asyncio
    async def test_authorized_dm_is_saved_to_inbox(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        update = _make_dm_update(user_id=123456, text="DM text")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "123456",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox):
                await m.handle_message(update, MagicMock())

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["source"] == "telegram"
            assert data["text"] == "DM text"
            assert data["user_id"] == 123456
            # DM messages must NOT carry group_chat_id
            assert "group_chat_id" not in data

    @pytest.mark.asyncio
    async def test_unauthorized_dm_is_silently_dropped(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        update = _make_dm_update(user_id=999999, text="sneaky")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "123456",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox):
                await m.handle_message(update, MagicMock())

            assert list(inbox.glob("*.json")) == []


# ---------------------------------------------------------------------------
# handle_message — Group path
# ---------------------------------------------------------------------------

class TestHandleMessageGroup:
    """Group messages use the whitelist-based gating."""

    @pytest.mark.asyncio
    async def test_whitelisted_group_user_message_saved(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        chat_id = -1001234567890
        user_id = 111111
        store = _make_whitelist_store(chat_id, enabled=True, user_ids=[user_id])
        update = _make_group_update(user_id=user_id, chat_id=chat_id)

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "999",  # user not in ALLOWED_USERS but in whitelist
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", True), \
                 patch.object(m, "load_whitelist", return_value=store):
                await m.handle_message(update, MagicMock())

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["source"] == "lobster-group"
            assert data["group_chat_id"] == chat_id
            assert data["group_title"] == "Test Group"

    @pytest.mark.asyncio
    async def test_non_whitelisted_group_message_is_dropped(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        chat_id = -1001234567890
        store: dict = {"groups": {}}  # group not in whitelist → DROP_SILENT
        update = _make_group_update(user_id=111111, chat_id=chat_id)

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "999",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", True), \
                 patch.object(m, "load_whitelist", return_value=store):
                await m.handle_message(update, MagicMock())

            assert list(inbox.glob("*.json")) == []
            # Must not reply in the group
            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_group_message_is_dropped(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        chat_id = -1001234567890
        store = _make_whitelist_store(chat_id, enabled=False, user_ids=[111111])
        update = _make_group_update(user_id=111111, chat_id=chat_id)

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "999",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", True), \
                 patch.object(m, "load_whitelist", return_value=store):
                await m.handle_message(update, MagicMock())

            assert list(inbox.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_unknown_user_in_whitelisted_group_gets_registration_dm(
        self, temp_messages_dir
    ):
        inbox = temp_messages_dir / "inbox"
        chat_id = -1001234567890
        # Group is enabled but user_id=888 is NOT in allowed_user_ids
        store = _make_whitelist_store(chat_id, enabled=True, user_ids=[111111])
        update = _make_group_update(user_id=888888, chat_id=chat_id)

        mock_bot = AsyncMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "999",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            # Simulate bot_app.bot.send_message via a module-level bot reference
            mock_bot_app = MagicMock()
            mock_bot_app.bot.send_message = AsyncMock()

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", True), \
                 patch.object(m, "load_whitelist", return_value=store), \
                 patch.object(m, "bot_app", mock_bot_app):
                await m.handle_message(update, MagicMock())

            # Message must NOT reach inbox
            assert list(inbox.glob("*.json")) == []
            # Registration DM must have been sent
            mock_bot_app.bot.send_message.assert_called_once()
            kwargs = mock_bot_app.bot.send_message.call_args
            assert kwargs[1]["chat_id"] == 888888

    @pytest.mark.asyncio
    async def test_group_message_when_gating_disabled_is_dropped(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        update = _make_group_update(user_id=111111)

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "111111",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", False):
                await m.handle_message(update, MagicMock())

            # Even if user is in ALLOWED_USERS, group messages drop when gating unavailable
            assert list(inbox.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_supergroup_message_uses_lobster_group_source(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        chat_id = -1009876543210
        user_id = 222222
        store = _make_whitelist_store(chat_id, enabled=True, user_ids=[user_id])
        update = _make_group_update(user_id=user_id, chat_id=chat_id, chat_type="supergroup")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "999",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", True), \
                 patch.object(m, "load_whitelist", return_value=store):
                await m.handle_message(update, MagicMock())

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["source"] == "lobster-group"


# ---------------------------------------------------------------------------
# handle_edited_message — two-tier gating
# ---------------------------------------------------------------------------

class TestHandleEditedMessageGating:
    """Edited messages apply the same two-tier gating as regular messages."""

    def _make_edited_update(
        self,
        user_id: int = 123456,
        chat_type: str = "private",
        chat_id: int = 123456,
        text: str = "edited text",
    ) -> MagicMock:
        update = MagicMock()
        user = MagicMock()
        user.id = user_id
        user.first_name = "Editor"
        user.username = "editor"
        update.effective_user = user

        msg = MagicMock()
        msg.message_id = 42
        msg.chat_id = chat_id
        msg.text = text
        msg.reply_to_message = None

        chat = MagicMock()
        chat.type = chat_type
        chat.id = chat_id
        chat.title = "Test Group" if chat_type in ("group", "supergroup") else None
        msg.chat = chat

        update.edited_message = msg
        # update.message is None for edited_message events
        update.message = None
        return update

    @pytest.mark.asyncio
    async def test_authorized_dm_edit_reaches_inbox(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        update = self._make_edited_update(user_id=123456, chat_type="private")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "123456",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_MESSAGES", temp_messages_dir):
                await m.handle_edited_message(update, MagicMock())

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["source"] == "telegram"
            assert "group_chat_id" not in data

    @pytest.mark.asyncio
    async def test_unauthorized_dm_edit_is_dropped(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        update = self._make_edited_update(user_id=999999, chat_type="private")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "123456",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox):
                await m.handle_edited_message(update, MagicMock())

            assert list(inbox.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_whitelisted_group_edit_reaches_inbox_with_group_source(
        self, temp_messages_dir
    ):
        inbox = temp_messages_dir / "inbox"
        chat_id = -1001234567890
        user_id = 111111
        store = _make_whitelist_store(chat_id, enabled=True, user_ids=[user_id])
        update = self._make_edited_update(
            user_id=user_id, chat_type="group", chat_id=chat_id
        )

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "999",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", True), \
                 patch.object(m, "load_whitelist", return_value=store), \
                 patch.object(m, "_MESSAGES", temp_messages_dir):
                await m.handle_edited_message(update, MagicMock())

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["source"] == "lobster-group"
            assert data["group_chat_id"] == chat_id

    @pytest.mark.asyncio
    async def test_non_whitelisted_group_edit_is_dropped(self, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        store: dict = {"groups": {}}
        update = self._make_edited_update(
            user_id=111111, chat_type="group", chat_id=-1001234567890
        )

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_ALLOWED_USERS": "999",
        }):
            import importlib
            import src.bot.lobster_bot as m
            importlib.reload(m)

            with patch.object(m, "INBOX_DIR", inbox), \
                 patch.object(m, "_GROUP_GATING_ENABLED", True), \
                 patch.object(m, "load_whitelist", return_value=store):
                await m.handle_edited_message(update, MagicMock())

            assert list(inbox.glob("*.json")) == []
