"""
Tests for is_invocation() and is_session_followup() in multiplayer_telegram_bot.gating.
"""

from datetime import datetime, timedelta, timezone

import pytest

from multiplayer_telegram_bot.gating import is_invocation, is_session_followup
from multiplayer_telegram_bot.session import GroupSession

BOT_USERNAME = "Awp_Sebastian_bot"
BOT_USER_ID = 8796720409

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity(etype: str, offset: int, length: int) -> dict:
    return {"type": etype, "offset": offset, "length": length}


def _future_session(chat_id: int, invoker_user_id: int) -> GroupSession:
    return GroupSession(
        chat_id=chat_id,
        invoker_user_id=invoker_user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        active=True,
    )


def _expired_session(chat_id: int, invoker_user_id: int) -> GroupSession:
    return GroupSession(
        chat_id=chat_id,
        invoker_user_id=invoker_user_id,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        active=True,
    )


# ---------------------------------------------------------------------------
# is_invocation tests
# ---------------------------------------------------------------------------

class TestIsInvocationMention:
    def test_mention_via_entity(self):
        text = "@Awp_Sebastian_bot what is the weather?"
        entities = [_make_entity("mention", 0, len("@Awp_Sebastian_bot"))]
        assert is_invocation(text, BOT_USERNAME, BOT_USER_ID, entities, None)

    def test_mention_via_entity_case_insensitive(self):
        text = "@awp_sebastian_bot hello"
        entities = [_make_entity("mention", 0, len("@awp_sebastian_bot"))]
        assert is_invocation(text, BOT_USERNAME, BOT_USER_ID, entities, None)

    def test_mention_in_middle_of_text(self):
        text = "hey @Awp_Sebastian_bot can you help?"
        offset = text.index("@")
        entities = [_make_entity("mention", offset, len("@Awp_Sebastian_bot"))]
        assert is_invocation(text, BOT_USERNAME, BOT_USER_ID, entities, None)

    def test_different_mention_not_invocation(self):
        text = "@OtherBot hello"
        entities = [_make_entity("mention", 0, len("@OtherBot"))]
        assert not is_invocation(text, BOT_USERNAME, BOT_USER_ID, entities, None)

    def test_mention_fallback_string_search_no_entities(self):
        text = "Hey @Awp_Sebastian_bot are you there?"
        assert is_invocation(text, BOT_USERNAME, BOT_USER_ID, None, None)

    def test_mention_fallback_case_insensitive(self):
        text = "@AWP_SEBASTIAN_BOT yo"
        assert is_invocation(text, BOT_USERNAME, BOT_USER_ID, None, None)

    def test_no_mention_no_invocation(self):
        text = "Hey everyone what's up"
        assert not is_invocation(text, BOT_USERNAME, BOT_USER_ID, None, None)

    def test_partial_username_not_invocation(self):
        # "@Awp_Sebastian" (missing "_bot") should not trigger
        text = "@Awp_Sebastian what's up"
        assert not is_invocation(text, BOT_USERNAME, BOT_USER_ID, None, None)


class TestIsInvocationCommand:
    def test_slash_command(self):
        assert is_invocation("/start", BOT_USERNAME, BOT_USER_ID, None, None)

    def test_slash_help(self):
        assert is_invocation("/help", BOT_USERNAME, BOT_USER_ID, None, None)

    def test_slash_any_command(self):
        assert is_invocation("/mycommand@Awp_Sebastian_bot", BOT_USERNAME, BOT_USER_ID, None, None)

    def test_not_command_without_slash(self):
        assert not is_invocation("start", BOT_USERNAME, BOT_USER_ID, None, None)


class TestIsInvocationReply:
    def test_reply_to_bot(self):
        assert is_invocation(
            "yes please",
            BOT_USERNAME,
            BOT_USER_ID,
            None,
            reply_to_user_id=BOT_USER_ID,
        )

    def test_reply_to_other_user_not_invocation(self):
        assert not is_invocation(
            "yes please",
            BOT_USERNAME,
            BOT_USER_ID,
            None,
            reply_to_user_id=12345,
        )

    def test_reply_to_none_not_invocation(self):
        assert not is_invocation(
            "just a message",
            BOT_USERNAME,
            BOT_USER_ID,
            None,
            reply_to_user_id=None,
        )


class TestIsInvocationEdgeCases:
    def test_none_text_returns_false(self):
        assert not is_invocation(None, BOT_USERNAME, BOT_USER_ID, None, None)

    def test_empty_text_returns_false(self):
        assert not is_invocation("", BOT_USERNAME, BOT_USER_ID, None, None)

    def test_none_text_with_reply_to_bot(self):
        # Even with no text, reply-to-bot is an invocation
        assert is_invocation(None, BOT_USERNAME, BOT_USER_ID, None, BOT_USER_ID)

    def test_entity_list_empty(self):
        text = "hello world"
        assert not is_invocation(text, BOT_USERNAME, BOT_USER_ID, [], None)

    def test_entity_wrong_type_ignored(self):
        # entity type "bold" does not trigger mention detection
        # When entities list is provided (non-None), string-search fallback is
        # NOT used — entities is authoritative. A non-mention entity type means
        # this is NOT an @mention invocation.
        text = "@Awp_Sebastian_bot"
        entities = [_make_entity("bold", 0, len(text))]
        # entities list is provided but contains no "mention" type → not invoked
        assert not is_invocation(text, BOT_USERNAME, BOT_USER_ID, entities, None)


# ---------------------------------------------------------------------------
# is_session_followup tests
# ---------------------------------------------------------------------------

class TestIsSessionFollowup:
    def test_active_session_matching_user(self):
        session = _future_session(-100, invoker_user_id=42)
        assert is_session_followup(-100, 42, session)

    def test_no_session_returns_false(self):
        assert not is_session_followup(-100, 42, None)

    def test_expired_session_returns_false(self):
        session = _expired_session(-100, invoker_user_id=42)
        assert not is_session_followup(-100, 42, session)

    def test_inactive_session_returns_false(self):
        session = _future_session(-100, invoker_user_id=42)
        session.active = False
        assert not is_session_followup(-100, 42, session)

    def test_different_user_returns_false(self):
        session = _future_session(-100, invoker_user_id=42)
        assert not is_session_followup(-100, 99, session)

    def test_matching_user_id_int_comparison(self):
        # Ensure int vs int comparison works correctly
        session = _future_session(-100, invoker_user_id=8796720409)
        assert is_session_followup(-100, 8796720409, session)
