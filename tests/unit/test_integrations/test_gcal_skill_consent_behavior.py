"""
Tests for the gcal-links skill's consent-link auth trigger behavior (BIS-255).

These tests verify the Python logic pattern prescribed in
``lobster-shop/gcal-links/behavior/system.md`` for the "connect my Google
Calendar" intent.  The pattern is:

1. Call ``generate_consent_link("calendar")``; on success, send the user the
   myownlobster.ai consent URL.
2. On any exception (RuntimeError from missing env vars, network failure, etc.),
   fall back gracefully to a deep link and log a warning — never surface the
   error to the user.
3. Deep link behavior for individual event creation is unaffected by this path.

The tests import the real ``generate_consent_link`` and ``gcal_add_link_md``
functions with HTTP and env-var I/O mocked out, so they verify real integration
points without requiring live services.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_auth.consent import generate_consent_link
from utils.calendar import gcal_add_link_md

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_INSTANCE_URL = "https://vps.example.com"
_FAKE_SECRET = "test-internal-secret-abc123"
_FAKE_CONSENT_URL = "https://myownlobster.ai/connect/calendar?token=abc123"
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
# Helper: the exact auth-trigger logic from system.md
# (extracted as a pure function so we can test it without a full dispatcher)
# ---------------------------------------------------------------------------


def _handle_connect_calendar() -> tuple[str, list[str]]:
    """
    Replicate the auth-trigger code block from system.md as a plain function.

    Returns:
        (reply_text, warning_messages) — warning_messages is empty on success.
    """
    import logging

    warnings: list[str] = []
    log = logging.getLogger(__name__)

    try:
        url = generate_consent_link("calendar")
        reply = (
            "To connect your Google Calendar, tap this link (expires in 30 minutes):\n"
            f"[Connect Google Calendar]({url})\n\n"
            "After connecting, I'll be able to read and create calendar events for you."
        )
    except Exception as exc:
        msg = f"generate_consent_link('calendar') failed — falling back to deep link: {exc}"
        log.warning(msg)
        warnings.append(msg)

        link = gcal_add_link_md(
            title="My Event",
            start=datetime.now(tz=timezone.utc),
        )
        reply = (
            "I couldn't generate a connection link right now. "
            "You can still add individual events to your calendar using this link:\n"
            f"{link}"
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
            reply, warnings = _handle_connect_calendar()

        assert _FAKE_CONSENT_URL in reply
        assert warnings == []

    def test_reply_contains_connect_calendar_link_text(self):
        """Reply uses the expected link label for mobile readability."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ):
            reply, _ = _handle_connect_calendar()

        assert "Connect Google Calendar" in reply

    def test_reply_mentions_expiry(self):
        """Reply tells the user the link expires so they know to act promptly."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ):
            reply, _ = _handle_connect_calendar()

        assert "30 minutes" in reply

    def test_reply_does_not_mention_deep_link_fallback(self):
        """On success, the reply should not mention fallback language."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ):
            reply, _ = _handle_connect_calendar()

        assert "couldn't generate" not in reply.lower()

    def test_consent_url_contains_calendar_scope(self):
        """The returned URL must point to the calendar consent path."""
        url = "https://myownlobster.ai/connect/calendar?token=xyz"
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(url=url),
        ):
            reply, _ = _handle_connect_calendar()

        assert "myownlobster.ai/connect/calendar" in reply
        assert "token=" in reply


# ---------------------------------------------------------------------------
# Fallback: generate_consent_link raises RuntimeError (missing env vars)
# ---------------------------------------------------------------------------


class TestAuthTriggerFallbackMissingEnv:
    def test_fallback_when_instance_url_missing(self):
        """Missing LOBSTER_INSTANCE_URL → fallback to deep link, no user-facing error."""
        env = {"LOBSTER_INTERNAL_SECRET": _FAKE_SECRET}
        with patch.dict("os.environ", env, clear=True):
            reply, warnings = _handle_connect_calendar()

        # Reply uses fallback language
        assert "couldn't generate" in reply.lower()
        # Deep link is present (calendar.google.com)
        assert "calendar.google.com" in reply
        # Warning was logged
        assert len(warnings) == 1

    def test_fallback_when_secret_missing(self):
        """Missing LOBSTER_INTERNAL_SECRET → fallback to deep link."""
        env = {"LOBSTER_INSTANCE_URL": _FAKE_INSTANCE_URL}
        with patch.dict("os.environ", env, clear=True):
            reply, warnings = _handle_connect_calendar()

        assert "couldn't generate" in reply.lower()
        assert "calendar.google.com" in reply
        assert len(warnings) == 1

    def test_fallback_when_both_env_vars_missing(self):
        """Both env vars missing → fallback to deep link."""
        with patch.dict("os.environ", {}, clear=True):
            reply, warnings = _handle_connect_calendar()

        assert "couldn't generate" in reply.lower()
        assert "calendar.google.com" in reply
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
            reply, warnings = _handle_connect_calendar()

        assert "couldn't generate" in reply.lower()
        assert "calendar.google.com" in reply
        assert len(warnings) == 1

    def test_fallback_on_timeout(self):
        """Timeout → graceful fallback."""
        import requests as req_lib

        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=req_lib.exceptions.Timeout("timed out"),
        ):
            reply, warnings = _handle_connect_calendar()

        assert "couldn't generate" in reply.lower()
        assert "calendar.google.com" in reply

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
            reply, warnings = _handle_connect_calendar()

        assert "couldn't generate" in reply.lower()
        assert "calendar.google.com" in reply


# ---------------------------------------------------------------------------
# Fallback reply must not expose internal error details
# ---------------------------------------------------------------------------


class TestFallbackReplyIsUserFriendly:
    def test_error_details_not_surfaced_to_user(self):
        """The fallback reply must not contain exception messages or env var names."""
        with patch.dict("os.environ", {}, clear=True):
            reply, _ = _handle_connect_calendar()

        assert "LOBSTER_INSTANCE_URL" not in reply
        assert "LOBSTER_INTERNAL_SECRET" not in reply
        assert "RuntimeError" not in reply
        assert "Missing required" not in reply

    def test_error_details_not_surfaced_on_network_error(self):
        """Network error details must not leak into the reply text."""
        import requests as req_lib

        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            reply, _ = _handle_connect_calendar()

        assert "connection refused" not in reply
        assert "ConnectionError" not in reply


# ---------------------------------------------------------------------------
# Deep link behavior is preserved for individual event creation
# ---------------------------------------------------------------------------


class TestDeepLinkPreservedForEventCreation:
    """
    The deep link path (Mode A) for adding a single event must be unaffected
    by the consent-link flow.  These tests verify gcal_add_link_md works
    independently and produces a usable Google Calendar URL.
    """

    def test_deep_link_contains_google_calendar_domain(self):
        start = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
        link = gcal_add_link_md(title="Doctor appointment", start=start)
        assert "calendar.google.com" in link

    def test_deep_link_contains_event_title(self):
        start = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
        link = gcal_add_link_md(title="Doctor appointment", start=start)
        assert "Doctor" in link or "Doctor+appointment" in link or "Doctor%20appointment" in link

    def test_deep_link_does_not_require_env_vars(self):
        """Deep link generation must work with no env vars set."""
        with patch.dict("os.environ", {}, clear=True):
            start = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
            link = gcal_add_link_md(title="Test Event", start=start)
        assert "calendar.google.com" in link


# ---------------------------------------------------------------------------
# generate_consent_link scope is "calendar" (not "gmail")
# ---------------------------------------------------------------------------


class TestConsentLinkScope:
    def test_requests_calendar_scope(self):
        """The auth trigger must call generate_consent_link with scope='calendar'."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ) as mock_post:
            _handle_connect_calendar()

        call_json = mock_post.call_args.kwargs["json"]
        assert call_json["scope"] == "calendar"

    def test_gmail_scope_not_used(self):
        """The calendar auth trigger must not request gmail scope."""
        with patch.dict("os.environ", _ENV), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_consent_response(),
        ) as mock_post:
            _handle_connect_calendar()

        call_json = mock_post.call_args.kwargs["json"]
        assert call_json["scope"] != "gmail"


# ---------------------------------------------------------------------------
# Warning logged on fallback (without leaking secrets)
# ---------------------------------------------------------------------------


class TestWarningLogging:
    def test_warning_logged_on_fallback(self, caplog):
        """A warning must be logged when generate_consent_link fails."""
        with caplog.at_level(logging.WARNING):
            with patch.dict("os.environ", {}, clear=True):
                _handle_connect_calendar()

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
                _handle_connect_calendar()

        for record in caplog.records:
            assert _FAKE_SECRET not in record.getMessage()
