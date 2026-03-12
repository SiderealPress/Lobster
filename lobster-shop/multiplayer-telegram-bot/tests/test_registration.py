"""
Tests for registration.py — pure function unit tests with mocked DM sending.
"""

import pytest

from multiplayer_telegram_bot.registration import (
    DEFAULT_REGISTRATION_MESSAGE,
    RegistrationDM,
    SendDMResult,
    build_registration_dm,
    handle_registration_flow,
    send_registration_dm,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

USER_ID = 123456789
GROUP_ID = -1001234567890


def make_send_fn(returns: bool = True) -> tuple:
    """Return a (send_fn, calls_list) pair.

    send_fn records calls in calls_list and returns the given value.
    """
    calls = []

    def send_fn(user_id: int, text: str) -> bool:
        calls.append({"user_id": user_id, "text": text})
        return returns

    return send_fn, calls


def raising_send_fn(user_id: int, text: str) -> bool:
    raise RuntimeError("Telegram API unreachable")


# ---------------------------------------------------------------------------
# build_registration_dm
# ---------------------------------------------------------------------------

class TestBuildRegistrationDM:
    def test_returns_registration_dm_namedtuple(self):
        dm = build_registration_dm(USER_ID, GROUP_ID)
        assert isinstance(dm, RegistrationDM)

    def test_user_id_preserved(self):
        dm = build_registration_dm(USER_ID, GROUP_ID)
        assert dm.user_id == USER_ID

    def test_group_chat_id_preserved(self):
        dm = build_registration_dm(USER_ID, GROUP_ID)
        assert dm.group_chat_id == GROUP_ID

    def test_text_is_default_template_without_group_name(self):
        dm = build_registration_dm(USER_ID, GROUP_ID)
        assert dm.text == DEFAULT_REGISTRATION_MESSAGE

    def test_custom_template_used(self):
        custom = "Please register with /register to access this bot."
        dm = build_registration_dm(USER_ID, GROUP_ID, message_template=custom)
        assert dm.text == custom

    def test_group_name_substituted_in_template(self):
        template = "Hi! To access {group_name}, send /register."
        dm = build_registration_dm(
            USER_ID,
            GROUP_ID,
            group_name="Test Team",
            message_template=template,
        )
        assert "Test Team" in dm.text
        assert "{group_name}" not in dm.text

    def test_group_name_ignored_when_template_has_no_placeholder(self):
        template = "Send /register to get access."
        dm = build_registration_dm(
            USER_ID,
            GROUP_ID,
            group_name="Test Team",
            message_template=template,
        )
        assert dm.text == template

    def test_no_group_name_leaves_placeholder_intact_if_present(self):
        template = "Welcome to {group_name}! Send /register."
        dm = build_registration_dm(
            USER_ID,
            GROUP_ID,
            group_name=None,
            message_template=template,
        )
        # Without group_name, placeholder is not substituted
        assert "{group_name}" in dm.text

    def test_default_message_contains_register(self):
        dm = build_registration_dm(USER_ID, GROUP_ID)
        assert "register" in dm.text.lower()

    def test_text_is_non_empty(self):
        dm = build_registration_dm(USER_ID, GROUP_ID)
        assert len(dm.text) > 0


# ---------------------------------------------------------------------------
# send_registration_dm
# ---------------------------------------------------------------------------

class TestSendRegistrationDM:
    def test_successful_send_returns_success(self):
        send_fn, calls = make_send_fn(returns=True)
        dm = build_registration_dm(USER_ID, GROUP_ID)
        result = send_registration_dm(dm, send_fn)
        assert result.success is True

    def test_failed_send_returns_failure(self):
        send_fn, calls = make_send_fn(returns=False)
        dm = build_registration_dm(USER_ID, GROUP_ID)
        result = send_registration_dm(dm, send_fn)
        assert result.success is False

    def test_user_id_preserved_in_result(self):
        send_fn, _ = make_send_fn()
        dm = build_registration_dm(USER_ID, GROUP_ID)
        result = send_registration_dm(dm, send_fn)
        assert result.user_id == USER_ID

    def test_send_fn_called_with_correct_user_id(self):
        send_fn, calls = make_send_fn()
        dm = build_registration_dm(USER_ID, GROUP_ID)
        send_registration_dm(dm, send_fn)
        assert len(calls) == 1
        assert calls[0]["user_id"] == USER_ID

    def test_send_fn_called_with_correct_text(self):
        send_fn, calls = make_send_fn()
        dm = build_registration_dm(USER_ID, GROUP_ID)
        send_registration_dm(dm, send_fn)
        assert calls[0]["text"] == dm.text

    def test_exception_in_send_fn_returns_failure(self):
        dm = build_registration_dm(USER_ID, GROUP_ID)
        result = send_registration_dm(dm, raising_send_fn)
        assert result.success is False
        assert result.error is not None
        assert "Telegram API unreachable" in result.error

    def test_no_error_on_success(self):
        send_fn, _ = make_send_fn()
        dm = build_registration_dm(USER_ID, GROUP_ID)
        result = send_registration_dm(dm, send_fn)
        assert result.error is None

    def test_returns_send_dm_result_namedtuple(self):
        send_fn, _ = make_send_fn()
        dm = build_registration_dm(USER_ID, GROUP_ID)
        result = send_registration_dm(dm, send_fn)
        assert isinstance(result, SendDMResult)


# ---------------------------------------------------------------------------
# handle_registration_flow (integration of build + send)
# ---------------------------------------------------------------------------

class TestHandleRegistrationFlow:
    def test_successful_flow_returns_success(self):
        send_fn, calls = make_send_fn(returns=True)
        result = handle_registration_flow(USER_ID, GROUP_ID, send_fn)
        assert result.success is True

    def test_send_fn_called_once(self):
        send_fn, calls = make_send_fn()
        handle_registration_flow(USER_ID, GROUP_ID, send_fn)
        assert len(calls) == 1

    def test_group_name_flows_through_to_message(self):
        send_fn, calls = make_send_fn()
        template = "Welcome to {group_name}! Send /register."
        handle_registration_flow(
            USER_ID,
            GROUP_ID,
            send_fn,
            group_name="Test Group",
            message_template=template,
        )
        assert "Test Group" in calls[0]["text"]

    def test_exception_returns_failure_not_raises(self):
        result = handle_registration_flow(USER_ID, GROUP_ID, raising_send_fn)
        assert result.success is False
        assert result.error is not None

    def test_user_id_in_result(self):
        send_fn, _ = make_send_fn()
        result = handle_registration_flow(USER_ID, GROUP_ID, send_fn)
        assert result.user_id == USER_ID
