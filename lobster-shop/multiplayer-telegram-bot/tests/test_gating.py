"""
Tests for gating.py — pure function unit tests, no I/O required.
"""

import pytest

from multiplayer_telegram_bot.gating import (
    GatingAction,
    GatingResult,
    gate_message,
    gate_messages,
    should_allow,
    should_drop,
    should_register,
)
from multiplayer_telegram_bot.whitelist import WhitelistStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_store(groups: dict | None = None) -> WhitelistStore:
    return {"groups": groups or {}}


def make_group(enabled: bool = True, user_ids: list[int] | None = None) -> dict:
    return {
        "name": "Test Group",
        "enabled": enabled,
        "allowed_user_ids": user_ids or [],
    }


GROUP_ID = -1001234567890
USER_A = 111111
USER_B = 222222
USER_UNKNOWN = 999999


# ---------------------------------------------------------------------------
# gate_message: group not in whitelist
# ---------------------------------------------------------------------------

class TestGateMessageGroupNotInWhitelist:
    def test_unknown_group_drops_silently(self):
        store = make_store({})
        result = gate_message(GROUP_ID, USER_A, store)
        assert result.action == GatingAction.DROP_SILENT

    def test_unknown_group_preserves_chat_id(self):
        store = make_store({})
        result = gate_message(GROUP_ID, USER_A, store)
        assert result.chat_id == GROUP_ID

    def test_unknown_group_preserves_user_id(self):
        store = make_store({})
        result = gate_message(GROUP_ID, USER_A, store)
        assert result.user_id == USER_A

    def test_disabled_group_drops_silently(self):
        store = make_store({str(GROUP_ID): make_group(enabled=False, user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_A, store)
        assert result.action == GatingAction.DROP_SILENT

    def test_reason_is_string(self):
        store = make_store({})
        result = gate_message(GROUP_ID, USER_A, store)
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0


# ---------------------------------------------------------------------------
# gate_message: group enabled, user whitelisted
# ---------------------------------------------------------------------------

class TestGateMessageUserAllowed:
    def test_whitelisted_user_is_allowed(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_A, store)
        assert result.action == GatingAction.ALLOW

    def test_second_whitelisted_user_is_allowed(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A, USER_B])})
        result = gate_message(GROUP_ID, USER_B, store)
        assert result.action == GatingAction.ALLOW

    def test_allowed_result_preserves_ids(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_A, store)
        assert result.chat_id == GROUP_ID
        assert result.user_id == USER_A


# ---------------------------------------------------------------------------
# gate_message: group enabled, user NOT whitelisted
# ---------------------------------------------------------------------------

class TestGateMessageUserNotAllowed:
    def test_unknown_user_in_enabled_group_triggers_registration(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_UNKNOWN, store)
        assert result.action == GatingAction.SEND_REGISTRATION_DM

    def test_empty_whitelist_group_triggers_registration(self):
        # Group is enabled but has no allowed users yet
        store = make_store({str(GROUP_ID): make_group(user_ids=[])})
        result = gate_message(GROUP_ID, USER_A, store)
        assert result.action == GatingAction.SEND_REGISTRATION_DM

    def test_registration_result_preserves_ids(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_UNKNOWN, store)
        assert result.chat_id == GROUP_ID
        assert result.user_id == USER_UNKNOWN


# ---------------------------------------------------------------------------
# GatingResult is a NamedTuple — structural tests
# ---------------------------------------------------------------------------

class TestGatingResultStructure:
    def test_result_is_namedtuple(self):
        store = make_store({})
        result = gate_message(GROUP_ID, USER_A, store)
        assert isinstance(result, GatingResult)

    def test_result_has_action(self):
        store = make_store({})
        result = gate_message(GROUP_ID, USER_A, store)
        assert isinstance(result.action, GatingAction)

    def test_result_has_reason(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_A, store)
        assert isinstance(result.reason, str)


# ---------------------------------------------------------------------------
# Convenience predicates
# ---------------------------------------------------------------------------

class TestConveniencePredicates:
    def test_should_allow_true_for_allow(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_A, store)
        assert should_allow(result) is True
        assert should_drop(result) is False
        assert should_register(result) is False

    def test_should_drop_true_for_unknown_group(self):
        store = make_store({})
        result = gate_message(GROUP_ID, USER_A, store)
        assert should_drop(result) is True
        assert should_allow(result) is False
        assert should_register(result) is False

    def test_should_register_true_for_unknown_user(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        result = gate_message(GROUP_ID, USER_UNKNOWN, store)
        assert should_register(result) is True
        assert should_allow(result) is False
        assert should_drop(result) is False


# ---------------------------------------------------------------------------
# Batch gating
# ---------------------------------------------------------------------------

class TestGateMessages:
    def test_batch_returns_same_length(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        messages = [
            {"chat_id": GROUP_ID, "user_id": USER_A, "text": "hi"},
            {"chat_id": GROUP_ID, "user_id": USER_UNKNOWN, "text": "hello"},
        ]
        results = gate_messages(messages, store)
        assert len(results) == 2

    def test_batch_returns_message_with_result_pairs(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        messages = [{"chat_id": GROUP_ID, "user_id": USER_A, "text": "hi"}]
        results = gate_messages(messages, store)
        msg, result = results[0]
        assert msg["text"] == "hi"
        assert isinstance(result, GatingResult)

    def test_batch_correctly_classifies_mixed_messages(self):
        store = make_store({str(GROUP_ID): make_group(user_ids=[USER_A])})
        messages = [
            {"chat_id": GROUP_ID, "user_id": USER_A, "text": "allowed"},
            {"chat_id": GROUP_ID, "user_id": USER_UNKNOWN, "text": "unknown"},
            {"chat_id": -9999999, "user_id": USER_A, "text": "unknown group"},
        ]
        results = gate_messages(messages, store)

        _, r0 = results[0]
        _, r1 = results[1]
        _, r2 = results[2]

        assert r0.action == GatingAction.ALLOW
        assert r1.action == GatingAction.SEND_REGISTRATION_DM
        assert r2.action == GatingAction.DROP_SILENT

    def test_empty_batch_returns_empty(self):
        store = make_store({})
        results = gate_messages([], store)
        assert results == []
