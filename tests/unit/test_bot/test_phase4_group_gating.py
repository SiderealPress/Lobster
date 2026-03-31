"""
Unit tests for Phase 4: two-tier group gating in handle_edited_message and handle_reaction.

Verifies that:
- _check_message_access returns True for allowed DM users
- _check_message_access returns False for non-allowed DM users
- _check_message_access allows group users when gate_message returns ALLOW
- _check_message_access drops group users when gate_message returns DROP_SILENT
- _check_message_access drops group users when gate_message returns SEND_REGISTRATION_DM
- _check_message_access drops all group messages when gating is unavailable
- handle_edited_message applies two-tier gating (group + DM)
- handle_edited_message sets source="lobster-group" and group context fields for group edits
- handle_reaction applies two-tier gating (group + DM)
- _emit_reaction_signal includes group_chat_id and group_title for group reactions
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dm_update(user_id: int, text: str = "hello") -> MagicMock:
    """Build a mock Update for a private DM edited_message or text message."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"

    msg = MagicMock()
    msg.text = text
    msg.message_id = 1
    msg.chat_id = user_id
    msg.chat.type = "private"
    msg.chat.id = user_id
    msg.chat.title = None
    msg.reply_to_message = None

    update.effective_message = msg
    update.edited_message = msg
    update.message_reaction = None
    return update


def _make_group_update(
    user_id: int,
    chat_id: int = -100123,
    chat_title: str = "Test Group",
    text: str = "hello from group",
) -> MagicMock:
    """Build a mock Update for a group edited_message or text message."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "groupuser"
    update.effective_user.first_name = "Group"

    msg = MagicMock()
    msg.text = text
    msg.message_id = 42
    msg.chat_id = chat_id
    msg.chat.type = "supergroup"
    msg.chat.id = chat_id
    msg.chat.title = chat_title
    msg.reply_to_message = None

    update.effective_message = msg
    update.edited_message = msg
    update.message_reaction = None
    return update


def _make_reaction_update(
    user_id: int,
    chat_id: int,
    chat_type: str = "private",
    chat_title: str | None = None,
    msg_id: int = 10,
    new_emojis: list[str] | None = None,
    old_emojis: list[str] | None = None,
) -> MagicMock:
    """Build a mock Update for a MessageReactionUpdated event."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id

    def _make_reaction(emoji: str) -> MagicMock:
        r = MagicMock()
        r.emoji = emoji
        return r

    reaction_update = MagicMock()
    reaction_update.chat.id = chat_id
    reaction_update.chat.type = chat_type
    reaction_update.chat.title = chat_title
    reaction_update.message_id = msg_id
    reaction_update.new_reaction = [_make_reaction(e) for e in (new_emojis or [])]
    reaction_update.old_reaction = [_make_reaction(e) for e in (old_emojis or [])]

    update.message_reaction = reaction_update
    update.effective_message = None  # reactions have no effective_message
    return update


def _gate_result(action_name: str) -> MagicMock:
    """Build a mock gate_message result with the given action."""
    result = MagicMock()

    class _Action:
        pass

    result.action = action_name
    result.reason = action_name
    return result


# ---------------------------------------------------------------------------
# _check_message_access
# ---------------------------------------------------------------------------


class TestCheckMessageAccess:
    """Pure unit tests for the _check_message_access helper."""

    @pytest.mark.asyncio
    async def test_allowed_dm_user_is_accepted(self, bot_module):
        update = _make_dm_update(user_id=123456)
        result = await bot_module._check_message_access(update, MagicMock())
        assert result is True

    @pytest.mark.asyncio
    async def test_non_allowed_dm_user_is_rejected(self, bot_module):
        update = _make_dm_update(user_id=999999)
        result = await bot_module._check_message_access(update, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_group_user_allowed_when_gate_returns_allow(self, bot_module):
        update = _make_group_update(user_id=999999)
        allow_result = MagicMock()
        allow_result.action = bot_module.GatingAction.ALLOW

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "gate_message", return_value=allow_result),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            result = await bot_module._check_message_access(update, MagicMock())
        assert result is True

    @pytest.mark.asyncio
    async def test_group_user_dropped_when_gate_returns_drop_silent(self, bot_module):
        update = _make_group_update(user_id=999999)
        drop_result = MagicMock()
        drop_result.action = bot_module.GatingAction.DROP_SILENT
        drop_result.reason = "group not whitelisted"

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "gate_message", return_value=drop_result),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            result = await bot_module._check_message_access(update, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_group_user_dropped_when_gate_returns_registration_dm(self, bot_module):
        update = _make_group_update(user_id=999999)
        reg_result = MagicMock()
        reg_result.action = bot_module.GatingAction.SEND_REGISTRATION_DM
        reg_result.reason = "user not whitelisted"

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "gate_message", return_value=reg_result),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            result = await bot_module._check_message_access(update, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_group_message_dropped_when_gating_unavailable(self, bot_module):
        update = _make_group_update(user_id=123456)

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", False):
            result = await bot_module._check_message_access(update, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_user_missing(self, bot_module):
        update = MagicMock()
        update.effective_user = None
        update.effective_message = MagicMock()
        result = await bot_module._check_message_access(update, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_message_missing(self, bot_module):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_message = None
        result = await bot_module._check_message_access(update, MagicMock())
        assert result is False


# ---------------------------------------------------------------------------
# handle_edited_message — group gating
# ---------------------------------------------------------------------------


class TestHandleEditedMessageGroupGating:
    """Verify that handle_edited_message applies two-tier gating."""

    @pytest.mark.asyncio
    async def test_non_allowed_dm_user_edit_is_dropped(
        self, bot_module, temp_messages_dir
    ):
        """Edited messages from non-allowed DM users must be dropped."""
        inbox = temp_messages_dir / "inbox"
        update = _make_dm_update(user_id=999999, text="edited text")

        with patch.object(bot_module, "INBOX_DIR", inbox):
            await bot_module.handle_edited_message(update, MagicMock())

        assert list(inbox.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_allowed_dm_user_edit_is_accepted(
        self, bot_module, temp_messages_dir
    ):
        """Edited messages from allowed DM users must be written to inbox."""
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        processing.mkdir(parents=True, exist_ok=True)
        update = _make_dm_update(user_id=123456, text="edited text")

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "_MESSAGES", temp_messages_dir),
        ):
            await bot_module.handle_edited_message(update, MagicMock())

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["text"] == "edited text"
        assert data["source"] == "telegram"

    @pytest.mark.asyncio
    async def test_non_whitelisted_group_edit_is_dropped(
        self, bot_module, temp_messages_dir
    ):
        """Edited messages from non-whitelisted group users must be dropped."""
        inbox = temp_messages_dir / "inbox"
        update = _make_group_update(user_id=999999, text="edited group text")

        drop_result = MagicMock()
        drop_result.action = bot_module.GatingAction.DROP_SILENT
        drop_result.reason = "group not whitelisted"

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "gate_message", return_value=drop_result),
            patch.object(bot_module, "load_whitelist", return_value={}),
            patch.object(bot_module, "INBOX_DIR", inbox),
        ):
            await bot_module.handle_edited_message(update, MagicMock())

        assert list(inbox.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_whitelisted_group_edit_sets_correct_source_and_context(
        self, bot_module, temp_messages_dir
    ):
        """Edited messages from whitelisted group users must use lobster-group source
        and include group_chat_id and group_title fields."""
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        processing.mkdir(parents=True, exist_ok=True)

        update = _make_group_update(
            user_id=999999,
            chat_id=-100999,
            chat_title="My Group",
            text="edited group text",
        )

        allow_result = MagicMock()
        allow_result.action = bot_module.GatingAction.ALLOW

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "gate_message", return_value=allow_result),
            patch.object(bot_module, "load_whitelist", return_value={}),
            patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"),
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "_MESSAGES", temp_messages_dir),
        ):
            await bot_module.handle_edited_message(update, MagicMock())

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "lobster-group"
        assert data["group_chat_id"] == -100999
        assert data["group_title"] == "My Group"
        assert data["text"] == "edited group text"

    @pytest.mark.asyncio
    async def test_group_edit_dropped_when_gating_unavailable(
        self, bot_module, temp_messages_dir
    ):
        """When gating skill is unavailable, all group edits must be dropped."""
        inbox = temp_messages_dir / "inbox"
        update = _make_group_update(user_id=123456, text="edited group text")

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", False),
            patch.object(bot_module, "INBOX_DIR", inbox),
        ):
            await bot_module.handle_edited_message(update, MagicMock())

        assert list(inbox.glob("*.json")) == []


# ---------------------------------------------------------------------------
# handle_reaction — group gating
# ---------------------------------------------------------------------------


class TestHandleReactionGroupGating:
    """Verify that handle_reaction applies two-tier gating for group reactions."""

    @pytest.mark.asyncio
    async def test_dm_non_allowed_user_reaction_dropped(self, bot_module):
        """DM reactions from non-allowed users must be dropped."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=999999,
            chat_id=999999,
            chat_type="private",
            new_emojis=["\U0001f44d"],
        )

        with patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60):
            await bot_module.handle_reaction(update, MagicMock())

        assert (999999, 10) not in bot_module._pending_reactions

    @pytest.mark.asyncio
    async def test_dm_allowed_user_reaction_buffered(self, bot_module):
        """DM reactions from allowed users must be buffered."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=123456,
            chat_id=123456,
            chat_type="private",
            new_emojis=["\U0001f44d"],
        )

        with patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60):
            await bot_module.handle_reaction(update, MagicMock())

        key = (123456, 10)
        assert key in bot_module._pending_reactions
        bot_module._pending_reactions.pop(key).cancel()

    @pytest.mark.asyncio
    async def test_group_non_whitelisted_reaction_dropped(self, bot_module):
        """Group reactions from non-whitelisted users must be silently dropped."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=999999,
            chat_id=-100888,
            chat_type="supergroup",
            chat_title="Test Group",
            new_emojis=["\U0001f44d"],
        )

        drop_result = MagicMock()
        drop_result.action = bot_module.GatingAction.DROP_SILENT
        drop_result.reason = "group not whitelisted"

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "gate_message", return_value=drop_result),
            patch.object(bot_module, "load_whitelist", return_value={}),
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60),
        ):
            await bot_module.handle_reaction(update, MagicMock())

        assert (-100888, 10) not in bot_module._pending_reactions

    @pytest.mark.asyncio
    async def test_group_whitelisted_reaction_buffered(self, bot_module):
        """Group reactions from whitelisted users must be buffered."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=999999,
            chat_id=-100888,
            chat_type="supergroup",
            chat_title="Test Group",
            new_emojis=["\U0001f44d"],
        )

        allow_result = MagicMock()
        allow_result.action = bot_module.GatingAction.ALLOW

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "gate_message", return_value=allow_result),
            patch.object(bot_module, "load_whitelist", return_value={}),
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60),
        ):
            await bot_module.handle_reaction(update, MagicMock())

        key = (-100888, 10)
        assert key in bot_module._pending_reactions
        bot_module._pending_reactions.pop(key).cancel()

    @pytest.mark.asyncio
    async def test_group_reaction_dropped_when_gating_unavailable(self, bot_module):
        """When gating skill is unavailable, all group reactions must be dropped."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=123456,
            chat_id=-100888,
            chat_type="supergroup",
            new_emojis=["\U0001f44d"],
        )

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", False),
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60),
        ):
            await bot_module.handle_reaction(update, MagicMock())

        assert (-100888, 10) not in bot_module._pending_reactions


# ---------------------------------------------------------------------------
# _emit_reaction_signal — group context fields
# ---------------------------------------------------------------------------


class TestEmitReactionSignalGroupContext:
    """Verify that _emit_reaction_signal includes group fields for group reactions."""

    @pytest.mark.asyncio
    async def test_group_reaction_includes_group_context_fields(
        self, bot_module, temp_messages_dir
    ):
        inbox = temp_messages_dir / "inbox"
        bot_module._sent_message_buffer.clear()

        with (
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 0),
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"),
        ):
            await bot_module._emit_reaction_signal(
                -100888,
                99,
                "\U0001f44d",
                chat_type="supergroup",
                chat_title="My Group",
            )

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "lobster-group"
        assert data["group_chat_id"] == -100888
        assert data["group_title"] == "My Group"

    @pytest.mark.asyncio
    async def test_dm_reaction_excludes_group_context_fields(
        self, bot_module, temp_messages_dir
    ):
        inbox = temp_messages_dir / "inbox"
        bot_module._sent_message_buffer.clear()

        with (
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 0),
            patch.object(bot_module, "INBOX_DIR", inbox),
        ):
            await bot_module._emit_reaction_signal(
                123456,
                99,
                "\U0001f44d",
                chat_type="private",
                chat_title=None,
            )

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert "group_chat_id" not in data
        assert "group_title" not in data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    """Load lobster_bot with a patched environment and fresh module state."""
    import importlib

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123456")
    monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path / "messages"))

    (tmp_path / "messages" / "inbox").mkdir(parents=True, exist_ok=True)

    import src.bot.lobster_bot as module

    importlib.reload(module)

    module._pending_reactions.clear()
    module._sent_message_buffer.clear()

    yield module

    for task in list(module._pending_reactions.values()):
        task.cancel()
    module._pending_reactions.clear()


@pytest.fixture
def temp_messages_dir(tmp_path):
    """Create a temporary messages directory structure."""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return tmp_path
