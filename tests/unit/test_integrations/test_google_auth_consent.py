"""
Tests for src/integrations/google_auth/consent.py.

Covers:
- generate_consent_link: returns URL for "calendar" and "gmail" scopes
- generate_consent_link: raises ValueError for unknown scope
- generate_consent_link: raises RuntimeError when LOBSTER_INSTANCE_URL missing
- generate_consent_link: raises RuntimeError when LOBSTER_INTERNAL_SECRET missing
- generate_consent_link: raises RuntimeError when both env vars are missing
- generate_consent_link: raises RuntimeError on HTTP error response
- generate_consent_link: raises RuntimeError on network error
- generate_consent_link: raises RuntimeError when response JSON missing "url" key
- generate_consent_link: raises RuntimeError when response "url" is empty string
- _read_env: returns stripped values from environment
- _post_generate_consent_link: posts correct JSON payload
- Secrets do not appear in log output
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_auth.consent import (
    _post_generate_consent_link,
    _read_env,
    generate_consent_link,
)

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

_FAKE_INSTANCE_URL = "https://vps.example.com"
_FAKE_SECRET = "test-internal-secret-abc123"
_FAKE_CALENDAR_URL = "https://myownlobster.ai/connect/calendar?token=test-uuid"
_FAKE_GMAIL_URL = "https://myownlobster.ai/connect/gmail?token=test-uuid"


def _mock_response(url: str, status_code: int = 200) -> MagicMock:
    """Build a requests.Response mock that returns the given URL."""
    resp = MagicMock()
    resp.ok = status_code < 400
    resp.status_code = status_code
    resp.json.return_value = {"url": url}
    resp.text = f'{{"url": "{url}"}}'
    return resp


def _env(instance_url: str = _FAKE_INSTANCE_URL, secret: str = _FAKE_SECRET) -> dict:
    return {
        "LOBSTER_INSTANCE_URL": instance_url,
        "LOBSTER_INTERNAL_SECRET": secret,
    }


# ---------------------------------------------------------------------------
# generate_consent_link — happy path
# ---------------------------------------------------------------------------


class TestGenerateConsentLinkHappyPath:
    def test_calendar_scope_returns_url(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response(_FAKE_CALENDAR_URL),
        ) as mock_post:
            result = generate_consent_link("calendar")

        assert result == _FAKE_CALENDAR_URL
        mock_post.assert_called_once()

    def test_gmail_scope_returns_url(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response(_FAKE_GMAIL_URL),
        ) as mock_post:
            result = generate_consent_link("gmail")

        assert result == _FAKE_GMAIL_URL
        mock_post.assert_called_once()

    def test_posts_correct_json_payload(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response(_FAKE_CALENDAR_URL),
        ) as mock_post:
            generate_consent_link("calendar")

        call_kwargs = mock_post.call_args
        # Payload is passed as keyword argument "json"
        payload = call_kwargs.kwargs.get("json") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["json"]
        assert payload["scope"] == "calendar"
        assert payload["instance_url"] == _FAKE_INSTANCE_URL
        assert payload["instance_secret"] == _FAKE_SECRET

    def test_uses_myownlobster_endpoint_by_default(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response(_FAKE_CALENDAR_URL),
        ) as mock_post:
            generate_consent_link("calendar")

        called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs["url"]
        # Use the positional arg (first arg to requests.post)
        positional_url = mock_post.call_args[0][0] if mock_post.call_args[0] else mock_post.call_args.kwargs.get("url", "")
        assert "myownlobster.ai" in positional_url
        assert "generate-consent-link" in positional_url


# ---------------------------------------------------------------------------
# generate_consent_link — scope validation
# ---------------------------------------------------------------------------


class TestGenerateConsentLinkScopeValidation:
    def test_invalid_scope_raises_value_error(self):
        with patch.dict("os.environ", _env()):
            with pytest.raises(ValueError, match="Invalid scope"):
                generate_consent_link("drive")

    def test_empty_scope_raises_value_error(self):
        with patch.dict("os.environ", _env()):
            with pytest.raises(ValueError, match="Invalid scope"):
                generate_consent_link("")

    def test_scope_case_sensitive(self):
        """Scope must be lowercase exactly; 'Calendar' is rejected."""
        with patch.dict("os.environ", _env()):
            with pytest.raises(ValueError, match="Invalid scope"):
                generate_consent_link("Calendar")


# ---------------------------------------------------------------------------
# generate_consent_link — missing env vars
# ---------------------------------------------------------------------------


class TestGenerateConsentLinkMissingEnv:
    def test_missing_instance_url_raises(self):
        env = {"LOBSTER_INTERNAL_SECRET": _FAKE_SECRET}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="LOBSTER_INSTANCE_URL"):
                generate_consent_link("calendar")

    def test_missing_secret_raises(self):
        env = {"LOBSTER_INSTANCE_URL": _FAKE_INSTANCE_URL}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="LOBSTER_INTERNAL_SECRET"):
                generate_consent_link("calendar")

    def test_both_missing_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="Missing required environment variables"):
                generate_consent_link("calendar")

    def test_empty_string_instance_url_raises(self):
        env = {"LOBSTER_INSTANCE_URL": "   ", "LOBSTER_INTERNAL_SECRET": _FAKE_SECRET}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="LOBSTER_INSTANCE_URL"):
                generate_consent_link("calendar")

    def test_empty_string_secret_raises(self):
        env = {"LOBSTER_INSTANCE_URL": _FAKE_INSTANCE_URL, "LOBSTER_INTERNAL_SECRET": ""}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="LOBSTER_INTERNAL_SECRET"):
                generate_consent_link("calendar")


# ---------------------------------------------------------------------------
# generate_consent_link — HTTP failure cases
# ---------------------------------------------------------------------------


class TestGenerateConsentLinkHTTPErrors:
    def test_http_4xx_raises_runtime_error(self):
        error_resp = MagicMock()
        error_resp.ok = False
        error_resp.status_code = 403
        error_resp.text = "Forbidden"

        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=error_resp,
        ):
            with pytest.raises(RuntimeError, match="HTTP 403"):
                generate_consent_link("calendar")

    def test_http_5xx_raises_runtime_error(self):
        error_resp = MagicMock()
        error_resp.ok = False
        error_resp.status_code = 503
        error_resp.text = "Service Unavailable"

        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=error_resp,
        ):
            with pytest.raises(RuntimeError, match="HTTP 503"):
                generate_consent_link("gmail")

    def test_network_error_raises_runtime_error(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=requests.exceptions.ConnectionError("connection refused"),
        ):
            with pytest.raises(RuntimeError, match="Failed to reach"):
                generate_consent_link("calendar")

    def test_timeout_raises_runtime_error(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=requests.exceptions.Timeout("timed out"),
        ):
            with pytest.raises(RuntimeError, match="Failed to reach"):
                generate_consent_link("calendar")

    def test_missing_url_key_in_response_raises(self):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"token": "some-uuid"}  # "url" key absent
        resp.text = '{"token": "some-uuid"}'

        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=resp,
        ):
            with pytest.raises(RuntimeError, match="Unexpected response"):
                generate_consent_link("calendar")

    def test_empty_url_in_response_raises(self):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"url": ""}
        resp.text = '{"url": ""}'

        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=resp,
        ):
            with pytest.raises(RuntimeError, match="empty consent URL"):
                generate_consent_link("calendar")

    def test_invalid_json_response_raises(self):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.side_effect = ValueError("not JSON")
        resp.text = "not json"

        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=resp,
        ):
            with pytest.raises(RuntimeError, match="Unexpected response"):
                generate_consent_link("calendar")


# ---------------------------------------------------------------------------
# _read_env: unit tests for the env-reading helper
# ---------------------------------------------------------------------------


class TestReadEnv:
    def test_returns_stripped_values(self):
        env = {
            "LOBSTER_INSTANCE_URL": "  https://vps.example.com  ",
            "LOBSTER_INTERNAL_SECRET": "  mysecret  ",
        }
        with patch.dict("os.environ", env, clear=True):
            url, secret = _read_env()
        assert url == "https://vps.example.com"
        assert secret == "mysecret"

    def test_raises_listing_all_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError) as exc_info:
                _read_env()
        msg = str(exc_info.value)
        assert "LOBSTER_INSTANCE_URL" in msg
        assert "LOBSTER_INTERNAL_SECRET" in msg


# ---------------------------------------------------------------------------
# Secrets must not appear in log output
# ---------------------------------------------------------------------------


class TestNoSecretsInLogs:
    def test_secret_not_logged(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="integrations.google_auth.consent"):
            with patch.dict("os.environ", _env(secret="super-secret-value")), patch(
                "integrations.google_auth.consent.requests.post",
                return_value=_mock_response(_FAKE_CALENDAR_URL),
            ):
                generate_consent_link("calendar")

        for record in caplog.records:
            assert "super-secret-value" not in record.getMessage()

    def test_instance_url_logged_but_not_secret(self, caplog):
        """instance_url appears in logs (it's not sensitive); secret does not."""
        with caplog.at_level(logging.INFO, logger="integrations.google_auth.consent"):
            with patch.dict("os.environ", _env(secret="do-not-log-me")), patch(
                "integrations.google_auth.consent.requests.post",
                return_value=_mock_response(_FAKE_CALENDAR_URL),
            ):
                generate_consent_link("calendar")

        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert "do-not-log-me" not in all_log_text


# ---------------------------------------------------------------------------
# _post_generate_consent_link: injectable endpoint for testing
# ---------------------------------------------------------------------------


class TestPostGenerateConsentLink:
    def test_uses_custom_endpoint(self):
        with patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response("https://test.example.com/connect/calendar?token=abc"),
        ) as mock_post:
            result = _post_generate_consent_link(
                scope="calendar",
                instance_url=_FAKE_INSTANCE_URL,
                instance_secret=_FAKE_SECRET,
                endpoint="https://staging.myownlobster.ai/api/generate-consent-link",
            )

        assert result == "https://test.example.com/connect/calendar?token=abc"
        called_url = mock_post.call_args[0][0]
        assert called_url == "https://staging.myownlobster.ai/api/generate-consent-link"

    def test_passes_timeout(self):
        with patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response(_FAKE_CALENDAR_URL),
        ) as mock_post:
            _post_generate_consent_link(
                scope="calendar",
                instance_url=_FAKE_INSTANCE_URL,
                instance_secret=_FAKE_SECRET,
                timeout=42,
            )

        assert mock_post.call_args.kwargs["timeout"] == 42
