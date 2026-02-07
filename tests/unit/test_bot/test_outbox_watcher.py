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
                chat_id=123456, text="Hello from Lobster!"
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
