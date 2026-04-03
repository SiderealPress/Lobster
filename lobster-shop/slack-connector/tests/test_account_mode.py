"""Tests for account_mode — token detection, validation, and scope lookup."""

from __future__ import annotations

from typing import Any

import pytest

from src.account_mode import (
    BOT,
    PERSON,
    detect_from_token,
    get_required_scopes,
    resolve_account_type,
    validate_token_mode_match,
    validate_person_token,
    validate_bot_token,
)


# ---------------------------------------------------------------------------
# Test helpers — fake auth_test functions for dependency injection
# ---------------------------------------------------------------------------


def _make_fake_auth_test(
    ok: bool, data: dict[str, Any]
) -> Any:
    """Create a fake _auth_test_fn for dependency injection."""
    def _fake(token: str) -> tuple[bool, dict[str, Any]]:
        return ok, data
    return _fake


# ---------------------------------------------------------------------------
# detect_from_token (pure)
# ---------------------------------------------------------------------------


class TestDetectFromToken:
    def test_bot_token(self) -> None:
        assert detect_from_token("xoxb-123-456-abc") == BOT

    def test_person_token(self) -> None:
        assert detect_from_token("xoxp-123-456-abc") == PERSON

    def test_unknown_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="Unrecognized token prefix"):
            detect_from_token("xapp-123-456")

    def test_empty_token_raises(self) -> None:
        with pytest.raises(ValueError, match="Unrecognized token prefix"):
            detect_from_token("")

    def test_partial_prefix_raises(self) -> None:
        with pytest.raises(ValueError):
            detect_from_token("xox")


# ---------------------------------------------------------------------------
# get_required_scopes (pure)
# ---------------------------------------------------------------------------


class TestGetRequiredScopes:
    def test_bot_scopes_returned(self) -> None:
        scopes = get_required_scopes(BOT)
        assert "channels:history" in scopes
        assert "chat:write" in scopes
        assert isinstance(scopes, list)

    def test_person_scopes_returned(self) -> None:
        scopes = get_required_scopes(PERSON)
        assert "channels:history" in scopes
        assert "channels:write" in scopes
        assert isinstance(scopes, list)

    def test_scopes_are_sorted(self) -> None:
        bot_scopes = get_required_scopes(BOT)
        assert bot_scopes == sorted(bot_scopes)

        person_scopes = get_required_scopes(PERSON)
        assert person_scopes == sorted(person_scopes)

    def test_bot_has_chat_write(self) -> None:
        """Bot mode needs chat:write for responding."""
        assert "chat:write" in get_required_scopes(BOT)

    def test_person_has_channels_write(self) -> None:
        """Person mode needs channels:write for joining channels."""
        assert "channels:write" in get_required_scopes(PERSON)

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid mode"):
            get_required_scopes("invalid")


# ---------------------------------------------------------------------------
# validate_token_mode_match (pure)
# ---------------------------------------------------------------------------


class TestValidateTokenModeMatch:
    def test_bot_token_matches_bot_mode(self) -> None:
        is_match, err = validate_token_mode_match("xoxb-123", BOT)
        assert is_match is True
        assert err == ""

    def test_person_token_matches_person_mode(self) -> None:
        is_match, err = validate_token_mode_match("xoxp-123", PERSON)
        assert is_match is True
        assert err == ""

    def test_bot_token_fails_person_mode(self) -> None:
        is_match, err = validate_token_mode_match("xoxb-123", PERSON)
        assert is_match is False
        assert "Expected user token" in err
        assert "got bot token" in err

    def test_person_token_fails_bot_mode(self) -> None:
        is_match, err = validate_token_mode_match("xoxp-123", BOT)
        assert is_match is False
        assert "Expected bot token" in err
        assert "got user token" in err

    def test_unknown_token_fails(self) -> None:
        is_match, err = validate_token_mode_match("xapp-123", BOT)
        assert is_match is False
        assert "Unrecognized" in err


# ---------------------------------------------------------------------------
# resolve_account_type (pure)
# ---------------------------------------------------------------------------


class TestResolveAccountType:
    def test_default_is_bot(self) -> None:
        assert resolve_account_type() == BOT

    def test_preference_override(self) -> None:
        assert resolve_account_type(preference=PERSON) == PERSON

    def test_env_overrides_preference(self) -> None:
        assert resolve_account_type(env_override=PERSON, preference=BOT) == PERSON

    def test_invalid_falls_back_to_bot(self) -> None:
        assert resolve_account_type(env_override="invalid") == BOT

    def test_empty_env_uses_preference(self) -> None:
        assert resolve_account_type(env_override="", preference=PERSON) == PERSON


# ---------------------------------------------------------------------------
# validate_person_token (side-effect boundary — mocked)
# ---------------------------------------------------------------------------


class TestValidatePersonToken:
    def test_wrong_token_type_rejects_immediately(self) -> None:
        """xoxb- token should fail validation without making HTTP call."""
        ok, info = validate_person_token("xoxb-123-wrong-type")
        assert ok is False
        assert "Expected user token" in info["error"]

    def test_valid_person_token(self) -> None:
        fake = _make_fake_auth_test(True, {
            "user_id": "U123",
            "user": "lobster",
            "team": "MyWorkspace",
            "url": "https://myworkspace.slack.com",
        })
        ok, info = validate_person_token("xoxp-valid-token", _auth_test_fn=fake)
        assert ok is True
        assert info["user_id"] == "U123"
        assert info["name"] == "lobster"

    def test_failed_auth_test(self) -> None:
        fake = _make_fake_auth_test(False, {"error": "invalid_auth"})
        ok, info = validate_person_token("xoxp-bad-token", _auth_test_fn=fake)
        assert ok is False
        assert "invalid_auth" in info["error"]


# ---------------------------------------------------------------------------
# validate_bot_token (side-effect boundary — dependency injected)
# ---------------------------------------------------------------------------


class TestValidateBotToken:
    def test_wrong_token_type_rejects(self) -> None:
        ok, info = validate_bot_token("xoxp-123-wrong-type")
        assert ok is False
        assert "Expected bot token" in info["error"]

    def test_valid_bot_token(self) -> None:
        fake = _make_fake_auth_test(True, {
            "user_id": "U456",
            "bot_id": "B789",
            "user": "lobster-bot",
            "team": "MyWorkspace",
        })
        ok, info = validate_bot_token("xoxb-valid-token", _auth_test_fn=fake)
        assert ok is True
        assert info["bot_id"] == "B789"
