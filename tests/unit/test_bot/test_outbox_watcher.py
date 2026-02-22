"""
Tests for Telegram Bot Outbox Watcher

Tests the OutboxHandler that sends replies via Telegram.
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

            mock_bot_app.bot.send_message.assert_called_once_with(
                chat_id=123456, text="Hello from Lobster!",
                parse_mode="Markdown", reply_markup=None
            )

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
    """Tests for the split_message function."""

    @pytest.fixture
    def bot_module(self):
        return get_bot_module()

    def test_short_message_no_split(self, bot_module):
        """Messages under the limit are returned as-is."""
        result = bot_module.split_message("Hello world")
        assert result == ["Hello world"]

    def test_exactly_at_limit(self, bot_module):
        """Message exactly at limit is not split."""
        text = "a" * 4000
        result = bot_module.split_message(text)
        assert result == [text]

    def test_split_at_paragraph_boundary(self, bot_module):
        """Prefers splitting at paragraph boundaries (double newline)."""
        para1 = "a" * 3000
        para2 = "b" * 3000
        text = para1 + "\n\n" + para2
        result = bot_module.split_message(text)
        assert len(result) == 2
        assert result[0] == para1
        assert result[1] == para2

    def test_split_at_single_newline(self, bot_module):
        """Falls back to single newline when no paragraph boundary fits."""
        line = "x" * 200
        # 21 lines of 200 chars each, joined by newlines = ~4200 chars
        text = "\n".join([line] * 21)
        result = bot_module.split_message(text)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 4000

    def test_hard_split(self, bot_module):
        """Falls back to hard split when no newlines present."""
        text = "a" * 8500
        result = bot_module.split_message(text)
        assert len(result) == 3  # 4000 + 4000 + 500
        assert result[0] == "a" * 4000
        assert result[1] == "a" * 4000
        assert result[2] == "a" * 500

    def test_custom_max_length(self, bot_module):
        """Supports custom max_length parameter."""
        text = "a" * 100
        result = bot_module.split_message(text, max_length=30)
        assert len(result) == 4  # 30 + 30 + 30 + 10

    def test_empty_string(self, bot_module):
        """Empty string returns single empty chunk."""
        result = bot_module.split_message("")
        assert result == [""]

    def test_multi_paragraph_split(self, bot_module):
        """Handles multiple paragraphs requiring several splits."""
        paras = ["paragraph " + str(i) + " " + "x" * 1500 for i in range(5)]
        text = "\n\n".join(paras)
        result = bot_module.split_message(text)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 4000


class TestLongMessageSending:
    """Tests that process_reply splits long messages into multiple sends."""

    @pytest.fixture
    def mock_bot_app(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        return app

    @pytest.fixture
    def bot_module(self):
        return get_bot_module()

    @pytest.mark.asyncio
    async def test_long_message_split_into_chunks(self, temp_messages_dir, mock_bot_app, bot_module):
        """Long messages should result in multiple send_message calls."""
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
    async def test_buttons_only_on_last_chunk(self, temp_messages_dir, mock_bot_app, bot_module):
        """Inline keyboard buttons should only be attached to the last chunk."""
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
            assert len(calls) == 2
            # First chunk: no reply_markup
            assert calls[0].kwargs.get("reply_markup") is None
            # Last chunk: has reply_markup
            assert calls[1].kwargs.get("reply_markup") is not None
        finally:
            bot_module.bot_app = original_bot_app
            loop.close()
