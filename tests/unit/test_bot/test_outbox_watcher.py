"""
Tests for Telegram Bot Outbox Watcher

Tests the OutboxHandler that sends replies via Telegram, and the
split_message utility that enforces Telegram's 4096-character limit.
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio


def get_bot_module():
    """Import bot module with required environment variables set."""
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
        return bot_module


class TestOutboxHandler:
    """Tests for OutboxHandler class."""

    @pytest.fixture
    def mock_bot_app(self):
        """Create mock bot application."""
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        return app

    @pytest.fixture
    def bot_module(self):
        """Get bot module with environment set up."""
        return get_bot_module()

    @pytest.mark.asyncio
    async def test_processes_reply_file(self, temp_messages_dir, mock_bot_app, bot_module):
        """Test that reply file is processed and sent."""
        outbox = temp_messages_dir / "outbox"

        # Create reply file
        reply = {
            "chat_id": 123456,
            "text": "Hello from Lobster!",
            "source": "telegram",
        }
        reply_file = outbox / "reply_1.json"
        reply_file.write_text(json.dumps(reply))

        handler = bot_module.OutboxHandler()

        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app

        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))

            mock_bot_app.bot.send_message.assert_called_once()
            call_kwargs = mock_bot_app.bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 123456
            # HTML conversion is applied before sending; plain text should be present
            assert "Hello from Lobster!" in call_kwargs["text"]

            assert not reply_file.exists()
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()

    @pytest.mark.asyncio
    async def test_handles_missing_chat_id(self, temp_messages_dir, mock_bot_app, bot_module):
        """Test that missing chat_id is handled gracefully."""
        outbox = temp_messages_dir / "outbox"

        reply = {"text": "Hello!"}
        reply_file = outbox / "reply_1.json"
        reply_file.write_text(json.dumps(reply))

        handler = bot_module.OutboxHandler()

        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app

        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))
            mock_bot_app.bot.send_message.assert_not_called()
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()

    @pytest.mark.asyncio
    async def test_handles_missing_text(self, temp_messages_dir, mock_bot_app, bot_module):
        """Test that missing text is handled gracefully."""
        outbox = temp_messages_dir / "outbox"

        reply = {"chat_id": 123456}
        reply_file = outbox / "reply_1.json"
        reply_file.write_text(json.dumps(reply))

        handler = bot_module.OutboxHandler()

        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app

        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))
            mock_bot_app.bot.send_message.assert_not_called()
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()

    @pytest.mark.asyncio
    async def test_handles_invalid_json(self, temp_messages_dir, mock_bot_app, bot_module):
        """Test that invalid JSON is handled gracefully."""
        outbox = temp_messages_dir / "outbox"

        reply_file = outbox / "reply_1.json"
        reply_file.write_text("not valid json {{{")

        handler = bot_module.OutboxHandler()

        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app

        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))
            mock_bot_app.bot.send_message.assert_not_called()
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()

    def test_on_created_triggers_for_json_files(self, temp_messages_dir, bot_module):
        """Test that on_created triggers for .json files."""
        from watchdog.events import FileCreatedEvent

        handler = bot_module.OutboxHandler()

        event = FileCreatedEvent(str(temp_messages_dir / "outbox" / "test.json"))

        original_bot_app = bot_module.bot_app
        original_loop = bot_module.main_loop

        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        bot_module.bot_app = MagicMock()
        bot_module.main_loop = mock_loop

        try:
            with patch("asyncio.run_coroutine_threadsafe") as mock_run:
                handler.on_created(event)
                mock_run.assert_called_once()
        finally:
            bot_module.bot_app = original_bot_app
            bot_module.main_loop = original_loop

    def test_on_created_ignores_non_json_files(self, temp_messages_dir, bot_module):
        """Test that on_created ignores non-.json files."""
        from watchdog.events import FileCreatedEvent

        handler = bot_module.OutboxHandler()

        event = FileCreatedEvent(str(temp_messages_dir / "outbox" / "test.txt"))

        with patch("asyncio.run_coroutine_threadsafe") as mock_run:
            handler.on_created(event)
            mock_run.assert_not_called()

    def test_on_created_ignores_directories(self, temp_messages_dir, bot_module):
        """Test that on_created ignores directories."""
        from watchdog.events import DirCreatedEvent

        handler = bot_module.OutboxHandler()

        event = DirCreatedEvent(str(temp_messages_dir / "outbox" / "subdir"))

        with patch("asyncio.run_coroutine_threadsafe") as mock_run:
            handler.on_created(event)
            mock_run.assert_not_called()


class TestSplitMessage:
    """Tests for the split_message function.

    Covers the enhanced v2 implementation:
    - Code-block-aware splitting (never break inside triple-backtick fences)
    - Continuation labels on follow-on chunks
    - Sentence-boundary fallback
    - Standard paragraph/newline/hard split paths
    """

    @pytest.fixture
    def bot_module(self):
        return get_bot_module()

    # ------------------------------------------------------------------
    # Basic boundary conditions
    # ------------------------------------------------------------------

    def test_short_message_no_split(self, bot_module):
        """Messages under the limit are returned as a single-element list."""
        result = bot_module.split_message("Hello world")
        assert result == ["Hello world"]

    def test_empty_string_no_split(self, bot_module):
        """Empty string returns a single empty chunk."""
        result = bot_module.split_message("")
        assert result == [""]

    def test_exactly_at_limit_no_split(self, bot_module):
        """Message exactly at the limit is not split."""
        text = "a" * 4000
        result = bot_module.split_message(text)
        assert result == [text]

    def test_one_char_over_limit_splits(self, bot_module):
        """Message one character over the limit is split."""
        text = "a" * 4001
        result = bot_module.split_message(text)
        assert len(result) == 2

    # ------------------------------------------------------------------
    # Split boundary selection
    # ------------------------------------------------------------------

    def test_split_at_paragraph_boundary(self, bot_module):
        """Prefers paragraph boundaries (double newline) over other breaks."""
        para1 = "a" * 3000
        para2 = "b" * 3000
        text = para1 + "\n\n" + para2
        result = bot_module.split_message(text)
        assert len(result) == 2
        assert para1 in result[0]
        assert para2 in result[1]

    def test_split_at_single_newline(self, bot_module):
        """Falls back to single newline when no paragraph boundary fits."""
        # 21 lines of 200 chars, single-newline separated
        line = "x" * 200
        text = "\n".join([line] * 21)
        result = bot_module.split_message(text)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 4000

    def test_all_chunks_within_limit(self, bot_module):
        """Every chunk must be within the configured max_length."""
        text = "word " * 2000  # ~10000 chars
        result = bot_module.split_message(text)
        for chunk in result:
            assert len(chunk) <= 4000

    def test_hard_split_no_whitespace(self, bot_module):
        """Falls back to hard split when text has no whitespace or newlines."""
        text = "a" * 8500
        result = bot_module.split_message(text)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 4000

    def test_custom_max_length(self, bot_module):
        """Supports custom max_length parameter."""
        text = "a" * 100
        result = bot_module.split_message(text, max_length=30)
        for chunk in result:
            assert len(chunk) <= 30

    def test_multi_paragraph_all_chunks_bounded(self, bot_module):
        """Multiple paragraphs all produce chunks within the limit."""
        paras = ["paragraph " + str(i) + " " + "x" * 1500 for i in range(5)]
        text = "\n\n".join(paras)
        result = bot_module.split_message(text)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 4000

    def test_content_preserved_across_chunks(self, bot_module):
        """All content from the original message appears in the chunks."""
        para1 = "First paragraph " + "a" * 1500
        para2 = "Second paragraph " + "b" * 1500
        para3 = "Third paragraph " + "c" * 1500
        text = para1 + "\n\n" + para2 + "\n\n" + para3
        result = bot_module.split_message(text)
        combined = " ".join(result)
        # All three distinctive markers must appear somewhere in the output
        assert "First paragraph" in combined
        assert "Second paragraph" in combined
        assert "Third paragraph" in combined

    # ------------------------------------------------------------------
    # Continuation labels
    # ------------------------------------------------------------------

    def test_first_chunk_has_no_continuation_label(self, bot_module):
        """The first chunk never starts with the continuation label."""
        text = "a" * 5000
        result = bot_module.split_message(text)
        assert len(result) >= 2
        assert not result[0].startswith("_(continued)_")

    def test_subsequent_chunks_have_continuation_label(self, bot_module):
        """All chunks after the first are prefixed with the continuation label."""
        text = "a" * 9000
        result = bot_module.split_message(text)
        assert len(result) >= 3
        for chunk in result[1:]:
            assert chunk.startswith("_(continued)_")

    def test_single_chunk_no_continuation_label(self, bot_module):
        """A message that fits in one chunk has no continuation label."""
        result = bot_module.split_message("Short message")
        assert len(result) == 1
        assert not result[0].startswith("_(continued)_")

    # ------------------------------------------------------------------
    # Code block awareness
    # ------------------------------------------------------------------

    def test_does_not_split_inside_code_block(self, bot_module):
        """Split points that fall inside a triple-backtick block are avoided."""
        # Pad preamble to push a naive splitter into the code block
        padded_preamble = "P" * 2000 + "\n\n"
        code_content = "x = 1\n" * 400   # ~2800 chars of code
        code_block = f"```python\n{code_content}```"
        postamble = "\n\nAnd here is more text after the code."
        text = padded_preamble + code_block + postamble

        result = bot_module.split_message(text)

        # Every chunk must have balanced ``` fences
        for chunk in result:
            fence_count = chunk.count("```")
            assert fence_count % 2 == 0, (
                f"Chunk has unmatched code fences: {chunk[:200]!r}"
            )

    def test_small_code_block_not_split(self, bot_module):
        """A small code block that fits in one chunk is never split."""
        text = "Intro text.\n\n" + "```python\nprint('hello')\n```" + "\n\nOutro text."
        result = bot_module.split_message(text)
        # Message is well under 4000 chars — should be a single chunk
        assert len(result) == 1
        assert "```python" in result[0]

    def test_is_inside_code_block_helper_false_outside(self, bot_module):
        """_is_inside_code_block returns False for positions outside code blocks."""
        text = "before ```code``` after"
        assert not bot_module._is_inside_code_block(text, 2)    # before the block
        assert not bot_module._is_inside_code_block(text, 20)   # after the block

    def test_is_inside_code_block_helper_true_inside(self, bot_module):
        """_is_inside_code_block returns True for a position inside a block."""
        text = "before ```code``` after"
        inside_pos = text.index("code") + 2
        assert bot_module._is_inside_code_block(text, inside_pos)

    def test_is_inside_code_block_at_open_fence(self, bot_module):
        """Position at the opening ``` is considered just before entering the block."""
        text = "```code```"
        # Position 0 is before the first backtick — not inside
        assert not bot_module._is_inside_code_block(text, 0)

    def test_two_consecutive_code_blocks(self, bot_module):
        """Two code blocks in a message each satisfy the fence-balance invariant."""
        block = "```\n" + "x" * 100 + "\n```"
        text = "Header\n\n" + block + "\n\nMiddle\n\n" + block + "\n\nFooter"
        result = bot_module.split_message(text)
        for chunk in result:
            assert chunk.count("```") % 2 == 0


class TestLongMessageSending:
    """Tests that process_reply splits long messages and sends multiple Telegram messages."""

    @pytest.fixture
    def mock_bot_app(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        return app

    @pytest.fixture
    def bot_module(self):
        return get_bot_module()

    @pytest.mark.asyncio
    async def test_long_message_sends_multiple_chunks(
        self, temp_messages_dir, mock_bot_app, bot_module
    ):
        """A message over TELEGRAM_MAX_LENGTH triggers multiple send_message calls."""
        outbox = temp_messages_dir / "outbox"

        long_text = ("First paragraph. " + "a" * 3000 + "\n\n"
                     + "Second paragraph. " + "b" * 3000)
        reply = {
            "chat_id": 123456,
            "text": long_text,
            "source": "telegram",
        }
        reply_file = outbox / "reply_long.json"
        reply_file.write_text(json.dumps(reply))

        handler = bot_module.OutboxHandler()
        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app
        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))
            assert mock_bot_app.bot.send_message.call_count == 2
            assert not reply_file.exists()
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()

    @pytest.mark.asyncio
    async def test_buttons_attached_only_to_last_chunk(
        self, temp_messages_dir, mock_bot_app, bot_module
    ):
        """Inline keyboard buttons are only attached to the final chunk."""
        outbox = temp_messages_dir / "outbox"

        long_text = "a" * 5000
        reply = {
            "chat_id": 123456,
            "text": long_text,
            "source": "telegram",
            "buttons": [["Yes", "No"]],
        }
        reply_file = outbox / "reply_buttons.json"
        reply_file.write_text(json.dumps(reply))

        handler = bot_module.OutboxHandler()
        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app
        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))

            calls = mock_bot_app.bot.send_message.call_args_list
            assert len(calls) >= 2
            # All chunks except the last must have no reply_markup
            for call in calls[:-1]:
                assert call.kwargs.get("reply_markup") is None
            # Last chunk must have reply_markup
            assert calls[-1].kwargs.get("reply_markup") is not None
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()

    @pytest.mark.asyncio
    async def test_short_message_sends_single_call(
        self, temp_messages_dir, mock_bot_app, bot_module
    ):
        """A short message results in exactly one send_message call."""
        outbox = temp_messages_dir / "outbox"

        reply = {
            "chat_id": 123456,
            "text": "Short message.",
            "source": "telegram",
        }
        reply_file = outbox / "reply_short.json"
        reply_file.write_text(json.dumps(reply))

        handler = bot_module.OutboxHandler()
        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app
        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))
            assert mock_bot_app.bot.send_message.call_count == 1
            assert not reply_file.exists()
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()

    @pytest.mark.asyncio
    async def test_continuation_label_in_second_send(
        self, temp_messages_dir, mock_bot_app, bot_module
    ):
        """The second Telegram message includes the continuation label."""
        outbox = temp_messages_dir / "outbox"

        long_text = ("Part one. " + "a" * 3500 + "\n\n"
                     + "Part two. " + "b" * 3500)
        reply = {
            "chat_id": 123456,
            "text": long_text,
            "source": "telegram",
        }
        reply_file = outbox / "reply_cont.json"
        reply_file.write_text(json.dumps(reply))

        handler = bot_module.OutboxHandler()
        original_bot_app = bot_module.bot_app
        bot_module.bot_app = mock_bot_app
        loop = asyncio.new_event_loop()
        bot_module.main_loop = loop

        try:
            await handler.process_reply(str(reply_file))
            calls = mock_bot_app.bot.send_message.call_args_list
            assert len(calls) >= 2
            # Second call text (after HTML conversion) should contain "continued"
            second_text = calls[1].kwargs.get("text", "")
            assert "continued" in second_text.lower()
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()
