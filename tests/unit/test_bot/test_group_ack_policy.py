"""
Tests for Group Chat Ack + Engagement Policy

Covers:
- _is_direct_invocation: entity-based mention, reply-to-bot, neither
- _get_thread_root_id: reply vs top-level
- _is_in_engaged_thread / _mark_thread_engaged / _expire_engaged_threads
- handle_message ack behavior: DM always acks, group acks only for direct/engaged
- msg_data fields: direct_invocation, thread_root_message_id added for group messages
"""

import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import os
import importlib


# ---------------------------------------------------------------------------
# Pure unit tests for engagement helpers — no bot required
# ---------------------------------------------------------------------------


def _load_bot_module():
    """Import and return the lobster_bot module, ensuring required env vars are set.

    lobster_bot.py raises ValueError at module level if TELEGRAM_BOT_TOKEN is
    absent.  Tests that call this helper do not care about the bot token — they
    only need the pure helper functions (_is_direct_invocation, etc.) — so we
    supply dummy values to satisfy the module-level guard.
    """
    with patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "test_token",
        "TELEGRAM_ALLOWED_USERS": "111",
    }):
        import src.bot.lobster_bot as bot_module
        return bot_module


class TestIsDirectInvocation:
    """Tests for _is_direct_invocation()."""

    def test_mention_entity_matching_bot_username(self):
        bot_module = _load_bot_module()
        message = MagicMock()
        message.reply_to_message = None
        # Build an entity that mentions @testbot
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 8  # len("@testbot")
        message.entities = [entity]
        message.caption_entities = []
        message.text = "@testbot hello"
        message.caption = ""
        result = bot_module._is_direct_invocation(message, "testbot")
        assert result is True

    def test_mention_entity_different_bot_username(self):
        bot_module = _load_bot_module()
        message = MagicMock()
        message.reply_to_message = None
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 10  # len("@otherbot")
        message.entities = [entity]
        message.caption_entities = []
        message.text = "@otherbot hello"
        message.caption = ""
        result = bot_module._is_direct_invocation(message, "testbot")
        assert result is False

    def test_no_mention_no_reply(self):
        bot_module = _load_bot_module()
        message = MagicMock()
        message.reply_to_message = None
        message.entities = []
        message.caption_entities = []
        message.text = "hey what's up"
        message.caption = ""
        result = bot_module._is_direct_invocation(message, "testbot")
        assert result is False

    def test_reply_to_bot_message(self):
        bot_module = _load_bot_module()
        message = MagicMock()
        reply_msg = MagicMock()
        reply_msg.from_user.is_bot = True
        reply_msg.from_user.username = "testbot"
        message.reply_to_message = reply_msg
        message.entities = []
        message.caption_entities = []
        message.text = "thanks"
        message.caption = ""
        result = bot_module._is_direct_invocation(message, "testbot")
        assert result is True

    def test_reply_to_human_message(self):
        bot_module = _load_bot_module()
        message = MagicMock()
        reply_msg = MagicMock()
        reply_msg.from_user.is_bot = False
        reply_msg.from_user.username = "someuser"
        message.reply_to_message = reply_msg
        message.entities = []
        message.caption_entities = []
        message.text = "I agree"
        message.caption = ""
        result = bot_module._is_direct_invocation(message, "testbot")
        assert result is False

    def test_empty_bot_username_does_not_crash(self):
        """When bot username is unknown, skip entity check but still check reply."""
        bot_module = _load_bot_module()
        message = MagicMock()
        message.reply_to_message = None
        message.entities = []
        message.caption_entities = []
        message.text = "@unknownbot hi"
        message.caption = ""
        result = bot_module._is_direct_invocation(message, "")
        assert result is False


class TestGetThreadRootId:
    """Tests for _get_thread_root_id()."""

    def test_top_level_message_returns_none(self):
        bot_module = _load_bot_module()
        message = MagicMock()
        message.reply_to_message = None
        assert bot_module._get_thread_root_id(message) is None

    def test_reply_returns_replied_to_message_id(self):
        bot_module = _load_bot_module()
        message = MagicMock()
        message.reply_to_message.message_id = 42
        assert bot_module._get_thread_root_id(message) == 42


class TestEngagementWindow:
    """Tests for the in-memory engagement tracking helpers."""

    def setup_method(self):
        """Clear engaged threads before each test."""
        bot_module = _load_bot_module()
        bot_module._engaged_threads.clear()

    def test_not_engaged_initially(self):
        bot_module = _load_bot_module()
        assert bot_module._is_in_engaged_thread(1001, None) is False
        assert bot_module._is_in_engaged_thread(1001, 7) is False

    def test_mark_and_check_engaged(self):
        bot_module = _load_bot_module()
        bot_module._mark_thread_engaged(1001, 7)
        assert bot_module._is_in_engaged_thread(1001, 7) is True

    def test_different_thread_root_not_engaged(self):
        bot_module = _load_bot_module()
        bot_module._mark_thread_engaged(1001, 7)
        assert bot_module._is_in_engaged_thread(1001, 99) is False

    def test_different_chat_not_engaged(self):
        bot_module = _load_bot_module()
        bot_module._mark_thread_engaged(1001, 7)
        assert bot_module._is_in_engaged_thread(9999, 7) is False

    def test_expires_after_window(self):
        bot_module = _load_bot_module()
        # Manually insert a stale entry
        bot_module._engaged_threads[(1001, 7)] = time.time() - bot_module.ENGAGEMENT_WINDOW_SECONDS - 1
        assert bot_module._is_in_engaged_thread(1001, 7) is False

    def test_expire_removes_stale_only(self):
        bot_module = _load_bot_module()
        bot_module._mark_thread_engaged(1001, 7)  # fresh
        bot_module._engaged_threads[(1002, 5)] = time.time() - bot_module.ENGAGEMENT_WINDOW_SECONDS - 1  # stale
        bot_module._expire_engaged_threads()
        assert bot_module._is_in_engaged_thread(1001, 7) is True
        assert bot_module._is_in_engaged_thread(1002, 5) is False

    def test_mark_refreshes_window(self):
        bot_module = _load_bot_module()
        # Set a nearly-expired entry
        bot_module._engaged_threads[(1001, 7)] = time.time() - (bot_module.ENGAGEMENT_WINDOW_SECONDS - 5)
        # Refresh it
        bot_module._mark_thread_engaged(1001, 7)
        # Should be alive for another full window
        assert bot_module._is_in_engaged_thread(1001, 7) is True


# ---------------------------------------------------------------------------
# Integration-style tests for handle_message ack behavior
# ---------------------------------------------------------------------------


class TestGroupAckPolicy:
    """Test that acks are sent/suppressed correctly in group vs DM context."""

    @pytest.fixture(autouse=True)
    def clear_engagement(self):
        """Reset engagement state between tests."""
        import src.bot.lobster_bot as bot_module
        bot_module._engaged_threads.clear()
        yield
        bot_module._engaged_threads.clear()

    def _make_group_message(self, text="hello", chat_id=-100123, is_mention=False,
                             is_reply_to_bot=False, message_id=10):
        """Build a mock Update for a group text message."""
        update = MagicMock()
        user = update.effective_user
        user.id = 111
        user.first_name = "TestAdmin"
        user.username = "drewtest"

        msg = update.message
        msg.message_id = message_id
        msg.chat_id = chat_id
        msg.text = text
        msg.voice = None
        msg.audio = None
        msg.photo = None
        msg.document = None
        msg.reply_text = AsyncMock()
        msg.caption = None

        chat = msg.chat
        chat.id = chat_id
        chat.type = "supergroup"
        chat.title = "Test Group"

        if is_mention:
            entity = MagicMock()
            entity.type = "mention"
            entity.offset = 0
            entity.length = len("@testbot")
            msg.entities = [entity]
            msg.text = "@testbot " + text
        else:
            msg.entities = []
        msg.caption_entities = []

        if is_reply_to_bot:
            reply_msg = MagicMock()
            reply_msg.message_id = 5
            reply_msg.from_user.is_bot = True
            reply_msg.from_user.username = "testbot"
            msg.reply_to_message = reply_msg
        else:
            msg.reply_to_message = None

        return update

    def _make_dm_message(self, text="hello", user_id=111):
        """Build a mock Update for a DM message."""
        update = MagicMock()
        user = update.effective_user
        user.id = user_id
        user.first_name = "TestAdmin"
        user.username = "drewtest"

        msg = update.message
        msg.message_id = 1
        msg.chat_id = user_id
        msg.text = text
        msg.voice = None
        msg.audio = None
        msg.photo = None
        msg.document = None
        msg.reply_text = AsyncMock()
        msg.entities = []
        msg.caption_entities = []
        msg.caption = None
        msg.reply_to_message = None

        chat = msg.chat
        chat.id = user_id
        chat.type = "private"
        chat.title = None

        return update

    @pytest.mark.asyncio
    async def test_dm_always_acks(self, temp_messages_dir):
        """DM messages always get an acknowledgment."""
        inbox = temp_messages_dir / "inbox"
        update = self._make_dm_message(user_id=111)
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_GROUP_GATING_ENABLED", False), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None):
                await bot_module.handle_message(update, context)

            update.message.reply_text.assert_called_once()
            assert "received" in update.message.reply_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_group_passive_message_no_ack(self, temp_messages_dir):
        """Group messages without @mention or reply don't get acked."""
        inbox = temp_messages_dir / "inbox"
        update = self._make_group_message(text="has anyone seen my keys", is_mention=False)
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"):
                await bot_module.handle_message(update, context)

            update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_passive_message_written_to_inbox(self, temp_messages_dir):
        """Passive group messages are still written to inbox (processed silently)."""
        inbox = temp_messages_dir / "inbox"
        update = self._make_group_message(text="anyone up for lunch", is_mention=False)
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"):
                await bot_module.handle_message(update, context)

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["direct_invocation"] is False

    @pytest.mark.asyncio
    async def test_group_mention_acks(self, temp_messages_dir):
        """Group message with @mention gets acked."""
        inbox = temp_messages_dir / "inbox"
        update = self._make_group_message(text="lookup the weather", is_mention=True)
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"):
                await bot_module.handle_message(update, context)

            update.message.reply_text.assert_called_once()
            ack_text = update.message.reply_text.call_args[0][0]
            assert "got it" in ack_text.lower() or "processing" in ack_text.lower()

    @pytest.mark.asyncio
    async def test_group_mention_sets_direct_invocation_true(self, temp_messages_dir):
        """direct_invocation=True is set in msg_data for @mention messages."""
        inbox = temp_messages_dir / "inbox"
        update = self._make_group_message(text="summarize the docs", is_mention=True)
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"):
                await bot_module.handle_message(update, context)

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["direct_invocation"] is True

    @pytest.mark.asyncio
    async def test_group_reply_to_bot_acks(self, temp_messages_dir):
        """Reply to a bot message in a group triggers an ack."""
        inbox = temp_messages_dir / "inbox"
        update = self._make_group_message(text="can you elaborate", is_reply_to_bot=True)
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"):
                await bot_module.handle_message(update, context)

            update.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_engaged_thread_continuation_acks(self, temp_messages_dir):
        """Messages in an active engagement window get acked even without @mention."""
        inbox = temp_messages_dir / "inbox"
        # First message with @mention establishes engagement
        update1 = self._make_group_message(text="what time is it", is_mention=True, message_id=10)
        update2 = self._make_group_message(
            text="and what about tomorrow",
            is_mention=False,
            message_id=11,
        )
        # Make update2 a reply in the same thread (reply to message 10)
        update2.message.reply_to_message = MagicMock()
        update2.message.reply_to_message.message_id = 10
        update2.message.reply_to_message.from_user.is_bot = False
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            shared_patches = dict(
                _check_group_gating=True,
                wake_claude_if_hibernating=lambda: None,
                is_user_onboarded=return_value_True,
                _get_bot_username=return_value_testbot,
                extract_reply_to_context=return_value_None,
                get_source_for_chat=return_value_lobster_group,
            )

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"):
                # First message: direct invocation, sets engagement for thread root=None
                await bot_module.handle_message(update1, context)
                # Second message: not a mention, but in the engaged thread
                await bot_module.handle_message(update2, context)

            # Both should have been acked
            assert update1.message.reply_text.call_count == 1
            assert update2.message.reply_text.call_count == 1

    @pytest.mark.asyncio
    async def test_group_message_has_thread_root_in_msg_data(self, temp_messages_dir):
        """thread_root_message_id is populated in msg_data for group messages."""
        inbox = temp_messages_dir / "inbox"
        update = self._make_group_message(is_reply_to_bot=True, message_id=20)
        # reply_to_message.message_id = 5 (set in _make_group_message)
        context = MagicMock()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": "111",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"):
                await bot_module.handle_message(update, context)

            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["thread_root_message_id"] == 5


class TestSecondUserCloseEdgeCase:
    """Closure signals from a non-invoker must not close an open session.

    Scenario: User A @mentions the bot (opens a session). User B (also
    authorized) says "thanks" in the same chat. The session must stay open.
    Only when User A says "thanks" should the session close.
    """

    USER_A = 111  # session invoker
    USER_B = 222  # different authorized user
    CHAT_ID = -100123

    @pytest.fixture(autouse=True)
    def clear_engagement(self):
        import src.bot.lobster_bot as bot_module
        bot_module._engaged_threads.clear()
        yield
        bot_module._engaged_threads.clear()

    def _make_group_message(self, text, user_id, chat_id=CHAT_ID,
                            is_mention=False, message_id=10):
        """Build a mock Update with a configurable sender."""
        update = MagicMock()
        user = update.effective_user
        user.id = user_id
        user.first_name = "User" + str(user_id)
        user.username = "user" + str(user_id)

        msg = update.message
        msg.message_id = message_id
        msg.chat_id = chat_id
        msg.text = text
        msg.voice = None
        msg.audio = None
        msg.photo = None
        msg.document = None
        msg.reply_text = AsyncMock()
        msg.caption = None
        msg.caption_entities = []
        msg.reply_to_message = None

        chat = msg.chat
        chat.id = chat_id
        chat.type = "supergroup"
        chat.title = "Test Group"

        if is_mention:
            entity = MagicMock()
            entity.type = "mention"
            entity.offset = 0
            entity.length = len("@testbot")
            msg.entities = [entity]
            msg.text = "@testbot " + text
        else:
            msg.entities = []

        return update

    @pytest.mark.asyncio
    async def test_user_b_closure_signal_does_not_close_session(
        self, temp_messages_dir
    ):
        """Session opened by user A stays open when user B sends a closure signal.

        This test drives handle_message with get_active_session/close_session
        mocked so we can inspect exactly which calls were made. The mock
        simulates an active session owned by USER_A.

        Test flow:
          1. User A @mentions bot → session opens (mocked open_session returns session)
          2. User B sends "thanks" → get_active_session returns USER_A's session
             → close_session must NOT be called
          3. User A (who is session invoker) is in the engaged thread → sends "thanks"
             → close_session MUST be called once
        """
        inbox = temp_messages_dir / "inbox"
        context = MagicMock()

        # User B sends a closure signal (should NOT close the session)
        update_b_thanks = self._make_group_message(
            text="thanks", user_id=self.USER_B, message_id=11
        )
        # User A sends a closure signal (SHOULD close the session)
        update_a_thanks = self._make_group_message(
            text="thanks", user_id=self.USER_A, message_id=12
        )

        from multiplayer_telegram_bot.session import GroupSession
        from datetime import datetime, timedelta, timezone

        # An active session owned by USER_A
        active_session = GroupSession(
            chat_id=self.CHAT_ID,
            invoker_user_id=self.USER_A,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
            active=True,
        )

        close_session_calls: list = []

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_ALLOWED_USERS": f"{self.USER_A},{self.USER_B}",
        }):
            import src.bot.lobster_bot as bot_module
            importlib.reload(bot_module)
            bot_module._engaged_threads.clear()

            with patch.object(bot_module, "INBOX_DIR", inbox), \
                 patch.object(bot_module, "_check_group_gating", return_value=True), \
                 patch.object(bot_module, "wake_claude_if_hibernating", lambda: None), \
                 patch.object(bot_module, "is_user_onboarded", return_value=True), \
                 patch.object(bot_module, "_get_bot_username", return_value="testbot"), \
                 patch.object(bot_module, "extract_reply_to_context", return_value=None), \
                 patch.object(bot_module, "get_source_for_chat", return_value="lobster-group"), \
                 patch.object(bot_module, "_GROUP_SESSION_ENABLED", True), \
                 patch.object(bot_module, "get_active_session", return_value=active_session), \
                 patch.object(bot_module, "open_session") as mock_open, \
                 patch.object(bot_module, "close_session", side_effect=lambda cid: close_session_calls.append(cid)) as mock_close:

                # Step 1: User B sends "thanks" — session must NOT close
                # USER_B is not in an engaged thread and is not the invoker, so
                # even if closure signal is detected, it should not fire for them.
                await bot_module.handle_message(update_b_thanks, context)

                assert mock_close.call_count == 0, (
                    f"close_session must NOT be called when non-invoker (user B) "
                    f"sends a closure signal; was called {mock_close.call_count} times"
                )

                # Step 2: User A (the invoker) is in an engaged thread and says "thanks"
                # → session SHOULD close
                bot_module._mark_thread_engaged(self.CHAT_ID, None)
                await bot_module.handle_message(update_a_thanks, context)

                assert mock_close.call_count == 1, (
                    f"close_session must be called exactly once when the invoker "
                    f"(user A) sends a closure signal; was called {mock_close.call_count} times"
                )
                assert close_session_calls[0] == self.CHAT_ID


# Sentinel values reused in test (avoids MagicMock confusion with return_value=True)
return_value_True = MagicMock(return_value=True)
return_value_testbot = MagicMock(return_value="testbot")
return_value_None = MagicMock(return_value=None)
return_value_lobster_group = MagicMock(return_value="lobster-group")
