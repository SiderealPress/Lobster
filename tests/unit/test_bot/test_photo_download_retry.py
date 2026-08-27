"""
Unit tests for the Telegram photo-download retry helper (issue #2252).

Covers `_download_telegram_file_with_retry`, the shared retry wrapper around
`context.bot.get_file` + `file.download_to_drive` used by both
`handle_photo_message` and `_handle_media_group_photo`.

Tests cover:
- Succeeds immediately with no retries when the first attempt works
- Recovers after N transient (TimedOut/NetworkError) failures, within budget
- Gives up and re-raises after exhausting all retry attempts
- Does not retry on a non-network exception (fails fast)
- `_handle_media_group_photo` sends a user-facing failure reply when retries
  are exhausted (previously silent per issue #2252)
"""

import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import Forbidden, NetworkError, TimedOut


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    """Load lobster_bot with a patched environment and a fresh module state."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123456")
    monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path / "messages"))
    (tmp_path / "messages" / "inbox").mkdir(parents=True, exist_ok=True)

    import src.bot.lobster_bot as module

    importlib.reload(module)

    # Keep retry backoff at zero so tests don't actually sleep.
    monkeypatch.setattr(module, "FILE_DOWNLOAD_BACKOFF_SECONDS", 0.0)

    yield module


def _make_context(get_file_side_effects):
    """Build a mock ContextTypes with context.bot.get_file() driven by a side_effect list.

    Each entry in `get_file_side_effects` is either an Exception instance (raised)
    or a MagicMock standing in for the returned `telegram.File` object.
    """
    context = MagicMock()
    context.bot.get_file = AsyncMock(side_effect=get_file_side_effects)
    return context


def _make_file():
    """A mock `telegram.File` with an awaitable download_to_drive()."""
    f = MagicMock()
    f.download_to_drive = AsyncMock(return_value=None)
    return f


class TestDownloadRetryHelper:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt_without_retry(self, bot_module, tmp_path):
        good_file = _make_file()
        context = _make_context([good_file])
        dest = tmp_path / "photo.jpg"

        await bot_module._download_telegram_file_with_retry(context, "file123", dest)

        assert context.bot.get_file.await_count == 1
        good_file.download_to_drive.assert_awaited_once_with(dest)

    @pytest.mark.asyncio
    async def test_recovers_after_transient_timeout_within_retry_budget(self, bot_module, tmp_path):
        good_file = _make_file()
        context = _make_context([TimedOut("simulated read timeout"), good_file])
        dest = tmp_path / "photo.jpg"

        await bot_module._download_telegram_file_with_retry(
            context, "file123", dest, max_attempts=3
        )

        assert context.bot.get_file.await_count == 2
        good_file.download_to_drive.assert_awaited_once_with(dest)

    @pytest.mark.asyncio
    async def test_recovers_from_network_error_on_final_attempt(self, bot_module, tmp_path):
        good_file = _make_file()
        context = _make_context(
            [NetworkError("conn reset"), NetworkError("conn reset"), good_file]
        )
        dest = tmp_path / "photo.jpg"

        await bot_module._download_telegram_file_with_retry(
            context, "file123", dest, max_attempts=3
        )

        assert context.bot.get_file.await_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_all_retry_attempts(self, bot_module, tmp_path):
        max_attempts = 3
        context = _make_context(
            [TimedOut("t1"), TimedOut("t2"), TimedOut("t3"), TimedOut("t4")]
        )
        dest = tmp_path / "photo.jpg"

        with pytest.raises(TimedOut):
            await bot_module._download_telegram_file_with_retry(
                context, "file123", dest, max_attempts=max_attempts
            )

        assert context.bot.get_file.await_count == max_attempts

    @pytest.mark.asyncio
    async def test_non_network_exception_is_not_retried(self, bot_module, tmp_path):
        # Forbidden (e.g. bot blocked/kicked) is a TelegramError but NOT a
        # NetworkError/TimedOut subclass in python-telegram-bot, unlike
        # BadRequest (which *is* a NetworkError subclass upstream and is
        # therefore intentionally retried by this helper).
        context = _make_context([Forbidden("bot was blocked by the user")])
        dest = tmp_path / "photo.jpg"

        with pytest.raises(Forbidden):
            await bot_module._download_telegram_file_with_retry(context, "file123", dest)

        # Fails fast: only one attempt, no retry loop for non-network errors.
        assert context.bot.get_file.await_count == 1


class TestMediaGroupPhotoFailureNotice:
    @pytest.mark.asyncio
    async def test_reports_failure_to_user_when_retries_exhausted(self, bot_module, tmp_path, monkeypatch):
        """Regression test: _handle_media_group_photo used to silently drop a
        photo from an album on download failure, with no user-facing error at
        all. It should now notify the user."""

        async def _always_fails(context, file_id, dest_path, **kwargs):
            raise TimedOut("simulated exhausted retries")

        monkeypatch.setattr(bot_module, "_download_telegram_file_with_retry", _always_fails)

        update = MagicMock()
        message = update.message
        message.media_group_id = "group-1"
        message.chat_id = 555
        message.caption = None
        message.photo = [MagicMock(file_id="file123")]
        message.reply_text = AsyncMock(return_value=None)
        update.effective_user.id = 1
        update.effective_user.username = "tester"
        update.effective_user.first_name = "Tester"

        context = MagicMock()

        await bot_module._handle_media_group_photo(update, context, "msg-1")

        message.reply_text.assert_awaited_once()
        (call_text,), _ = message.reply_text.call_args
        assert "fail" in call_text.lower()

        # The photo must not have been buffered into the media group after failure.
        assert "group-1" not in bot_module._media_group_buffers
