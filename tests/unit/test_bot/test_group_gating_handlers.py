"""
Tests for Phase 4: two-tier group gating in all non-text message handlers.

Covers:
- _check_group_gating: allowed DM user passes, unauthorized DM user dropped,
  whitelisted group user passes, non-whitelisted group user dropped,
  group gating disabled drops all group messages.
- handle_edited_message: non-whitelisted group user dropped; allowed user
  processed with correct source and group fields.
- handle_reaction: non-whitelisted group user dropped; allowed user buffered.
- handle_photo_message: writes correct source and group fields for group message.
- handle_document_message: writes correct source and group fields.
- handle_audio_message: writes correct source and group fields.
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_chat(chat_type: str, chat_id: int = -100) -> MagicMock:
    chat = MagicMock()
    chat.type = chat_type
    chat.id = chat_id
    chat.title = "Test Group"
    return chat


def _make_user(user_id: int = 123456) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.username = "testuser"
    user.first_name = "Test"
    return user


def _make_message(user_id: int = 123456, chat_type: str = "private", chat_id: int = 123456) -> MagicMock:
    msg = MagicMock()
    msg.message_id = 1
    msg.chat_id = chat_id
    msg.chat = _make_chat(chat_type, chat_id)
    msg.caption = None
    msg.reply_to_message = None
    msg.media_group_id = None
    msg.reply_text = AsyncMock()
    return msg


# ---------------------------------------------------------------------------
# _check_group_gating unit tests
# ---------------------------------------------------------------------------


class TestCheckGroupGating:
    """Pure unit tests for the two-tier gating helper."""

    @pytest.mark.asyncio
    async def test_dm_allowed_user_passes(self, bot_module):
        user = _make_user(123456)
        chat = _make_chat("private", 123456)
        context = MagicMock()

        result = await bot_module._check_group_gating(user, chat, context)
        assert result is True

    @pytest.mark.asyncio
    async def test_dm_unauthorized_user_dropped(self, bot_module):
        user = _make_user(999999)
        chat = _make_chat("private", 999999)
        context = MagicMock()

        result = await bot_module._check_group_gating(user, chat, context)
        assert result is False

    @pytest.mark.asyncio
    async def test_group_gating_enabled_allowed_user_passes(self, bot_module):
        """Whitelisted group + whitelisted user → ALLOW."""
        user = _make_user(123456)
        chat = _make_chat("group", -100)
        context = MagicMock()

        from multiplayer_telegram_bot.gating import GatingResult, GatingAction

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(
                bot_module,
                "gate_message",
                return_value=GatingResult(action=GatingAction.ALLOW, chat_id=-100, user_id=123456, reason="allowed"),
            ),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            result = await bot_module._check_group_gating(user, chat, context)

        assert result is True

    @pytest.mark.asyncio
    async def test_group_gating_enabled_drop_silent(self, bot_module):
        """Unwhitelisted group → DROP_SILENT."""
        user = _make_user(123456)
        chat = _make_chat("group", -100)
        context = MagicMock()

        from multiplayer_telegram_bot.gating import GatingResult, GatingAction

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(
                bot_module,
                "gate_message",
                return_value=GatingResult(action=GatingAction.DROP_SILENT, chat_id=-100, user_id=123456, reason="group not enabled"),
            ),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            result = await bot_module._check_group_gating(user, chat, context)

        assert result is False

    @pytest.mark.asyncio
    async def test_group_gating_enabled_send_registration_dm_drops(self, bot_module):
        """Non-whitelisted user in whitelisted group → drop silently (no DM)."""
        user = _make_user(99999)
        chat = _make_chat("group", -100)
        context = MagicMock()

        from multiplayer_telegram_bot.gating import GatingResult, GatingAction

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(
                bot_module,
                "gate_message",
                return_value=GatingResult(
                    action=GatingAction.SEND_REGISTRATION_DM, chat_id=-100, user_id=99999, reason="user not allowed"
                ),
            ),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            result = await bot_module._check_group_gating(user, chat, context)

        assert result is False

    @pytest.mark.asyncio
    async def test_group_gating_disabled_drops_all_group_messages(self, bot_module):
        """When the skill is unavailable, all group messages are dropped."""
        user = _make_user(123456)
        chat = _make_chat("supergroup", -100)
        context = MagicMock()

        with patch.object(bot_module, "_GROUP_GATING_ENABLED", False):
            result = await bot_module._check_group_gating(user, chat, context)

        assert result is False


# ---------------------------------------------------------------------------
# handle_edited_message
# ---------------------------------------------------------------------------


class TestHandleEditedMessageGating:
    @pytest.mark.asyncio
    async def test_unauthorized_group_user_dropped(self, bot_module, temp_messages_dir):
        """Edited messages from non-whitelisted group users must not reach inbox."""
        inbox = temp_messages_dir / "inbox"

        update = MagicMock()
        update.edited_message = MagicMock()
        update.edited_message.text = "edited text"
        update.edited_message.message_id = 42
        update.edited_message.chat = _make_chat("group", -100)
        update.edited_message.chat_id = -100
        update.effective_user = _make_user(999999)
        context = MagicMock()

        from multiplayer_telegram_bot.gating import GatingResult, GatingAction

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(
                bot_module,
                "gate_message",
                return_value=GatingResult(action=GatingAction.DROP_SILENT, chat_id=-100, user_id=123456, reason="group not enabled"),
            ),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            await bot_module.handle_edited_message(update, context)

        assert list(inbox.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_allowed_dm_user_edit_written_with_correct_source(
        self, bot_module, temp_messages_dir
    ):
        """Edits from allowed DM users must be written with source='telegram'."""
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        processing.mkdir(parents=True, exist_ok=True)

        update = MagicMock()
        update.edited_message = MagicMock()
        update.edited_message.text = "edited text"
        update.edited_message.message_id = 42
        update.edited_message.chat = _make_chat("private", 123456)
        update.edited_message.chat_id = 123456
        update.effective_user = _make_user(123456)
        context = MagicMock()

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
        ):
            await bot_module.handle_edited_message(update, context)

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "telegram"
        assert "_edit_of_telegram_id" in data
        assert "group_chat_id" not in data

    @pytest.mark.asyncio
    async def test_allowed_group_user_edit_uses_group_source_and_fields(
        self, bot_module, temp_messages_dir
    ):
        """Edits from whitelisted group users must use source='lobster-group'."""
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        processing.mkdir(parents=True, exist_ok=True)

        update = MagicMock()
        update.edited_message = MagicMock()
        update.edited_message.text = "edited group text"
        update.edited_message.message_id = 55
        update.edited_message.chat = _make_chat("group", -100)
        update.edited_message.chat_id = -100
        update.effective_user = _make_user(123456)
        context = MagicMock()

        from multiplayer_telegram_bot.gating import GatingResult, GatingAction

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(
                bot_module,
                "gate_message",
                return_value=GatingResult(action=GatingAction.ALLOW, chat_id=-100, user_id=123456, reason="allowed"),
            ),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            await bot_module.handle_edited_message(update, context)

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "lobster-group"
        assert data["group_chat_id"] == -100
        assert data["group_title"] == "Test Group"


# ---------------------------------------------------------------------------
# handle_reaction
# ---------------------------------------------------------------------------


class TestHandleReactionGating:
    @pytest.mark.asyncio
    async def test_unauthorized_group_user_reaction_dropped(self, bot_module):
        """Reactions from non-whitelisted group users must not create a pending task."""
        bot_module._pending_reactions.clear()

        reaction_update = MagicMock()
        reaction_update.chat = _make_chat("group", -100)
        reaction_update.message_id = 77
        reaction_update.new_reaction = [MagicMock(emoji="👍")]
        reaction_update.old_reaction = []

        update = MagicMock()
        update.message_reaction = reaction_update
        update.effective_user = _make_user(999999)
        context = MagicMock()

        from multiplayer_telegram_bot.gating import GatingResult, GatingAction

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(
                bot_module,
                "gate_message",
                return_value=GatingResult(action=GatingAction.DROP_SILENT, chat_id=-100, user_id=999999, reason="not allowed"),
            ),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            await bot_module.handle_reaction(update, context)

        assert (-100, 77) not in bot_module._pending_reactions

    @pytest.mark.asyncio
    async def test_whitelisted_group_user_reaction_buffered(self, bot_module):
        """Reactions from whitelisted group users must create a pending task."""
        bot_module._pending_reactions.clear()

        reaction_update = MagicMock()
        reaction_update.chat = _make_chat("group", -100)
        reaction_update.message_id = 88
        reaction_update.new_reaction = [MagicMock(emoji="👍")]
        reaction_update.old_reaction = []

        update = MagicMock()
        update.message_reaction = reaction_update
        update.effective_user = _make_user(123456)
        context = MagicMock()

        from multiplayer_telegram_bot.gating import GatingResult, GatingAction

        with (
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60),
            patch.object(
                bot_module,
                "gate_message",
                return_value=GatingResult(action=GatingAction.ALLOW, chat_id=-100, user_id=123456, reason="allowed"),
            ),
            patch.object(bot_module, "load_whitelist", return_value={}),
        ):
            await bot_module.handle_reaction(update, context)

        key = (-100, 88)
        assert key in bot_module._pending_reactions
        bot_module._pending_reactions.pop(key).cancel()


# ---------------------------------------------------------------------------
# handle_photo_message source and group fields
# ---------------------------------------------------------------------------


class TestHandlePhotoMessageGroupSource:
    @pytest.mark.asyncio
    async def test_group_photo_uses_lobster_group_source(
        self, bot_module, temp_messages_dir
    ):
        """Photos from group chats must be written with source='lobster-group'."""
        inbox = temp_messages_dir / "inbox"
        images = temp_messages_dir / "images"
        images.mkdir(parents=True, exist_ok=True)

        photo = MagicMock()
        photo.file_id = "file123"

        message = _make_message(chat_type="group", chat_id=-100)
        message.photo = [photo]
        message.media_group_id = None

        update = MagicMock()
        update.message = message
        update.effective_user = _make_user(123456)
        context = MagicMock()

        # Simulate file download
        mock_file = AsyncMock()
        mock_file.download_to_drive = AsyncMock()
        context.bot.get_file = AsyncMock(return_value=mock_file)

        msg_id = "test_photo_msg"

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "IMAGES_DIR", images),
            patch.object(bot_module, "send_typing_indicator", AsyncMock()),
            patch.object(bot_module, "extract_reply_to_context", return_value=None),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
        ):
            await bot_module.handle_photo_message(update, context, msg_id)

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "lobster-group"
        assert data["group_chat_id"] == -100
        assert data["group_title"] == "Test Group"

    @pytest.mark.asyncio
    async def test_dm_photo_uses_telegram_source(
        self, bot_module, temp_messages_dir
    ):
        """Photos from DMs must be written with source='telegram'."""
        inbox = temp_messages_dir / "inbox"
        images = temp_messages_dir / "images"
        images.mkdir(parents=True, exist_ok=True)

        photo = MagicMock()
        photo.file_id = "file456"

        message = _make_message(chat_type="private", chat_id=123456)
        message.photo = [photo]
        message.media_group_id = None

        update = MagicMock()
        update.message = message
        update.effective_user = _make_user(123456)
        context = MagicMock()

        mock_file = AsyncMock()
        mock_file.download_to_drive = AsyncMock()
        context.bot.get_file = AsyncMock(return_value=mock_file)

        msg_id = "test_dm_photo_msg"

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "IMAGES_DIR", images),
            patch.object(bot_module, "send_typing_indicator", AsyncMock()),
            patch.object(bot_module, "extract_reply_to_context", return_value=None),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
        ):
            await bot_module.handle_photo_message(update, context, msg_id)

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "telegram"
        assert "group_chat_id" not in data


# ---------------------------------------------------------------------------
# handle_document_message source and group fields
# ---------------------------------------------------------------------------


class TestHandleDocumentMessageGroupSource:
    @pytest.mark.asyncio
    async def test_group_document_uses_lobster_group_source(
        self, bot_module, temp_messages_dir
    ):
        inbox = temp_messages_dir / "inbox"

        document = MagicMock()
        document.file_name = "test.pdf"
        document.mime_type = "application/pdf"
        document.file_size = 1024
        document.file_id = "doc123"

        message = _make_message(chat_type="group", chat_id=-100)
        message.document = document

        update = MagicMock()
        update.message = message
        update.effective_user = _make_user(123456)
        context = MagicMock()

        msg_id = "test_doc_msg"

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "send_typing_indicator", AsyncMock()),
            patch.object(bot_module, "extract_reply_to_context", return_value=None),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
        ):
            await bot_module.handle_document_message(update, context, msg_id)

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "lobster-group"
        assert data["group_chat_id"] == -100
        assert data["group_title"] == "Test Group"

    @pytest.mark.asyncio
    async def test_dm_document_uses_telegram_source(
        self, bot_module, temp_messages_dir
    ):
        inbox = temp_messages_dir / "inbox"

        document = MagicMock()
        document.file_name = "file.txt"
        document.mime_type = "text/plain"
        document.file_size = 100
        document.file_id = "doc456"

        message = _make_message(chat_type="private", chat_id=123456)
        message.document = document

        update = MagicMock()
        update.message = message
        update.effective_user = _make_user(123456)
        context = MagicMock()

        msg_id = "test_dm_doc_msg"

        with (
            patch.object(bot_module, "INBOX_DIR", inbox),
            patch.object(bot_module, "send_typing_indicator", AsyncMock()),
            patch.object(bot_module, "extract_reply_to_context", return_value=None),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
        ):
            await bot_module.handle_document_message(update, context, msg_id)

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "telegram"
        assert "group_chat_id" not in data


# ---------------------------------------------------------------------------
# handle_audio_message source and group fields
# ---------------------------------------------------------------------------


class TestHandleAudioMessageGroupSource:
    @pytest.mark.asyncio
    async def test_group_audio_uses_lobster_group_source(
        self, bot_module, temp_messages_dir
    ):
        audio_dir = temp_messages_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        pending_dir = temp_messages_dir / "pending-transcription"
        pending_dir.mkdir(parents=True, exist_ok=True)

        audio_obj = MagicMock()
        audio_obj.file_id = "audio123"
        audio_obj.duration = 10
        audio_obj.file_size = 5000
        audio_obj.mime_type = "audio/ogg"
        audio_obj.file_name = None

        message = _make_message(chat_type="group", chat_id=-100)
        message.voice = audio_obj
        message.audio = None

        update = MagicMock()
        update.message = message
        update.effective_user = _make_user(123456)
        context = MagicMock()

        mock_file = AsyncMock()
        mock_file.download_to_drive = AsyncMock()
        context.bot.get_file = AsyncMock(return_value=mock_file)

        msg_id = "test_audio_msg"

        with (
            patch.object(bot_module, "AUDIO_DIR", audio_dir),
            patch.object(bot_module, "PENDING_TRANSCRIPTION_DIR", pending_dir),
            patch.object(bot_module, "send_typing_indicator", AsyncMock()),
            patch.object(bot_module, "extract_reply_to_context", return_value=None),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
        ):
            await bot_module.handle_audio_message(update, context, msg_id, audio_obj)

        files = list(pending_dir.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "lobster-group"
        assert data["group_chat_id"] == -100
        assert data["group_title"] == "Test Group"

    @pytest.mark.asyncio
    async def test_dm_audio_uses_telegram_source(
        self, bot_module, temp_messages_dir
    ):
        audio_dir = temp_messages_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        pending_dir = temp_messages_dir / "pending-transcription"
        pending_dir.mkdir(parents=True, exist_ok=True)

        audio_obj = MagicMock()
        audio_obj.file_id = "audio456"
        audio_obj.duration = 5
        audio_obj.file_size = 2000
        audio_obj.mime_type = "audio/ogg"
        audio_obj.file_name = None

        message = _make_message(chat_type="private", chat_id=123456)
        message.voice = audio_obj
        message.audio = None

        update = MagicMock()
        update.message = message
        update.effective_user = _make_user(123456)
        context = MagicMock()

        mock_file = AsyncMock()
        mock_file.download_to_drive = AsyncMock()
        context.bot.get_file = AsyncMock(return_value=mock_file)

        msg_id = "test_dm_audio_msg"

        with (
            patch.object(bot_module, "AUDIO_DIR", audio_dir),
            patch.object(bot_module, "PENDING_TRANSCRIPTION_DIR", pending_dir),
            patch.object(bot_module, "send_typing_indicator", AsyncMock()),
            patch.object(bot_module, "extract_reply_to_context", return_value=None),
            patch.object(bot_module, "_GROUP_GATING_ENABLED", True),
        ):
            await bot_module.handle_audio_message(update, context, msg_id, audio_obj)

        files = list(pending_dir.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "telegram"
        assert "group_chat_id" not in data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    """Load lobster_bot with test env and reset mutable state."""
    import importlib

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123456")
    monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path / "messages"))

    (tmp_path / "messages" / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "messages" / "pending-transcription").mkdir(parents=True, exist_ok=True)
    (tmp_path / "messages" / "audio").mkdir(parents=True, exist_ok=True)

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
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return tmp_path
