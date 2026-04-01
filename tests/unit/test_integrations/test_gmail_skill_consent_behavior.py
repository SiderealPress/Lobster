"""
Tests for the gmail skill's consent-link auth trigger behavior (BIS-256).

These tests verify the Python logic pattern prescribed in
``lobster-shop/gmail/behavior/system.md`` for the "connect my Gmail" intent.
The pattern is:

1. Call ``generate_consent_link("gmail")``; on success, send the user the
   myownlobster.ai consent URL.
2. On any exception (RuntimeError from missing env vars, network failure, etc.),
   fall back gracefully with a user-friendly message — never surface the error
   to the user.
3. The scope passed to generate_consent_link must be "gmail", not "calendar".

The tests import the real ``generate_consent_link`` function with HTTP and
env-var I/O mocked out, verifying real integration points without live services.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from pathlib import Path

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_auth.consent import generate_consent_link

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_INSTANCE_URL = "https://vps.example.com"
_FAKE_SECRET = "test-internal-secret-abc123"
_FAKE_CONSENT_URL = "https://myownlobster.ai/connect/gmail?token=abc123"
_ENV = {
    "LOBSTER_INSTANCE_URL": _FAKE_INSTANCE_URL,
    "LOBSTER_INTERNAL_SECRET": _FAKE_SECRET,
}


def _mock_consent_response(url: str = _FAKE_CONSENT_URL) -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {"url": url}
    resp.text = f'{{"url": "{url}"}}'
    return resp


# ---------------------------------------------------------------------------
# Helper: the exact auth-trigger logic from behavior/system.md
# (extracted as a pure function so we can test it without a full dispatcher)
# ---------------------------------------------------------------------------


def _handle_connect_gmail() -> tuple[str, list[str]]:
    """
    Replicate the auth-trigger code block from behavior/system.md as a
    plain function.

    Returns:
        (reply_text, warning_messages) — warning_messages is empty on success.
    """
    import logging

    warnings: list[str] = []
    log = logging.getLogger(__name__)

    try:
        url = generate_consent_link("gmail")
        reply = (
            "To connect your Gmail, tap this link (expires in 30 minutes):\n"
            f"[Connect Gmail]({url})\n\n"
            "After connecting, I'll be able to read and search your emails."
        )
    except Exception as exc:
        msg = f"generate_consent_link('gmail') failed — degrading gracefully: {exc}"
        log.warning(msg)
        warnings.append(msg)
        reply = (
            "I couldn't generate a Gmail connection link right now. "
            "Please try again in a few minutes."
        )

    return reply, warnings


# ---------------------------------------------------------------------------
# Happy path: generate_consent_link succeeds
# ---------------------------------------------------------------------------


class TestAuthTriggerHappyPath:
    def test_reply_contains_myownlobster_url(self):
        """When generate_consent_link succeeds, the reply contains the consent URL."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ):
            reply, warnings = _handle_connect_gmail()

        assert _FAKE_CONSENT_URL in reply
        assert warnings == []

    def test_reply_contains_connect_gmail_link_text(self):
        """Reply uses the expected link label for mobile readability."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ):
            reply, _ = _handle_connect_gmail()

        assert "Connect Gmail" in reply

    def test_reply_mentions_expiry(self):
        """Reply tells the user the link expires so they know to act promptly."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ):
            reply, _ = _handle_connect_gmail()

        assert "30 minutes" in reply

    def test_reply_does_not_mention_fallback_language(self):
        """On success, the reply should not mention fallback language."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ):
            reply, _ = _handle_connect_gmail()

        assert "couldn't generate" not in reply.lower()

    def test_consent_url_contains_gmail_scope(self):
        """The returned URL must point to the gmail consent path."""
        url = "https://myownlobster.ai/connect/gmail?token=xyz"
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(url=url),
        ):
            reply, _ = _handle_connect_gmail()

        assert "myownlobster.ai/connect/gmail" in reply
        assert "token=" in reply


# ---------------------------------------------------------------------------
# Fallback: generate_consent_link raises RuntimeError (missing env vars)
# ---------------------------------------------------------------------------


class TestAuthTriggerFallbackMissingEnv:
    def test_fallback_when_instance_url_missing(self):
        """Missing LOBSTER_INSTANCE_URL → graceful fallback, no user-facing error."""
        env = {"LOBSTER_INTERNAL_SECRET": _FAKE_SECRET}
        with patch.dict("os.environ", env, clear=True):
            reply, warnings = _handle_connect_gmail()

        assert "couldn't generate" in reply.lower()
        assert len(warnings) == 1

    def test_fallback_when_secret_missing(self):
        """Missing LOBSTER_INTERNAL_SECRET → graceful fallback."""
        env = {"LOBSTER_INSTANCE_URL": _FAKE_INSTANCE_URL}
        with patch.dict("os.environ", env, clear=True):
            reply, warnings = _handle_connect_gmail()

        assert "couldn't generate" in reply.lower()
        assert len(warnings) == 1

    def test_fallback_when_both_env_vars_missing(self):
        """Both env vars missing → graceful fallback."""
        with patch.dict("os.environ", {}, clear=True):
            reply, warnings = _handle_connect_gmail()

        assert "couldn't generate" in reply.lower()
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Fallback: generate_consent_link raises due to network error
# ---------------------------------------------------------------------------


class TestAuthTriggerFallbackNetworkError:
    def test_fallback_on_connection_error(self):
        """Network failure → graceful fallback, no error surfaced to user."""
        import requests as req_lib

        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            reply, warnings = _handle_connect_gmail()

        assert "couldn't generate" in reply.lower()
        assert len(warnings) == 1

    def test_fallback_on_timeout(self):
        """Timeout → graceful fallback."""
        import requests as req_lib

        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=req_lib.exceptions.Timeout("timed out"),
        ):
            reply, warnings = _handle_connect_gmail()

        assert "couldn't generate" in reply.lower()

    def test_fallback_on_http_500(self):
        """Server error from myownlobster.ai → graceful fallback."""
        error_resp = MagicMock()
        error_resp.ok = False
        error_resp.status_code = 500
        error_resp.text = "Internal Server Error"

        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=error_resp,
        ):
            reply, warnings = _handle_connect_gmail()

        assert "couldn't generate" in reply.lower()


# ---------------------------------------------------------------------------
# Fallback reply must not expose internal error details
# ---------------------------------------------------------------------------


class TestFallbackReplyIsUserFriendly:
    def test_env_var_names_not_surfaced(self):
        """Fallback reply must not contain env var names."""
        with patch.dict("os.environ", {}, clear=True):
            reply, _ = _handle_connect_gmail()

        assert "LOBSTER_INSTANCE_URL" not in reply
        assert "LOBSTER_INTERNAL_SECRET" not in reply
        assert "RuntimeError" not in reply
        assert "Missing required" not in reply

    def test_network_error_details_not_surfaced(self):
        """Network error details must not leak into the reply text."""
        import requests as req_lib

        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            reply, _ = _handle_connect_gmail()

        assert "connection refused" not in reply
        assert "ConnectionError" not in reply


# ---------------------------------------------------------------------------
# Scope isolation: gmail skill uses "gmail" scope, not "calendar"
# ---------------------------------------------------------------------------


class TestConsentLinkScope:
    def test_requests_gmail_scope(self):
        """The auth trigger must call generate_consent_link with scope='gmail'."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ) as mock_post:
            _handle_connect_gmail()

        call_json = mock_post.call_args.kwargs["json"]
        assert call_json["scope"] == "gmail"

    def test_calendar_scope_not_used(self):
        """The Gmail auth trigger must not request calendar scope."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ) as mock_post:
            _handle_connect_gmail()

        call_json = mock_post.call_args.kwargs["json"]
        assert call_json["scope"] != "calendar"


# ---------------------------------------------------------------------------
# Warning logged on fallback (without leaking secrets)
# ---------------------------------------------------------------------------


class TestWarningLogging:
    def test_warning_logged_on_fallback(self, caplog):
        """A warning must be logged when generate_consent_link fails."""
        with caplog.at_level(logging.WARNING):
            with patch.dict("os.environ", {}, clear=True):
                _handle_connect_gmail()

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_messages) >= 1

    def test_secret_not_in_warning_log(self, caplog):
        """The internal secret must not appear in any log record."""
        import requests as req_lib

        with caplog.at_level(logging.DEBUG):
            with patch.dict("os.environ", _ENV), patch(
                "integrations.google_auth.consent.requests.post",
                side_effect=req_lib.exceptions.ConnectionError("conn fail"),
            ):
                _handle_connect_gmail()

        for record in caplog.records:
            assert _FAKE_SECRET not in record.getMessage()


# ---------------------------------------------------------------------------
# Calendar skill consent behavior is unaffected by gmail skill
# ---------------------------------------------------------------------------


class TestCalendarSkillUnaffected:
    """Verify that the gmail and calendar consent paths are independent."""

    def test_gmail_consent_link_uses_gmail_path(self):
        """Gmail consent link should point to /connect/gmail."""
        url = "https://myownlobster.ai/connect/gmail?token=abc"
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(url=url),
        ):
            reply, _ = _handle_connect_gmail()

        assert "/connect/gmail" in reply
        assert "/connect/calendar" not in reply
