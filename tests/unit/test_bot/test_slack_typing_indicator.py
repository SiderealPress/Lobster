"""
Tests for the Slack post-then-update typing indicator pattern.

When a Slack reply is delivered, the router should:
1. Post a "..." placeholder immediately via chat.postMessage (with xoxp- token)
2. Capture the ts of the placeholder message
3. Call chat.update with the real reply text to replace the placeholder

This simulates a typing experience: users see the message appear, then fill in.

If the placeholder post fails (API error), the router must fall back to
posting the real text directly — no silent drop.

If LOBSTER_SLACK_TYPING_INDICATOR=false, the placeholder step is skipped
and the real text is posted directly (original behavior).
"""

import os
import importlib
import pytest
from unittest.mock import MagicMock, patch, call


PLACEHOLDER_TEXT = "..."
# The channel and ts values used throughout the tests
TEST_CHANNEL = "C0TEST12345"
TEST_PLACEHOLDER_TS = "1234567890.123456"
TEST_REPLY_TEXT = "Here is the real answer."


def load_slack_router(extra_env: dict | None = None):
    """Import slack_router with required env vars set.

    slack_router raises ValueError at module level if the required tokens are
    absent, so we always patch them in.  Tests can pass extra_env to override
    specific vars (e.g. to disable the typing indicator).
    """
    base_env = {
        "LOBSTER_SLACK_BOT_TOKEN": "xoxb-test-bot-token",
        "LOBSTER_SLACK_APP_TOKEN": "xapp-test-app-token",
        "LOBSTER_SLACK_USER_TOKEN": "xoxp-test-user-token",
        "LOBSTER_SLACK_CHANNEL_REMAP": "",
        "LOBSTER_SLACK_TYPING_INDICATOR": "true",
    }
    if extra_env:
        base_env.update(extra_env)

    with patch.dict(os.environ, base_env):
        # Patch Slack SDK constructors so module-level initialisation does not
        # make real network calls.
        with patch("slack_bolt.App") as mock_app_cls, \
             patch("slack_sdk.WebClient") as mock_client_cls, \
             patch("slack_bolt.adapter.socket_mode.SocketModeHandler"):
            mock_app_cls.return_value = MagicMock()
            # auth_test() is called at module level to get BOT_USER_ID
            mock_web_client = MagicMock()
            mock_web_client.auth_test.return_value = {
                "user_id": "U0BOT999",
                "user": "testbot",
            }
            mock_client_cls.return_value = mock_web_client

            import src.bot.slack_router as router_module
            importlib.reload(router_module)
            return router_module


# ---------------------------------------------------------------------------
# Happy path: placeholder → update
# ---------------------------------------------------------------------------

class TestTypingIndicatorHappyPath:
    """The normal case: placeholder posted, then updated with real text."""

    def _make_user_client(self, placeholder_ts: str = TEST_PLACEHOLDER_TS):
        """Return a mock user_client where postMessage returns the given ts."""
        user_client = MagicMock()
        post_response = MagicMock()
        post_response.__getitem__ = lambda self, key: placeholder_ts if key == "ts" else None
        post_response.get = lambda key, default=None: placeholder_ts if key == "ts" else default
        user_client.chat_postMessage.return_value = post_response
        user_client.chat_update = MagicMock()
        return user_client

    def test_placeholder_is_posted_before_real_text(self):
        """chat.postMessage is first called with the placeholder '...'."""
        router = load_slack_router()
        user_client = self._make_user_client()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        first_call = user_client.chat_postMessage.call_args_list[0]
        assert first_call.kwargs["text"] == PLACEHOLDER_TEXT
        assert first_call.kwargs["channel"] == TEST_CHANNEL

    def test_update_is_called_with_real_text(self):
        """chat.update is called with the real reply text after the placeholder."""
        router = load_slack_router()
        user_client = self._make_user_client()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        user_client.chat_update.assert_called_once()
        update_kwargs = user_client.chat_update.call_args.kwargs
        assert update_kwargs["text"] == TEST_REPLY_TEXT
        assert update_kwargs["channel"] == TEST_CHANNEL
        assert update_kwargs["ts"] == TEST_PLACEHOLDER_TS

    def test_update_uses_ts_from_placeholder_response(self):
        """The ts passed to chat.update comes from the postMessage response, not a constant."""
        unique_ts = "9999999999.000001"
        router = load_slack_router()
        user_client = self._make_user_client(placeholder_ts=unique_ts)
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        update_kwargs = user_client.chat_update.call_args.kwargs
        assert update_kwargs["ts"] == unique_ts

    def test_send_returns_true_on_success(self):
        """_send_slack_reply returns True when both placeholder and update succeed."""
        router = load_slack_router()
        user_client = self._make_user_client()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        result = router._send_slack_reply(reply)
        assert result is True

    def test_thread_ts_passed_to_placeholder_post(self):
        """thread_ts from the reply dict is forwarded to the placeholder postMessage."""
        router = load_slack_router()
        user_client = self._make_user_client()
        router.user_client = user_client

        reply = {
            "chat_id": TEST_CHANNEL,
            "text": TEST_REPLY_TEXT,
            "source": "slack",
            "thread_ts": "1111111111.000001",
        }
        router._send_slack_reply(reply)

        first_call_kwargs = user_client.chat_postMessage.call_args_list[0].kwargs
        assert first_call_kwargs.get("thread_ts") == "1111111111.000001"

    def test_thread_ts_passed_to_update(self):
        """thread_ts is also forwarded to the chat.update call."""
        router = load_slack_router()
        user_client = self._make_user_client()
        router.user_client = user_client

        reply = {
            "chat_id": TEST_CHANNEL,
            "text": TEST_REPLY_TEXT,
            "source": "slack",
            "thread_ts": "1111111111.000001",
        }
        router._send_slack_reply(reply)

        update_kwargs = user_client.chat_update.call_args.kwargs
        assert update_kwargs.get("thread_ts") == "1111111111.000001"

    def test_channel_remap_applied_before_posting_placeholder(self):
        """If channel_remap is configured, it is applied before the placeholder post."""
        router = load_slack_router(
            extra_env={"LOBSTER_SLACK_CHANNEL_REMAP": "CBOT001:CUSER001"}
        )
        user_client = self._make_user_client()
        router.user_client = user_client

        reply = {"chat_id": "CBOT001", "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        first_call_kwargs = user_client.chat_postMessage.call_args_list[0].kwargs
        # After remap, the post should target CUSER001, not CBOT001
        assert first_call_kwargs["channel"] == "CUSER001"

    def test_postmessage_called_once_for_placeholder(self):
        """chat.postMessage is called exactly once for the placeholder (not for the real text)."""
        router = load_slack_router()
        user_client = self._make_user_client()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        # Only one postMessage call: the placeholder. Real text goes via chat.update.
        assert user_client.chat_postMessage.call_count == 1


# ---------------------------------------------------------------------------
# Graceful degradation: placeholder post fails
# ---------------------------------------------------------------------------

class TestTypingIndicatorPlaceholderFails:
    """If the placeholder post fails, fall back to direct postMessage with real text."""

    def test_fallback_to_direct_post_when_placeholder_fails(self):
        """When chat.postMessage raises SlackApiError, real text is posted directly."""
        from slack_sdk.errors import SlackApiError
        router = load_slack_router()
        user_client = MagicMock()
        # First call (placeholder) raises; second call (fallback) succeeds.
        user_client.chat_postMessage.side_effect = [
            SlackApiError("not_allowed", {"error": "not_allowed"}),
            MagicMock(),
        ]
        user_client.chat_update = MagicMock()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        result = router._send_slack_reply(reply)

        # Must have tried twice: placeholder + fallback direct post
        assert user_client.chat_postMessage.call_count == 2
        # The second call must carry the real text
        second_call_kwargs = user_client.chat_postMessage.call_args_list[1].kwargs
        assert second_call_kwargs["text"] == TEST_REPLY_TEXT
        # chat.update must NOT be called (we fell back to direct post)
        user_client.chat_update.assert_not_called()
        assert result is True

    def test_fallback_returns_false_when_both_attempts_fail(self):
        """If both the placeholder AND the fallback post fail, _send_slack_reply returns False."""
        from slack_sdk.errors import SlackApiError
        router = load_slack_router()
        user_client = MagicMock()
        user_client.chat_postMessage.side_effect = SlackApiError("error", {"error": "error"})
        user_client.chat_update = MagicMock()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        result = router._send_slack_reply(reply)

        assert result is False

    def test_no_message_silently_dropped_when_placeholder_fails(self):
        """A SlackApiError on the placeholder does not silently discard the reply."""
        from slack_sdk.errors import SlackApiError
        router = load_slack_router()
        user_client = MagicMock()
        fallback_response = MagicMock()
        user_client.chat_postMessage.side_effect = [
            SlackApiError("restricted", {"error": "restricted"}),
            fallback_response,
        ]
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        # Verify the fallback carried the real text, not empty / placeholder text
        second_call = user_client.chat_postMessage.call_args_list[1]
        assert second_call.kwargs["text"] == TEST_REPLY_TEXT

    def test_update_failure_still_returns_true(self):
        """If chat.update raises after a successful placeholder, we still return True.

        The message was delivered (via the update or the placeholder itself);
        an update failure should not cause the outbox file to be re-queued.
        """
        from slack_sdk.errors import SlackApiError
        router = load_slack_router()
        user_client = self._make_user_client()
        user_client.chat_update.side_effect = SlackApiError("cant_update", {"error": "cant_update"})
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        result = router._send_slack_reply(reply)

        # Even though update failed, the placeholder exists in Slack — return True
        assert result is True

    def _make_user_client(self, placeholder_ts: str = TEST_PLACEHOLDER_TS):
        user_client = MagicMock()
        post_response = MagicMock()
        post_response.get = lambda key, default=None: placeholder_ts if key == "ts" else default
        user_client.chat_postMessage.return_value = post_response
        user_client.chat_update = MagicMock()
        return user_client


# ---------------------------------------------------------------------------
# Opt-out: LOBSTER_SLACK_TYPING_INDICATOR=false
# ---------------------------------------------------------------------------

class TestTypingIndicatorDisabled:
    """When the typing indicator is disabled, the original direct-post behaviour is preserved."""

    def test_disabled_posts_real_text_directly(self):
        """With LOBSTER_SLACK_TYPING_INDICATOR=false, postMessage gets the real text."""
        router = load_slack_router(
            extra_env={"LOBSTER_SLACK_TYPING_INDICATOR": "false"}
        )
        user_client = MagicMock()
        user_client.chat_postMessage.return_value = MagicMock()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        user_client.chat_postMessage.assert_called_once()
        call_kwargs = user_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["text"] == TEST_REPLY_TEXT

    def test_disabled_does_not_call_chat_update(self):
        """With typing indicator disabled, chat.update is never called."""
        router = load_slack_router(
            extra_env={"LOBSTER_SLACK_TYPING_INDICATOR": "false"}
        )
        user_client = MagicMock()
        user_client.chat_postMessage.return_value = MagicMock()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        user_client.chat_update.assert_not_called()

    def test_disabled_returns_true_on_success(self):
        """With typing indicator disabled, _send_slack_reply still returns True on success."""
        router = load_slack_router(
            extra_env={"LOBSTER_SLACK_TYPING_INDICATOR": "false"}
        )
        user_client = MagicMock()
        user_client.chat_postMessage.return_value = MagicMock()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        result = router._send_slack_reply(reply)

        assert result is True

    def test_disabled_zero_is_treated_as_false(self):
        """LOBSTER_SLACK_TYPING_INDICATOR=0 disables the typing indicator (truthy check)."""
        router = load_slack_router(
            extra_env={"LOBSTER_SLACK_TYPING_INDICATOR": "0"}
        )
        user_client = MagicMock()
        user_client.chat_postMessage.return_value = MagicMock()
        router.user_client = user_client

        reply = {"chat_id": TEST_CHANNEL, "text": TEST_REPLY_TEXT, "source": "slack"}
        router._send_slack_reply(reply)

        # Direct post only — no chat.update
        user_client.chat_update.assert_not_called()
        assert user_client.chat_postMessage.call_args.kwargs["text"] == TEST_REPLY_TEXT
