"""
Tests for src/integrations/google_calendar/config.py.

Covers:
- load_credentials() with both vars set
- load_credentials() raises GoogleCredentialError when vars are absent
- load_credentials() raises GoogleCredentialError for empty-string values
- load_credentials() with custom scopes and redirect_uri
- is_enabled() returns True when both vars are present
- is_enabled() returns False when one or both vars are absent
- is_enabled() logs a warning when disabled
- GoogleOAuthCredentials is immutable (frozen dataclass)
- _read_env strips whitespace and treats blank values as absent
"""

import logging
import os
import sys
from pathlib import Path

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.config import (
    DEFAULT_SCOPES,
    SCOPE_EVENTS,
    SCOPE_READONLY,
    GoogleCredentialError,
    GoogleOAuthCredentials,
    _read_env,
    is_enabled,
    load_credentials,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
_FAKE_CLIENT_SECRET = "fake-client-secret"
_BOTH_VARS = {
    "GOOGLE_CLIENT_ID": _FAKE_CLIENT_ID,
    "GOOGLE_CLIENT_SECRET": _FAKE_CLIENT_SECRET,
}


# ---------------------------------------------------------------------------
# _read_env
# ---------------------------------------------------------------------------


class TestReadEnv:
    def test_returns_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        assert _read_env("GOOGLE_CLIENT_ID") == _FAKE_CLIENT_ID

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        assert _read_env("GOOGLE_CLIENT_ID") is None

    def test_returns_none_for_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        assert _read_env("GOOGLE_CLIENT_ID") is None

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "  padded-value  ")
        assert _read_env("GOOGLE_CLIENT_ID") == "padded-value"

    def test_returns_none_for_whitespace_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "   ")
        assert _read_env("GOOGLE_CLIENT_ID") is None


# ---------------------------------------------------------------------------
# load_credentials — happy path
# ---------------------------------------------------------------------------


class TestLoadCredentialsSuccess:
    def test_returns_credentials_dataclass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        creds = load_credentials()
        assert isinstance(creds, GoogleOAuthCredentials)

    def test_client_id_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        creds = load_credentials()
        assert creds.client_id == _FAKE_CLIENT_ID

    def test_client_secret_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        creds = load_credentials()
        assert creds.client_secret == _FAKE_CLIENT_SECRET

    def test_default_scopes_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        creds = load_credentials()
        assert creds.scopes == DEFAULT_SCOPES

    def test_default_redirect_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        creds = load_credentials()
        assert creds.redirect_uri == "https://myownlobster.ai/auth/google/callback"

    def test_custom_scopes_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        custom_scopes = (SCOPE_READONLY,)
        creds = load_credentials(scopes=custom_scopes)
        assert creds.scopes == custom_scopes

    def test_custom_redirect_uri_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        custom_uri = "http://localhost:8080/callback"
        creds = load_credentials(redirect_uri=custom_uri)
        assert creds.redirect_uri == custom_uri

    def test_credentials_are_immutable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GoogleOAuthCredentials is a frozen dataclass — mutation must raise."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        creds = load_credentials()
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            creds.client_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# load_credentials — missing / empty vars
# ---------------------------------------------------------------------------


class TestLoadCredentialsErrors:
    def test_raises_when_client_id_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        with pytest.raises(GoogleCredentialError) as exc_info:
            load_credentials()
        assert "GOOGLE_CLIENT_ID" in str(exc_info.value)

    def test_raises_when_client_secret_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(GoogleCredentialError) as exc_info:
            load_credentials()
        assert "GOOGLE_CLIENT_SECRET" in str(exc_info.value)

    def test_raises_when_both_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(GoogleCredentialError) as exc_info:
            load_credentials()
        error_msg = str(exc_info.value)
        assert "GOOGLE_CLIENT_ID" in error_msg
        assert "GOOGLE_CLIENT_SECRET" in error_msg

    def test_raises_when_client_id_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        with pytest.raises(GoogleCredentialError):
            load_credentials()

    def test_raises_when_client_secret_is_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "   ")
        with pytest.raises(GoogleCredentialError):
            load_credentials()

    def test_error_message_mentions_config_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Error message should guide the user to config.env."""
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(GoogleCredentialError) as exc_info:
            load_credentials()
        assert "config.env" in str(exc_info.value)

    def test_error_is_subclass_of_runtime_error(self) -> None:
        assert issubclass(GoogleCredentialError, RuntimeError)


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


class TestIsEnabled:
    def test_returns_true_when_both_vars_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        assert is_enabled() is True

    def test_returns_false_when_client_id_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        assert is_enabled() is False

    def test_returns_false_when_client_secret_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        assert is_enabled() is False

    def test_returns_false_when_both_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        assert is_enabled() is False

    def test_returns_false_when_client_id_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        assert is_enabled() is False

    def test_logs_warning_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with caplog.at_level(logging.WARNING, logger="integrations.google_calendar.config"):
            result = is_enabled()
        assert result is False
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        assert "Google Calendar" in record.message or "GOOGLE_CLIENT_ID" in record.message

    def test_logs_missing_var_name_in_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        with caplog.at_level(logging.WARNING, logger="integrations.google_calendar.config"):
            is_enabled()
        assert any("GOOGLE_CLIENT_ID" in r.message for r in caplog.records)

    def test_no_warning_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", _FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
        with caplog.at_level(logging.WARNING, logger="integrations.google_calendar.config"):
            is_enabled()
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Scope constants
# ---------------------------------------------------------------------------


class TestScopeConstants:
    def test_readonly_scope_is_google_url(self) -> None:
        assert SCOPE_READONLY.startswith("https://www.googleapis.com/auth/calendar")

    def test_events_scope_is_google_url(self) -> None:
        assert SCOPE_EVENTS.startswith("https://www.googleapis.com/auth/calendar")

    def test_default_scopes_contains_both(self) -> None:
        assert SCOPE_READONLY in DEFAULT_SCOPES
        assert SCOPE_EVENTS in DEFAULT_SCOPES

    def test_scopes_are_distinct(self) -> None:
        assert SCOPE_READONLY != SCOPE_EVENTS
