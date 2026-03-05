"""
Tests for src/integrations/google_calendar/token_store.py.

Covers:
- _token_to_dict / _dict_to_token: round-trip serialisation, field types
- _token_path: safe filenames, directory traversal prevention, empty user_id
- save_token: creates file, mode 600, overwrites existing, handles bad user_id
- load_token: returns TokenData, returns None on missing file, None on corrupt JSON
- is_token_valid: delegates to oauth.is_token_valid (tested fully in oauth tests)
- get_valid_token: returns valid token, refreshes expired token, saves refreshed
  token, carries forward refresh_token when Google omits it, returns None on
  missing token, returns None when refresh fails, returns None when no refresh_token
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.config import (
    SCOPE_EVENTS,
    SCOPE_READONLY,
    GoogleOAuthCredentials,
    DEFAULT_SCOPES,
)
from integrations.google_calendar.oauth import (
    OAuthNetworkError,
    OAuthTokenError,
    TokenData,
)
from integrations.google_calendar.token_store import (
    _dict_to_token,
    _token_path,
    _token_to_dict,
    get_valid_token,
    load_token,
    save_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
_FAKE_CLIENT_SECRET = "fake-client-secret"
_FAKE_REDIRECT_URI = "https://myownlobster.ai/auth/google/callback"
_FAKE_CREDENTIALS = GoogleOAuthCredentials(
    client_id=_FAKE_CLIENT_ID,
    client_secret=_FAKE_CLIENT_SECRET,
    scopes=DEFAULT_SCOPES,
    redirect_uri=_FAKE_REDIRECT_URI,
)

_FAKE_ACCESS_TOKEN = "ya29.fake-access-token"
_FAKE_REFRESH_TOKEN = "1//fake-refresh-token"
_FAKE_SCOPE = f"{SCOPE_READONLY} {SCOPE_EVENTS}"

_FUTURE_EXPIRES = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_EXPIRED_EXPIRES = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_valid_token(refresh_token: str | None = _FAKE_REFRESH_TOKEN) -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=_FUTURE_EXPIRES,
        scope=_FAKE_SCOPE,
        refresh_token=refresh_token,
    )


def _make_expired_token(refresh_token: str | None = _FAKE_REFRESH_TOKEN) -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=_EXPIRED_EXPIRES,
        scope=_FAKE_SCOPE,
        refresh_token=refresh_token,
    )


# ---------------------------------------------------------------------------
# _token_to_dict
# ---------------------------------------------------------------------------


class TestTokenToDict:
    def test_returns_dict(self) -> None:
        token = _make_valid_token()
        result = _token_to_dict(token)
        assert isinstance(result, dict)

    def test_access_token_present(self) -> None:
        token = _make_valid_token()
        result = _token_to_dict(token)
        assert result["access_token"] == _FAKE_ACCESS_TOKEN

    def test_refresh_token_present(self) -> None:
        token = _make_valid_token()
        result = _token_to_dict(token)
        assert result["refresh_token"] == _FAKE_REFRESH_TOKEN

    def test_refresh_token_none_when_absent(self) -> None:
        token = _make_valid_token(refresh_token=None)
        result = _token_to_dict(token)
        assert result["refresh_token"] is None

    def test_scope_present(self) -> None:
        token = _make_valid_token()
        result = _token_to_dict(token)
        assert result["scope"] == _FAKE_SCOPE

    def test_expires_at_is_iso_string(self) -> None:
        token = _make_valid_token()
        result = _token_to_dict(token)
        # Should be parseable back to datetime
        parsed = datetime.fromisoformat(result["expires_at"])
        assert isinstance(parsed, datetime)

    def test_expires_at_preserves_value(self) -> None:
        token = _make_valid_token()
        result = _token_to_dict(token)
        parsed = datetime.fromisoformat(result["expires_at"])
        # Normalise to UTC for comparison
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        assert parsed == token.expires_at


# ---------------------------------------------------------------------------
# _dict_to_token
# ---------------------------------------------------------------------------


class TestDictToToken:
    def _make_dict(
        self,
        access_token: str = _FAKE_ACCESS_TOKEN,
        refresh_token: str | None = _FAKE_REFRESH_TOKEN,
        expires_at: str = _FUTURE_EXPIRES.isoformat(),
        scope: str = _FAKE_SCOPE,
    ) -> dict:
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "scope": scope,
        }

    def test_returns_token_data(self) -> None:
        data = self._make_dict()
        result = _dict_to_token(data)
        assert isinstance(result, TokenData)

    def test_access_token_populated(self) -> None:
        data = self._make_dict()
        result = _dict_to_token(data)
        assert result.access_token == _FAKE_ACCESS_TOKEN

    def test_refresh_token_populated(self) -> None:
        data = self._make_dict()
        result = _dict_to_token(data)
        assert result.refresh_token == _FAKE_REFRESH_TOKEN

    def test_refresh_token_none(self) -> None:
        data = self._make_dict(refresh_token=None)
        result = _dict_to_token(data)
        assert result.refresh_token is None

    def test_scope_populated(self) -> None:
        data = self._make_dict()
        result = _dict_to_token(data)
        assert result.scope == _FAKE_SCOPE

    def test_expires_at_is_timezone_aware(self) -> None:
        data = self._make_dict()
        result = _dict_to_token(data)
        assert result.expires_at.tzinfo is not None

    def test_naive_expires_at_gets_utc_tzinfo(self) -> None:
        # Legacy files may store naive datetimes
        naive_iso = "2099-01-01T00:00:00"
        data = self._make_dict(expires_at=naive_iso)
        result = _dict_to_token(data)
        assert result.expires_at.tzinfo == timezone.utc

    def test_raises_key_error_on_missing_access_token(self) -> None:
        data = {"expires_at": _FUTURE_EXPIRES.isoformat(), "scope": _FAKE_SCOPE}
        with pytest.raises(KeyError):
            _dict_to_token(data)

    def test_raises_value_error_on_invalid_expires_at(self) -> None:
        data = self._make_dict(expires_at="not-a-datetime")
        with pytest.raises(ValueError):
            _dict_to_token(data)


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------


class TestSerializationRoundTrip:
    def test_round_trip_preserves_all_fields(self) -> None:
        original = _make_valid_token()
        restored = _dict_to_token(_token_to_dict(original))
        assert restored.access_token == original.access_token
        assert restored.refresh_token == original.refresh_token
        assert restored.scope == original.scope
        # Compare at second precision (ISO format strips microseconds)
        assert abs((restored.expires_at - original.expires_at).total_seconds()) < 1

    def test_round_trip_with_none_refresh_token(self) -> None:
        original = _make_valid_token(refresh_token=None)
        restored = _dict_to_token(_token_to_dict(original))
        assert restored.refresh_token is None


# ---------------------------------------------------------------------------
# _token_path
# ---------------------------------------------------------------------------


class TestTokenPath:
    def test_returns_path_object(self, tmp_path: Path) -> None:
        result = _token_path("user123", tmp_path)
        assert isinstance(result, Path)

    def test_filename_is_user_id_dot_json(self, tmp_path: Path) -> None:
        result = _token_path("user123", tmp_path)
        assert result.name == "user123.json"

    def test_parent_is_token_dir(self, tmp_path: Path) -> None:
        result = _token_path("user123", tmp_path)
        assert result.parent == tmp_path

    def test_sanitises_alphanumeric_with_hyphens_and_underscores(self, tmp_path: Path) -> None:
        result = _token_path("user-123_abc", tmp_path)
        assert result.name == "user-123_abc.json"

    def test_strips_path_separator_from_user_id(self, tmp_path: Path) -> None:
        # Prevent directory traversal
        result = _token_path("../evil", tmp_path)
        # Dots and slashes should be stripped, leaving only alphanumeric
        assert "/" not in result.name
        assert ".." not in result.name

    def test_strips_dots_from_user_id(self, tmp_path: Path) -> None:
        result = _token_path("user.name", tmp_path)
        # Dots should be stripped (not in the allowed character set)
        assert "." not in result.stem

    def test_raises_value_error_on_empty_user_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            _token_path("", tmp_path)

    def test_raises_value_error_on_all_special_chars(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            _token_path("../.", tmp_path)

    def test_telegram_chat_id_is_valid(self, tmp_path: Path) -> None:
        # Telegram chat IDs are integers; when cast to str they pass cleanly
        result = _token_path("6645894734", tmp_path)
        assert result.name == "6645894734.json"


# ---------------------------------------------------------------------------
# save_token
# ---------------------------------------------------------------------------


class TestSaveToken:
    def test_creates_token_file(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        assert (tmp_path / "user1.json").exists()

    def test_token_file_is_valid_json(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        content = (tmp_path / "user1.json").read_text()
        data = json.loads(content)
        assert "access_token" in data

    def test_token_file_permissions_are_600(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        path = tmp_path / "user1.json"
        file_stat = path.stat()
        # Check only the permission bits (mask with 0o777)
        mode = stat.S_IMODE(file_stat.st_mode)
        assert mode == 0o600

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        token_a = _make_valid_token()
        save_token("user1", token_a, token_dir=tmp_path)
        # Save a different token — access_token differs
        token_b = TokenData(
            access_token="ya29.different-token",
            expires_at=_FUTURE_EXPIRES,
            scope=_FAKE_SCOPE,
            refresh_token=_FAKE_REFRESH_TOKEN,
        )
        save_token("user1", token_b, token_dir=tmp_path)
        data = json.loads((tmp_path / "user1.json").read_text())
        assert data["access_token"] == "ya29.different-token"

    def test_creates_token_dir_if_absent(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        token = _make_valid_token()
        save_token("user1", token, token_dir=nested)
        assert (nested / "user1.json").exists()

    def test_raises_value_error_on_bad_user_id(self, tmp_path: Path) -> None:
        # A user_id containing only special characters sanitises to an empty
        # filename, which triggers ValueError in _token_path.
        token = _make_valid_token()
        with pytest.raises(ValueError):
            save_token("../.", token, token_dir=tmp_path)

    def test_saved_token_can_be_round_tripped(self, tmp_path: Path) -> None:
        original = _make_valid_token()
        save_token("user1", original, token_dir=tmp_path)
        data = json.loads((tmp_path / "user1.json").read_text())
        restored = _dict_to_token(data)
        assert restored.access_token == original.access_token
        assert restored.refresh_token == original.refresh_token


# ---------------------------------------------------------------------------
# load_token
# ---------------------------------------------------------------------------


class TestLoadToken:
    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        result = load_token("nonexistent", token_dir=tmp_path)
        assert result is None

    def test_returns_token_data_after_save(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        result = load_token("user1", token_dir=tmp_path)
        assert isinstance(result, TokenData)

    def test_loaded_access_token_matches(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        result = load_token("user1", token_dir=tmp_path)
        assert result is not None
        assert result.access_token == _FAKE_ACCESS_TOKEN

    def test_loaded_refresh_token_matches(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        result = load_token("user1", token_dir=tmp_path)
        assert result is not None
        assert result.refresh_token == _FAKE_REFRESH_TOKEN

    def test_loaded_token_with_none_refresh_token(self, tmp_path: Path) -> None:
        token = _make_valid_token(refresh_token=None)
        save_token("user1", token, token_dir=tmp_path)
        result = load_token("user1", token_dir=tmp_path)
        assert result is not None
        assert result.refresh_token is None

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        path = tmp_path / "user1.json"
        path.write_text("{ not valid json }")
        result = load_token("user1", token_dir=tmp_path)
        assert result is None

    def test_returns_none_on_missing_required_field(self, tmp_path: Path) -> None:
        path = tmp_path / "user1.json"
        path.write_text(json.dumps({"expires_at": _FUTURE_EXPIRES.isoformat()}))
        result = load_token("user1", token_dir=tmp_path)
        assert result is None

    def test_returns_none_on_invalid_expires_at(self, tmp_path: Path) -> None:
        path = tmp_path / "user1.json"
        path.write_text(json.dumps({
            "access_token": _FAKE_ACCESS_TOKEN,
            "expires_at": "not-a-date",
            "scope": _FAKE_SCOPE,
        }))
        result = load_token("user1", token_dir=tmp_path)
        assert result is None

    def test_loaded_expires_at_is_timezone_aware(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        result = load_token("user1", token_dir=tmp_path)
        assert result is not None
        assert result.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------


class TestGetValidToken:
    def test_returns_none_when_no_token_file(self, tmp_path: Path) -> None:
        result = get_valid_token("user1", token_dir=tmp_path)
        assert result is None

    def test_returns_valid_token_without_refresh(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, token_dir=tmp_path)
        result = get_valid_token("user1", token_dir=tmp_path)
        assert result is not None
        assert result.access_token == _FAKE_ACCESS_TOKEN

    def test_refreshes_expired_token(self, tmp_path: Path) -> None:
        expired = _make_expired_token()
        save_token("user1", expired, token_dir=tmp_path)
        new_access = "ya29.refreshed-access-token"
        refreshed_token = TokenData(
            access_token=new_access,
            expires_at=_FUTURE_EXPIRES,
            scope=_FAKE_SCOPE,
            refresh_token=None,  # Google often omits this
        )
        with patch(
            "integrations.google_calendar.token_store.refresh_access_token",
            return_value=refreshed_token,
        ):
            result = get_valid_token(
                "user1", token_dir=tmp_path, credentials=_FAKE_CREDENTIALS
            )
        assert result is not None
        assert result.access_token == new_access

    def test_saves_refreshed_token_to_disk(self, tmp_path: Path) -> None:
        expired = _make_expired_token()
        save_token("user1", expired, token_dir=tmp_path)
        new_access = "ya29.refreshed-token"
        refreshed_token = TokenData(
            access_token=new_access,
            expires_at=_FUTURE_EXPIRES,
            scope=_FAKE_SCOPE,
            refresh_token=None,
        )
        with patch(
            "integrations.google_calendar.token_store.refresh_access_token",
            return_value=refreshed_token,
        ):
            get_valid_token("user1", token_dir=tmp_path, credentials=_FAKE_CREDENTIALS)
        # Now load directly from disk to confirm it was persisted
        stored = load_token("user1", token_dir=tmp_path)
        assert stored is not None
        assert stored.access_token == new_access

    def test_carries_forward_refresh_token_when_google_omits_it(self, tmp_path: Path) -> None:
        original_refresh = "1//original-refresh-token"
        expired = _make_expired_token(refresh_token=original_refresh)
        save_token("user1", expired, token_dir=tmp_path)
        # Refreshed response has no refresh_token
        refreshed_token = TokenData(
            access_token="ya29.new-access",
            expires_at=_FUTURE_EXPIRES,
            scope=_FAKE_SCOPE,
            refresh_token=None,
        )
        with patch(
            "integrations.google_calendar.token_store.refresh_access_token",
            return_value=refreshed_token,
        ):
            result = get_valid_token(
                "user1", token_dir=tmp_path, credentials=_FAKE_CREDENTIALS
            )
        assert result is not None
        assert result.refresh_token == original_refresh

    def test_returns_none_when_refresh_fails_with_oauth_error(self, tmp_path: Path) -> None:
        expired = _make_expired_token()
        save_token("user1", expired, token_dir=tmp_path)
        with patch(
            "integrations.google_calendar.token_store.refresh_access_token",
            side_effect=OAuthTokenError("invalid_grant", "Token revoked."),
        ):
            result = get_valid_token(
                "user1", token_dir=tmp_path, credentials=_FAKE_CREDENTIALS
            )
        assert result is None

    def test_returns_none_when_refresh_fails_with_network_error(self, tmp_path: Path) -> None:
        expired = _make_expired_token()
        save_token("user1", expired, token_dir=tmp_path)
        with patch(
            "integrations.google_calendar.token_store.refresh_access_token",
            side_effect=OAuthNetworkError("timeout"),
        ):
            result = get_valid_token(
                "user1", token_dir=tmp_path, credentials=_FAKE_CREDENTIALS
            )
        assert result is None

    def test_returns_none_when_token_expired_and_no_refresh_token(
        self, tmp_path: Path
    ) -> None:
        expired = _make_expired_token(refresh_token=None)
        save_token("user1", expired, token_dir=tmp_path)
        result = get_valid_token("user1", token_dir=tmp_path)
        assert result is None

    def test_does_not_call_refresh_for_valid_token(self, tmp_path: Path) -> None:
        valid = _make_valid_token()
        save_token("user1", valid, token_dir=tmp_path)
        with patch(
            "integrations.google_calendar.token_store.refresh_access_token",
        ) as mock_refresh:
            get_valid_token("user1", token_dir=tmp_path)
        mock_refresh.assert_not_called()

    def test_passes_credentials_to_refresh(self, tmp_path: Path) -> None:
        expired = _make_expired_token()
        save_token("user1", expired, token_dir=tmp_path)
        refreshed = TokenData(
            access_token="ya29.new",
            expires_at=_FUTURE_EXPIRES,
            scope=_FAKE_SCOPE,
        )
        with patch(
            "integrations.google_calendar.token_store.refresh_access_token",
            return_value=refreshed,
        ) as mock_refresh:
            get_valid_token(
                "user1", token_dir=tmp_path, credentials=_FAKE_CREDENTIALS
            )
        _, kwargs = mock_refresh.call_args
        assert kwargs.get("credentials") == _FAKE_CREDENTIALS
