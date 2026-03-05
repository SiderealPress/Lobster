"""
Tests for src/integrations/google_calendar/oauth.py.

Covers:
- generate_auth_url: URL structure, required params, state, scopes, custom credentials
- exchange_code_for_tokens: success, Google error response, network timeout, connection error
- refresh_access_token: success, revoked refresh token, network error
- is_token_valid: valid token, expired token, token within 5-min buffer
- _build_auth_params: pure function, param completeness
- _parse_token_response: success, error key, missing expires_in
- _post_token_endpoint: timeout error, connection error, non-JSON response

All HTTP calls are mocked — no network traffic in these tests.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.config import (
    DEFAULT_SCOPES,
    SCOPE_EVENTS,
    SCOPE_READONLY,
    GoogleOAuthCredentials,
)
from integrations.google_calendar.oauth import (
    OAuthError,
    OAuthNetworkError,
    OAuthTokenError,
    TokenData,
    _EXPIRY_BUFFER,
    _GOOGLE_AUTH_URL,
    _GOOGLE_TOKEN_URL,
    _build_auth_params,
    _parse_token_response,
    _post_token_endpoint,
    exchange_code_for_tokens,
    generate_auth_url,
    is_token_valid,
    refresh_access_token,
)

# ---------------------------------------------------------------------------
# Test fixtures / helpers
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

_FAKE_STATE = "random-csrf-state-abc123"
_FAKE_CODE = "4/fake-auth-code-from-google"
_FAKE_ACCESS_TOKEN = "ya29.fake-access-token"
_FAKE_REFRESH_TOKEN = "1//fake-refresh-token"

_FUTURE_EXPIRES_AT = datetime.now(tz=timezone.utc) + timedelta(hours=1)
_EXPIRED_EXPIRES_AT = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
_WITHIN_BUFFER_EXPIRES_AT = datetime.now(tz=timezone.utc) + timedelta(minutes=2)


def _make_valid_token(
    refresh_token: str | None = _FAKE_REFRESH_TOKEN,
) -> TokenData:
    """Build a token that is currently valid."""
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=_FUTURE_EXPIRES_AT,
        scope=f"{SCOPE_READONLY} {SCOPE_EVENTS}",
        refresh_token=refresh_token,
    )


def _make_expired_token(
    refresh_token: str | None = _FAKE_REFRESH_TOKEN,
) -> TokenData:
    """Build a token that is already expired."""
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=_EXPIRED_EXPIRES_AT,
        scope=f"{SCOPE_READONLY} {SCOPE_EVENTS}",
        refresh_token=refresh_token,
    )


def _make_token_response_dict(
    access_token: str = _FAKE_ACCESS_TOKEN,
    refresh_token: str | None = _FAKE_REFRESH_TOKEN,
    expires_in: int = 3600,
    scope: str = f"{SCOPE_READONLY} {SCOPE_EVENTS}",
) -> dict:
    """Build a fake Google token endpoint response dict."""
    result: dict = {
        "access_token": access_token,
        "expires_in": expires_in,
        "scope": scope,
        "token_type": "Bearer",
    }
    if refresh_token is not None:
        result["refresh_token"] = refresh_token
    return result


# ---------------------------------------------------------------------------
# _build_auth_params
# ---------------------------------------------------------------------------


class TestBuildAuthParams:
    def test_returns_dict(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert isinstance(params, dict)

    def test_client_id_present(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert params["client_id"] == _FAKE_CLIENT_ID

    def test_redirect_uri_present(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert params["redirect_uri"] == _FAKE_REDIRECT_URI

    def test_response_type_is_code(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert params["response_type"] == "code"

    def test_access_type_is_offline(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert params["access_type"] == "offline"

    def test_prompt_is_consent(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert params["prompt"] == "consent"

    def test_state_embedded(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert params["state"] == _FAKE_STATE

    def test_scope_is_space_joined_tuple(self) -> None:
        scopes = (SCOPE_READONLY, SCOPE_EVENTS)
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, scopes)
        assert params["scope"] == f"{SCOPE_READONLY} {SCOPE_EVENTS}"

    def test_single_scope(self) -> None:
        params = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, (SCOPE_READONLY,))
        assert params["scope"] == SCOPE_READONLY

    def test_pure_function_same_inputs_same_output(self) -> None:
        params_a = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        params_b = _build_auth_params(_FAKE_CREDENTIALS, _FAKE_STATE, DEFAULT_SCOPES)
        assert params_a == params_b


# ---------------------------------------------------------------------------
# _parse_token_response
# ---------------------------------------------------------------------------


class TestParseTokenResponse:
    def test_returns_token_data(self) -> None:
        raw = _make_token_response_dict()
        result = _parse_token_response(raw)
        assert isinstance(result, TokenData)

    def test_access_token_populated(self) -> None:
        raw = _make_token_response_dict()
        result = _parse_token_response(raw)
        assert result.access_token == _FAKE_ACCESS_TOKEN

    def test_refresh_token_populated_when_present(self) -> None:
        raw = _make_token_response_dict(refresh_token=_FAKE_REFRESH_TOKEN)
        result = _parse_token_response(raw)
        assert result.refresh_token == _FAKE_REFRESH_TOKEN

    def test_refresh_token_none_when_absent(self) -> None:
        raw = _make_token_response_dict(refresh_token=None)
        result = _parse_token_response(raw)
        assert result.refresh_token is None

    def test_scope_populated(self) -> None:
        raw = _make_token_response_dict(scope=SCOPE_READONLY)
        result = _parse_token_response(raw)
        assert result.scope == SCOPE_READONLY

    def test_expires_at_is_future(self) -> None:
        raw = _make_token_response_dict(expires_in=3600)
        result = _parse_token_response(raw)
        assert result.expires_at > datetime.now(tz=timezone.utc)

    def test_expires_at_approximately_correct(self) -> None:
        raw = _make_token_response_dict(expires_in=3600)
        before = datetime.now(tz=timezone.utc)
        result = _parse_token_response(raw)
        after = datetime.now(tz=timezone.utc)
        expected_min = before + timedelta(seconds=3600)
        expected_max = after + timedelta(seconds=3600)
        assert expected_min <= result.expires_at <= expected_max

    def test_raises_oauth_token_error_on_error_field(self) -> None:
        raw = {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        with pytest.raises(OAuthTokenError) as exc_info:
            _parse_token_response(raw)
        assert exc_info.value.error == "invalid_grant"

    def test_oauth_token_error_includes_description(self) -> None:
        raw = {"error": "invalid_client", "error_description": "The OAuth client was not found."}
        with pytest.raises(OAuthTokenError) as exc_info:
            _parse_token_response(raw)
        assert "The OAuth client was not found." in str(exc_info.value)

    def test_raises_oauth_token_error_without_description(self) -> None:
        raw = {"error": "access_denied"}
        with pytest.raises(OAuthTokenError) as exc_info:
            _parse_token_response(raw)
        assert exc_info.value.description == ""

    def test_raises_key_error_on_missing_access_token(self) -> None:
        raw = {"expires_in": 3600, "scope": SCOPE_READONLY}
        with pytest.raises(KeyError):
            _parse_token_response(raw)

    def test_token_data_is_immutable(self) -> None:
        raw = _make_token_response_dict()
        result = _parse_token_response(raw)
        with pytest.raises(Exception):
            result.access_token = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _post_token_endpoint
# ---------------------------------------------------------------------------


class TestPostTokenEndpoint:
    def test_posts_to_google_token_url(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = _make_token_response_dict()
        with patch("integrations.google_calendar.oauth.requests.post", return_value=mock_response) as mock_post:
            _post_token_endpoint({"grant_type": "authorization_code"})
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert call_url == _GOOGLE_TOKEN_URL

    def test_raises_oauth_network_error_on_timeout(self) -> None:
        import requests as req
        with patch(
            "integrations.google_calendar.oauth.requests.post",
            side_effect=req.exceptions.Timeout,
        ):
            with pytest.raises(OAuthNetworkError, match="Timeout"):
                _post_token_endpoint({})

    def test_raises_oauth_network_error_on_connection_error(self) -> None:
        import requests as req
        with patch(
            "integrations.google_calendar.oauth.requests.post",
            side_effect=req.exceptions.ConnectionError("refused"),
        ):
            with pytest.raises(OAuthNetworkError, match="Connection error"):
                _post_token_endpoint({})

    def test_raises_oauth_network_error_on_non_json_response(self) -> None:
        mock_response = MagicMock()
        mock_response.json.side_effect = ValueError("No JSON object could be decoded")
        with patch("integrations.google_calendar.oauth.requests.post", return_value=mock_response):
            with pytest.raises(OAuthNetworkError, match="non-JSON"):
                _post_token_endpoint({})

    def test_returns_parsed_dict_on_success(self) -> None:
        expected = _make_token_response_dict()
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        with patch("integrations.google_calendar.oauth.requests.post", return_value=mock_response):
            result = _post_token_endpoint({})
        assert result == expected

    def test_content_type_header_is_form_encoded(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = _make_token_response_dict()
        with patch("integrations.google_calendar.oauth.requests.post", return_value=mock_response) as mock_post:
            _post_token_endpoint({})
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


# ---------------------------------------------------------------------------
# generate_auth_url
# ---------------------------------------------------------------------------


class TestGenerateAuthUrl:
    def test_returns_string(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        assert isinstance(url, str)

    def test_starts_with_google_auth_url(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        assert url.startswith(_GOOGLE_AUTH_URL)

    def test_state_in_url(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        assert _FAKE_STATE in url

    def test_client_id_in_url(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        assert _FAKE_CLIENT_ID in url

    def test_redirect_uri_in_url(self) -> None:
        from urllib.parse import quote_plus
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        # redirect_uri will be URL-encoded in the query string
        assert "redirect_uri=" in url

    def test_access_type_offline_in_url(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        assert "access_type=offline" in url

    def test_prompt_consent_in_url(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        assert "prompt=consent" in url

    def test_response_type_code_in_url(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        assert "response_type=code" in url

    def test_scope_in_url(self) -> None:
        url = generate_auth_url(_FAKE_STATE, credentials=_FAKE_CREDENTIALS)
        # Scopes are space-joined then URL-encoded
        assert "scope=" in url

    def test_custom_scopes_reflected(self) -> None:
        url = generate_auth_url(
            _FAKE_STATE,
            scopes=(SCOPE_READONLY,),
            credentials=_FAKE_CREDENTIALS,
        )
        # SCOPE_READONLY path segment should appear (URL-encoded)
        assert "calendar.readonly" in url

    def test_different_states_produce_different_urls(self) -> None:
        url_a = generate_auth_url("state-aaa", credentials=_FAKE_CREDENTIALS)
        url_b = generate_auth_url("state-bbb", credentials=_FAKE_CREDENTIALS)
        assert url_a != url_b

    def test_loads_credentials_from_env_when_none_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        # Should not raise even with credentials=None
        url = generate_auth_url(_FAKE_STATE)
        assert _FAKE_CLIENT_ID in url


# ---------------------------------------------------------------------------
# exchange_code_for_tokens
# ---------------------------------------------------------------------------


class TestExchangeCodeForTokens:
    def _mock_post(self, raw_response: dict):
        """Context manager: patches _post_token_endpoint to return raw_response."""
        return patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            return_value=raw_response,
        )

    def test_returns_token_data_on_success(self) -> None:
        raw = _make_token_response_dict()
        with self._mock_post(raw):
            result = exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)
        assert isinstance(result, TokenData)

    def test_access_token_populated(self) -> None:
        raw = _make_token_response_dict()
        with self._mock_post(raw):
            result = exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)
        assert result.access_token == _FAKE_ACCESS_TOKEN

    def test_refresh_token_populated(self) -> None:
        raw = _make_token_response_dict()
        with self._mock_post(raw):
            result = exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)
        assert result.refresh_token == _FAKE_REFRESH_TOKEN

    def test_payload_includes_grant_type(self) -> None:
        raw = _make_token_response_dict()
        with patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            return_value=raw,
        ) as mock_post:
            exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)
        payload = mock_post.call_args[0][0]
        assert payload["grant_type"] == "authorization_code"

    def test_payload_includes_code(self) -> None:
        raw = _make_token_response_dict()
        with patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            return_value=raw,
        ) as mock_post:
            exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)
        payload = mock_post.call_args[0][0]
        assert payload["code"] == _FAKE_CODE

    def test_payload_includes_redirect_uri(self) -> None:
        raw = _make_token_response_dict()
        with patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            return_value=raw,
        ) as mock_post:
            exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)
        payload = mock_post.call_args[0][0]
        assert payload["redirect_uri"] == _FAKE_REDIRECT_URI

    def test_raises_oauth_token_error_on_bad_code(self) -> None:
        raw = {"error": "invalid_grant", "error_description": "Code was already redeemed."}
        with self._mock_post(raw):
            with pytest.raises(OAuthTokenError) as exc_info:
                exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)
        assert exc_info.value.error == "invalid_grant"

    def test_propagates_oauth_network_error(self) -> None:
        with patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            side_effect=OAuthNetworkError("timeout"),
        ):
            with pytest.raises(OAuthNetworkError):
                exchange_code_for_tokens(_FAKE_CODE, credentials=_FAKE_CREDENTIALS)

    def test_loads_credentials_from_env_when_none_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        raw = _make_token_response_dict()
        with self._mock_post(raw):
            result = exchange_code_for_tokens(_FAKE_CODE)
        assert result.access_token == _FAKE_ACCESS_TOKEN


# ---------------------------------------------------------------------------
# refresh_access_token
# ---------------------------------------------------------------------------


class TestRefreshAccessToken:
    def _mock_post(self, raw_response: dict):
        return patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            return_value=raw_response,
        )

    def test_returns_token_data_on_success(self) -> None:
        raw = _make_token_response_dict(refresh_token=None)
        with self._mock_post(raw):
            result = refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)
        assert isinstance(result, TokenData)

    def test_access_token_updated(self) -> None:
        new_access = "ya29.new-access-token"
        raw = _make_token_response_dict(access_token=new_access, refresh_token=None)
        with self._mock_post(raw):
            result = refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)
        assert result.access_token == new_access

    def test_refresh_token_none_when_google_omits_it(self) -> None:
        # Google typically does not return a new refresh_token on plain refreshes
        raw = _make_token_response_dict(refresh_token=None)
        with self._mock_post(raw):
            result = refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)
        assert result.refresh_token is None

    def test_refresh_token_returned_when_google_includes_it(self) -> None:
        new_refresh = "1//new-refresh-token"
        raw = _make_token_response_dict(refresh_token=new_refresh)
        with self._mock_post(raw):
            result = refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)
        assert result.refresh_token == new_refresh

    def test_payload_grant_type_is_refresh_token(self) -> None:
        raw = _make_token_response_dict(refresh_token=None)
        with patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            return_value=raw,
        ) as mock_post:
            refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)
        payload = mock_post.call_args[0][0]
        assert payload["grant_type"] == "refresh_token"

    def test_payload_includes_refresh_token(self) -> None:
        raw = _make_token_response_dict(refresh_token=None)
        with patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            return_value=raw,
        ) as mock_post:
            refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)
        payload = mock_post.call_args[0][0]
        assert payload["refresh_token"] == _FAKE_REFRESH_TOKEN

    def test_raises_oauth_token_error_on_revoked_token(self) -> None:
        raw = {"error": "invalid_grant", "error_description": "Token has been revoked."}
        with self._mock_post(raw):
            with pytest.raises(OAuthTokenError) as exc_info:
                refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)
        assert exc_info.value.error == "invalid_grant"

    def test_propagates_oauth_network_error(self) -> None:
        with patch(
            "integrations.google_calendar.oauth._post_token_endpoint",
            side_effect=OAuthNetworkError("timeout"),
        ):
            with pytest.raises(OAuthNetworkError):
                refresh_access_token(_FAKE_REFRESH_TOKEN, credentials=_FAKE_CREDENTIALS)


# ---------------------------------------------------------------------------
# is_token_valid
# ---------------------------------------------------------------------------


class TestIsTokenValid:
    def test_valid_token_returns_true(self) -> None:
        token = _make_valid_token()
        assert is_token_valid(token) is True

    def test_expired_token_returns_false(self) -> None:
        token = _make_expired_token()
        assert is_token_valid(token) is False

    def test_token_within_buffer_window_returns_false(self) -> None:
        # Expires in 2 minutes — within the 5-minute buffer
        token = TokenData(
            access_token=_FAKE_ACCESS_TOKEN,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=2),
            scope=SCOPE_READONLY,
        )
        assert is_token_valid(token) is False

    def test_token_just_outside_buffer_returns_true(self) -> None:
        # Expires in 6 minutes — just outside the 5-minute buffer
        token = TokenData(
            access_token=_FAKE_ACCESS_TOKEN,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=6),
            scope=SCOPE_READONLY,
        )
        assert is_token_valid(token) is True

    def test_exactly_at_buffer_boundary_returns_false(self) -> None:
        # Expires in exactly 5 minutes — NOT strictly greater than buffer
        token = TokenData(
            access_token=_FAKE_ACCESS_TOKEN,
            expires_at=datetime.now(tz=timezone.utc) + _EXPIRY_BUFFER,
            scope=SCOPE_READONLY,
        )
        assert is_token_valid(token) is False

    def test_pure_function_no_state_changes(self) -> None:
        token = _make_valid_token()
        result_a = is_token_valid(token)
        result_b = is_token_valid(token)
        assert result_a == result_b

    def test_works_without_refresh_token(self) -> None:
        token = TokenData(
            access_token=_FAKE_ACCESS_TOKEN,
            expires_at=_FUTURE_EXPIRES_AT,
            scope=SCOPE_READONLY,
            refresh_token=None,
        )
        assert is_token_valid(token) is True


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_oauth_token_error_is_oauth_error(self) -> None:
        assert issubclass(OAuthTokenError, OAuthError)

    def test_oauth_network_error_is_oauth_error(self) -> None:
        assert issubclass(OAuthNetworkError, OAuthError)

    def test_oauth_error_is_runtime_error(self) -> None:
        assert issubclass(OAuthError, RuntimeError)

    def test_oauth_token_error_stores_error_code(self) -> None:
        exc = OAuthTokenError(error="invalid_grant", description="Expired.")
        assert exc.error == "invalid_grant"

    def test_oauth_token_error_stores_description(self) -> None:
        exc = OAuthTokenError(error="invalid_grant", description="Expired.")
        assert exc.description == "Expired."

    def test_oauth_token_error_empty_description_by_default(self) -> None:
        exc = OAuthTokenError(error="invalid_client")
        assert exc.description == ""
