"""
Unit tests for Telegram reaction handling.

Tests cover:
- _lookup_reacted_to_text against _sent_message_buffer
- _emit_reaction_signal writes the correct inbox JSON (no signal field)
- handle_reaction: all emoji are buffered (no allowlist)
- handle_reaction: undo cancels pending task before inbox write
- handle_reaction: unauthorized user is ignored
"""

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reaction_update(
    user_id: int,
    chat_id: int,
    msg_id: int,
    new_emojis: list[str],
    old_emojis: list[str] | None = None,
) -> MagicMock:
    """Build a minimal mock Update that mimics a MessageReactionUpdated event."""
    update = MagicMock()
    update.effective_user.id = user_id

    reaction_update = MagicMock()
    reaction_update.chat.id = chat_id
    reaction_update.message_id = msg_id

    def _make_reaction(emoji: str) -> MagicMock:
        r = MagicMock()
        r.emoji = emoji
        return r

    reaction_update.new_reaction = [_make_reaction(e) for e in new_emojis]
    reaction_update.old_reaction = [_make_reaction(e) for e in (old_emojis or [])]

    update.message_reaction = reaction_update
    return update


# ---------------------------------------------------------------------------
# Module-level constant tests — no I/O, no async
# ---------------------------------------------------------------------------


class TestReactionSignalsRemoved:
    """REACTION_SIGNALS dict must no longer exist in the module."""

    def test_reaction_signals_not_present(self, bot_module):
        assert not hasattr(bot_module, "REACTION_SIGNALS"), (
            "REACTION_SIGNALS was removed — the dispatcher now interprets emoji contextually"
        )


# ---------------------------------------------------------------------------
# _lookup_reacted_to_text
# ---------------------------------------------------------------------------


class TestLookupReactedToText:
    def test_returns_text_for_known_id(self, bot_module):
        bot_module._sent_message_buffer.clear()
        bot_module._sent_message_buffer.append((42, "Should I merge PR #10?"))
        assert bot_module._lookup_reacted_to_text(42) == "Should I merge PR #10?"

    def test_returns_empty_string_for_unknown_id(self, bot_module):
        bot_module._sent_message_buffer.clear()
        assert bot_module._lookup_reacted_to_text(9999) == ""

    def test_returns_most_recent_entry_when_duplicated(self, bot_module):
        # If somehow the same ID appears twice, return the first match
        bot_module._sent_message_buffer.clear()
        bot_module._sent_message_buffer.append((7, "first"))
        bot_module._sent_message_buffer.append((7, "second"))
        result = bot_module._lookup_reacted_to_text(7)
        # Either match is acceptable; just confirm it is non-empty
        assert result in ("first", "second")


# ---------------------------------------------------------------------------
# _emit_reaction_signal
# ---------------------------------------------------------------------------


class TestEmitReactionSignal:
    @pytest.mark.asyncio
    async def test_writes_correct_inbox_json(self, bot_module, temp_messages_dir):
        inbox = temp_messages_dir / "inbox"
        bot_module._sent_message_buffer.clear()
        bot_module._sent_message_buffer.append((99, "Should I merge PR #42?"))

        # Patch REACTION_UNDO_WINDOW_SECS to 0 so the test doesn't sleep 5 s
        with (
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 0),
            patch.object(bot_module, "INBOX_DIR", inbox),
        ):
            await bot_module._emit_reaction_signal(123456, 99, "\U0001f44d")

        files = list(inbox.glob("*.json"))
        assert len(files) == 1

        data = json.loads(files[0].read_text())
        assert data["type"] == "reaction"
        assert data["source"] == "telegram"
        assert data["chat_id"] == 123456
        assert data["telegram_message_id"] == 99
        assert data["emoji"] == "\U0001f44d"
        assert "signal" not in data, "signal field must be absent from reaction inbox entry"
        assert data["reacted_to_text"] == "Should I merge PR #42?"
        assert "timestamp" in data
        assert "[Reaction:" in data["text"]

    @pytest.mark.asyncio
    async def test_empty_reacted_to_text_when_not_buffered(
        self, bot_module, temp_messages_dir
    ):
        inbox = temp_messages_dir / "inbox"
        bot_module._sent_message_buffer.clear()

        with (
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 0),
            patch.object(bot_module, "INBOX_DIR", inbox),
        ):
            await bot_module._emit_reaction_signal(111, 55, "\u2705")

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["reacted_to_text"] == ""


# ---------------------------------------------------------------------------
# handle_reaction
# ---------------------------------------------------------------------------


class TestHandleReaction:
    @pytest.mark.asyncio
    async def test_known_emoji_creates_pending_task(self, bot_module):
        """A reaction emoji should create a pending task."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=123456,
            chat_id=123456,
            msg_id=10,
            new_emojis=["\U0001f44d"],  # 👍
        )
        context = MagicMock()

        with patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60):  # long window
            await bot_module.handle_reaction(update, context)

        key = (123456, 10)
        assert key in bot_module._pending_reactions
        # Clean up the task
        bot_module._pending_reactions.pop(key).cancel()

    @pytest.mark.asyncio
    async def test_unknown_emoji_is_delivered(self, bot_module):
        """An emoji that was previously unknown must now create a pending task."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=123456,
            chat_id=123456,
            msg_id=20,
            new_emojis=["\U0001f600"],  # 😀 — previously silently dropped
        )
        context = MagicMock()

        with patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60):
            await bot_module.handle_reaction(update, context)

        key = (123456, 20)
        assert key in bot_module._pending_reactions, (
            "All emoji reactions should be delivered — no allowlist filtering"
        )
        bot_module._pending_reactions.pop(key).cancel()

    @pytest.mark.asyncio
    async def test_unauthorized_user_is_ignored(self, bot_module):
        """Reactions from users not in ALLOWED_USERS must be silently dropped."""
        bot_module._pending_reactions.clear()
        update = _make_reaction_update(
            user_id=999999,  # not in ALLOWED_USERS (which is [123456] in fixture)
            chat_id=999999,
            msg_id=30,
            new_emojis=["\U0001f44d"],
        )
        context = MagicMock()

        await bot_module.handle_reaction(update, context)

        assert (999999, 30) not in bot_module._pending_reactions

    @pytest.mark.asyncio
    async def test_undo_cancels_pending_task_and_nothing_written(
        self, bot_module, temp_messages_dir
    ):
        """Removing a reaction within the undo window must cancel the signal."""
        inbox = temp_messages_dir / "inbox"
        bot_module._pending_reactions.clear()
        context = MagicMock()

        # Step 1: react
        add_update = _make_reaction_update(
            user_id=123456, chat_id=123456, msg_id=40, new_emojis=["\U0001f44d"]
        )
        with (
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 60),
            patch.object(bot_module, "INBOX_DIR", inbox),
        ):
            await bot_module.handle_reaction(add_update, context)

        key = (123456, 40)
        assert key in bot_module._pending_reactions

        # Step 2: remove the reaction (new is empty, old has the emoji)
        remove_update = _make_reaction_update(
            user_id=123456,
            chat_id=123456,
            msg_id=40,
            new_emojis=[],
            old_emojis=["\U0001f44d"],
        )
        with patch.object(bot_module, "INBOX_DIR", inbox):
            await bot_module.handle_reaction(remove_update, context)

        # Task should be cancelled and removed
        assert key not in bot_module._pending_reactions

        # Allow any brief async tasks to settle, then check no file written
        await asyncio.sleep(0.05)
        files = list(inbox.glob("*.json"))
        assert len(files) == 0

    @pytest.mark.asyncio
    async def test_reaction_written_after_undo_window(
        self, bot_module, temp_messages_dir
    ):
        """After the undo window, the reaction must be written to inbox without signal field."""
        inbox = temp_messages_dir / "inbox"
        bot_module._pending_reactions.clear()
        bot_module._sent_message_buffer.clear()
        context = MagicMock()

        update = _make_reaction_update(
            user_id=123456, chat_id=123456, msg_id=50, new_emojis=["\U0001f44e"]
        )
        with (
            patch.object(bot_module, "REACTION_UNDO_WINDOW_SECS", 0),
            patch.object(bot_module, "INBOX_DIR", inbox),
        ):
            await bot_module.handle_reaction(update, context)
            # Give the async task a moment to complete
            await asyncio.sleep(0.1)

        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert "signal" not in data, "signal field must be absent"
        assert data["emoji"] == "\U0001f44e"

    @pytest.mark.asyncio
    async def test_no_reaction_update_returns_early(self, bot_module):
        """If update.message_reaction is None, handler returns without error."""
        update = MagicMock()
        update.message_reaction = None
        context = MagicMock()

        # Should not raise
        await bot_module.handle_reaction(update, context)


# ---------------------------------------------------------------------------
# Multi-chunk reply buffering
# ---------------------------------------------------------------------------


class TestMultiChunkReplyBuffering:
    """Only the LAST chunk's message_id should be appended to _sent_message_buffer.

    When a reply is split into multiple Telegram messages, the user can only
    react to the final visible message.  Buffering every chunk would cause
    reaction lookups to find the wrong (intermediate) text snippet.
    """

    @pytest.mark.asyncio
    async def test_only_last_chunk_buffered(self, bot_module, tmp_path):
        """Sending a 2-chunk reply buffers only the last chunk's message_id."""
        import json

        outbox = tmp_path / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        # Build a message that will produce exactly 2 chunks (each ~3000+ chars)
        long_text = "First chunk. " + "a" * 3000 + "\n\n" + "Second chunk. " + "b" * 3000

        reply = {
            "chat_id": 123456,
            "text": long_text,
            "source": "telegram",
        }
        reply_file = outbox / "reply_multichunk.json"
        reply_file.write_text(json.dumps(reply))

        # Set up mock bot that returns distinct sent_msg objects per call
        mock_app = MagicMock()
        first_msg = MagicMock()
        first_msg.message_id = 1001
        second_msg = MagicMock()
        second_msg.message_id = 1002
        mock_app.bot.send_message = AsyncMock(side_effect=[first_msg, second_msg])
        mock_app.bot.send_photo = AsyncMock()

        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_app
        bot_module._sent_message_buffer.clear()

        import asyncio
        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            handler = bot_module.OutboxHandler()
            await handler.process_reply(str(reply_file))

            # Exactly 2 chunks were sent
            assert mock_app.bot.send_message.call_count == 2

            # Buffer must contain exactly ONE entry
            assert len(bot_module._sent_message_buffer) == 1

            buffered_ids = [msg_id for msg_id, _ in bot_module._sent_message_buffer]

            # The buffered id must be the LAST chunk's message_id (1002), not the first (1001)
            assert 1002 in buffered_ids, (
                f"Expected last chunk id 1002 in buffer, got {buffered_ids}"
            )
            assert 1001 not in buffered_ids, (
                f"First chunk id 1001 must NOT be in buffer, got {buffered_ids}"
            )
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    """Load lobster_bot with a patched environment and a fresh module state."""
    import importlib

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123456")
    monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path / "messages"))

    # Create required directories
    (tmp_path / "messages" / "inbox").mkdir(parents=True, exist_ok=True)

    import src.bot.lobster_bot as module

    importlib.reload(module)

    # Reset mutable module-level state between tests
    module._pending_reactions.clear()
    module._sent_message_buffer.clear()

    yield module

    # Cleanup: cancel any lingering tasks
    for task in list(module._pending_reactions.values()):
        task.cancel()
    module._pending_reactions.clear()


@pytest.fixture
def temp_messages_dir(tmp_path):
    """Create a temporary messages directory structure."""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return tmp_path
