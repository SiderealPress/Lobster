"""
Tests for router.py — pure function unit tests, no I/O required.
"""

import pytest

from multiplayer_telegram_bot.router import (
    CHAT_TYPE_CHANNEL,
    CHAT_TYPE_GROUP,
    CHAT_TYPE_PRIVATE,
    CHAT_TYPE_SUPERGROUP,
    SOURCE_DEFAULT,
    SOURCE_GROUP,
    build_inbox_message,
    classify_message,
    get_source_for_chat,
    is_group_message,
)


# ---------------------------------------------------------------------------
# is_group_message
# ---------------------------------------------------------------------------

class TestIsGroupMessage:
    def test_group_returns_true(self):
        assert is_group_message("group") is True

    def test_supergroup_returns_true(self):
        assert is_group_message("supergroup") is True

    def test_private_returns_false(self):
        assert is_group_message("private") is False

    def test_channel_returns_false(self):
        assert is_group_message("channel") is False

    def test_empty_string_returns_false(self):
        assert is_group_message("") is False

    def test_unknown_type_returns_false(self):
        assert is_group_message("unknown") is False

    def test_case_sensitive(self):
        # Chat types from Telegram API are lowercase
        assert is_group_message("Group") is False
        assert is_group_message("SUPERGROUP") is False


# ---------------------------------------------------------------------------
# get_source_for_chat
# ---------------------------------------------------------------------------

class TestGetSourceForChat:
    def test_group_returns_lobster_group(self):
        assert get_source_for_chat("group") == SOURCE_GROUP

    def test_supergroup_returns_lobster_group(self):
        assert get_source_for_chat("supergroup") == SOURCE_GROUP

    def test_private_returns_default(self):
        assert get_source_for_chat("private") == SOURCE_DEFAULT

    def test_channel_returns_default(self):
        assert get_source_for_chat("channel") == SOURCE_DEFAULT

    def test_empty_returns_default(self):
        assert get_source_for_chat("") == SOURCE_DEFAULT

    def test_custom_default_source_for_private(self):
        result = get_source_for_chat("private", default_source="whatsapp")
        assert result == "whatsapp"

    def test_custom_default_does_not_affect_group(self):
        # Groups always get lobster-group regardless of default_source
        result = get_source_for_chat("group", default_source="whatsapp")
        assert result == SOURCE_GROUP

    def test_custom_default_for_channel(self):
        result = get_source_for_chat("channel", default_source="slack")
        assert result == "slack"


# ---------------------------------------------------------------------------
# build_inbox_message
# ---------------------------------------------------------------------------

class TestBuildInboxMessage:
    def test_private_message_uses_default_source(self):
        msg = build_inbox_message(
            text="Hello",
            chat_id=123456,
            user_id=123456,
            chat_type="private",
        )
        assert msg["source"] == SOURCE_DEFAULT

    def test_group_message_uses_lobster_group_source(self):
        msg = build_inbox_message(
            text="Hello group",
            chat_id=-1001234567890,
            user_id=111111,
            chat_type="group",
        )
        assert msg["source"] == SOURCE_GROUP

    def test_supergroup_message_uses_lobster_group_source(self):
        msg = build_inbox_message(
            text="Supergroup message",
            chat_id=-1009876543210,
            user_id=222222,
            chat_type="supergroup",
        )
        assert msg["source"] == SOURCE_GROUP

    def test_required_fields_present(self):
        msg = build_inbox_message(
            text="Test",
            chat_id=123,
            user_id=456,
            chat_type="private",
        )
        assert "text" in msg
        assert "chat_id" in msg
        assert "user_id" in msg
        assert "source" in msg
        assert "timestamp" in msg
        assert "chat_type" in msg

    def test_text_preserved(self):
        msg = build_inbox_message(
            text="Hello, world!",
            chat_id=1,
            user_id=2,
            chat_type="private",
        )
        assert msg["text"] == "Hello, world!"

    def test_chat_id_preserved(self):
        msg = build_inbox_message(
            text="x",
            chat_id=-1001234567890,
            user_id=1,
            chat_type="group",
        )
        assert msg["chat_id"] == -1001234567890

    def test_user_id_preserved(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=987654321,
            chat_type="private",
        )
        assert msg["user_id"] == 987654321

    def test_optional_username_included_when_provided(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
            username="testuser",
        )
        assert msg["username"] == "testuser"

    def test_optional_username_omitted_when_none(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
            username=None,
        )
        assert "username" not in msg

    def test_optional_first_name(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
            first_name="Alice",
        )
        assert msg["first_name"] == "Alice"

    def test_optional_last_name(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
            last_name="Smith",
        )
        assert msg["last_name"] == "Smith"

    def test_optional_message_id(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
            message_id=42,
        )
        assert msg["message_id"] == 42

    def test_message_id_omitted_when_none(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
        )
        assert "message_id" not in msg

    def test_custom_timestamp_used_when_provided(self):
        ts = "2026-03-11T12:00:00+00:00"
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
            timestamp=ts,
        )
        assert msg["timestamp"] == ts

    def test_auto_timestamp_when_not_provided(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
        )
        assert "timestamp" in msg
        assert len(msg["timestamp"]) > 0

    def test_chat_type_preserved_in_message(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="supergroup",
        )
        assert msg["chat_type"] == "supergroup"

    def test_custom_default_source_for_private(self):
        msg = build_inbox_message(
            text="x",
            chat_id=1,
            user_id=2,
            chat_type="private",
            default_source="sms",
        )
        assert msg["source"] == "sms"


# ---------------------------------------------------------------------------
# classify_message
# ---------------------------------------------------------------------------

class TestClassifyMessage:
    def test_group_message_classified_as_group(self):
        result = classify_message(
            chat_id=-100123,
            user_id=111,
            chat_type="group",
            text="hello",
        )
        assert result["is_group"] is True
        assert result["source"] == SOURCE_GROUP
        assert result["requires_gating"] is True

    def test_private_message_not_group(self):
        result = classify_message(
            chat_id=123,
            user_id=111,
            chat_type="private",
            text="hello",
        )
        assert result["is_group"] is False
        assert result["source"] == SOURCE_DEFAULT
        assert result["requires_gating"] is False

    def test_supergroup_requires_gating(self):
        result = classify_message(
            chat_id=-100999,
            user_id=111,
            chat_type="supergroup",
            text="hi",
        )
        assert result["requires_gating"] is True

    def test_chat_id_preserved(self):
        result = classify_message(-100123, 111, "group", "x")
        assert result["chat_id"] == -100123

    def test_user_id_preserved(self):
        result = classify_message(-100123, 42, "group", "x")
        assert result["user_id"] == 42
