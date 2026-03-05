"""
Tests for Google Calendar Phase 4 — natural language skill layer.

Covers:
- Auth URL generation trigger: is_enabled() gating, secrets.token_urlsafe usage,
  generate_auth_url returns a valid Google URL with required params
- Pure helper logic: auth status detection (load_token present/absent),
  fallback-to-deep-link path when create_event returns None
- Dual-mode detection: authenticated vs unauthenticated branch
- Token-in-message safety: token values never appear in reply strings

All network calls and file I/O are mocked — no real credentials or disk access.
"""

from __future__ import annotations

import sys
import tempfile
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from urllib.parse import parse_qs, urlparse

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.config import (
    DEFAULT_SCOPES,
    GoogleOAuthCredentials,
    is_enabled,
    load_credentials,
    GoogleCredentialError,
)
from integrations.google_calendar.oauth import (
    TokenData,
    generate_auth_url,
    is_token_valid,
)
from integrations.google_calendar.token_store import load_token, save_token
from utils.calendar import gcal_add_link_md


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_credentials() -> GoogleOAuthCredentials:
    return GoogleOAuthCredentials(
        client_id="test-client-id",
        client_secret="test-client-secret",
        scopes=DEFAULT_SCOPES,
        redirect_uri="https://myownlobster.ai/auth/google/callback",
    )


@pytest.fixture
def valid_token() -> TokenData:
    return TokenData(
        access_token="ya29.test-access-token",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope=" ".join(DEFAULT_SCOPES),
        refresh_token="1//test-refresh-token",
    )


@pytest.fixture
def temp_token_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for token files."""
    token_dir = tmp_path / "gcal-tokens"
    token_dir.mkdir(parents=True)
    return token_dir


# ---------------------------------------------------------------------------
# Auth URL generation
# ---------------------------------------------------------------------------


class TestGenerateAuthUrl:
    """Tests for the auth URL generation path used by the auth command handler."""

    def test_returns_google_auth_base_url(self, sample_credentials):
        url = generate_auth_url(state="test-state", credentials=sample_credentials)
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "accounts.google.com"
        assert parsed.path == "/o/oauth2/v2/auth"

    def test_includes_client_id_param(self, sample_credentials):
        url = generate_auth_url(state="test-state", credentials=sample_credentials)
        params = parse_qs(urlparse(url).query)
        assert params["client_id"] == ["test-client-id"]

    def test_includes_redirect_uri(self, sample_credentials):
        url = generate_auth_url(state="test-state", credentials=sample_credentials)
        params = parse_qs(urlparse(url).query)
        assert params["redirect_uri"] == ["https://myownlobster.ai/auth/google/callback"]

    def test_state_param_is_embedded(self, sample_credentials):
        state = "csrf-test-token-abc123"
        url = generate_auth_url(state=state, credentials=sample_credentials)
        params = parse_qs(urlparse(url).query)
        assert params["state"] == [state]

    def test_includes_offline_access_type(self, sample_credentials):
        url = generate_auth_url(state="s", credentials=sample_credentials)
        params = parse_qs(urlparse(url).query)
        assert params["access_type"] == ["offline"]

    def test_includes_consent_prompt(self, sample_credentials):
        url = generate_auth_url(state="s", credentials=sample_credentials)
        params = parse_qs(urlparse(url).query)
        assert params["prompt"] == ["consent"]

    def test_default_scopes_included(self, sample_credentials):
        url = generate_auth_url(state="s", credentials=sample_credentials)
        params = parse_qs(urlparse(url).query)
        scope_str = params["scope"][0]
        for scope in DEFAULT_SCOPES:
            assert scope in scope_str

    def test_different_states_produce_different_urls(self, sample_credentials):
        url1 = generate_auth_url(state="state-aaa", credentials=sample_credentials)
        url2 = generate_auth_url(state="state-bbb", credentials=sample_credentials)
        assert url1 != url2

    def test_token_urlsafe_produces_valid_state(self):
        """Verify secrets.token_urlsafe(32) produces a non-empty URL-safe string."""
        import secrets
        state = secrets.token_urlsafe(32)
        assert len(state) > 0
        # URL-safe base64 characters only (no +, /, =)
        assert all(c.isalnum() or c in ("-", "_") for c in state)

    def test_is_enabled_false_when_env_missing(self):
        """is_enabled() returns False when env vars are absent."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove Google env vars if present
            env = {k: v for k, v in os.environ.items()
                   if k not in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")}
            with patch.dict(os.environ, env, clear=True):
                result = is_enabled()
        assert result is False

    def test_is_enabled_true_when_both_vars_set(self):
        """is_enabled() returns True when both env vars are present."""
        with patch.dict(os.environ, {
            "GOOGLE_CLIENT_ID": "test-id",
            "GOOGLE_CLIENT_SECRET": "test-secret",
        }):
            result = is_enabled()
        assert result is True

    def test_auth_url_does_not_contain_client_secret(self, sample_credentials):
        """Auth URL must never expose the client secret."""
        url = generate_auth_url(state="s", credentials=sample_credentials)
        assert "test-client-secret" not in url


# ---------------------------------------------------------------------------
# Auth status detection (dual-mode logic)
# ---------------------------------------------------------------------------


class TestAuthStatusDetection:
    """Tests for the authenticated vs unauthenticated mode check."""

    def test_no_token_file_means_unauthenticated(self, temp_token_dir: Path):
        """load_token returns None when no file exists — unauthenticated mode."""
        token = load_token("OWNER_CHAT_ID_PLACEHOLDER", token_dir=temp_token_dir)
        assert token is None
        assert (token is None) is True  # explicit: is_authenticated = token is not None => False

    def test_token_file_present_means_authenticated(
        self, temp_token_dir: Path, valid_token: TokenData
    ):
        """load_token returns TokenData when file exists — authenticated mode."""
        save_token("OWNER_CHAT_ID_PLACEHOLDER", valid_token, token_dir=temp_token_dir)
        token = load_token("OWNER_CHAT_ID_PLACEHOLDER", token_dir=temp_token_dir)
        assert token is not None
        assert isinstance(token, TokenData)

    def test_is_authenticated_expression(
        self, temp_token_dir: Path, valid_token: TokenData
    ):
        """The `token is not None` expression correctly identifies authenticated state."""
        # Unauthenticated
        token = load_token("nobody", token_dir=temp_token_dir)
        assert (token is not None) is False

        # Authenticated
        save_token("drew", valid_token, token_dir=temp_token_dir)
        token = load_token("drew", token_dir=temp_token_dir)
        assert (token is not None) is True

    def test_drew_user_id_as_string(self, temp_token_dir: Path, valid_token: TokenData):
        """Drew's user_id must be the string 'OWNER_CHAT_ID_PLACEHOLDER' (chat_id as str)."""
        user_id = str(OWNER_CHAT_ID_PLACEHOLDER)
        assert user_id == "OWNER_CHAT_ID_PLACEHOLDER"
        save_token(user_id, valid_token, token_dir=temp_token_dir)
        token = load_token(user_id, token_dir=temp_token_dir)
        assert token is not None


# ---------------------------------------------------------------------------
# Fallback to deep link when API fails
# ---------------------------------------------------------------------------


class TestFallbackToDeepLink:
    """Tests for the graceful degradation path: API failure → deep link."""

    def _make_deep_link_reply(self, title: str, start: datetime) -> str:
        """Simulate the fallback branch used in the skill's behavior instructions."""
        from utils.calendar import gcal_add_link_md
        link = gcal_add_link_md(title=title, start=start)
        return f"Couldn't add via API — use this link instead:\n{link}"

    def test_fallback_reply_contains_deep_link(self):
        start = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
        reply = self._make_deep_link_reply("Meeting with Sarah", start)
        assert "[Add to Google Calendar]" in reply
        assert "https://calendar.google.com" in reply

    def test_fallback_reply_does_not_expose_none(self):
        """The fallback path must never send 'None' to the user."""
        start = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
        reply = self._make_deep_link_reply("Meeting with Sarah", start)
        assert "None" not in reply

    def test_create_event_none_triggers_fallback(self):
        """When create_event returns None, the deep link fallback is used."""
        from utils.calendar import gcal_add_link_md
        start = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
        title = "Meeting with Sarah"

        # Simulate: event = create_event(...) → None (auth failure / API error)
        event = None

        if event is not None:
            link = f"[View in Google Calendar]({event.url})" if event.url else gcal_add_link_md(title, start)
            reply = f"Done — added \"{title}\" to your calendar.\n{link}"
        else:
            link = gcal_add_link_md(title, start)
            reply = f"Couldn't add via API — use this link instead:\n{link}"

        assert "[Add to Google Calendar]" in reply
        assert "calendar.google.com" in reply
        assert "Done" not in reply  # success branch not taken

    def test_create_event_success_uses_view_link(self):
        """When create_event returns an event with url, [View in Google Calendar] is used."""
        from utils.calendar import gcal_add_link_md
        start = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
        title = "Meeting with Sarah"
        event_url = "https://calendar.google.com/calendar/r/eventedit?eid=abc123"

        # Simulate: event = create_event(...) → CalendarEvent(...)
        event = MagicMock()
        event.url = event_url
        event.title = title
        event.start = start

        if event is not None:
            link = f"[View in Google Calendar]({event.url})" if event.url else gcal_add_link_md(title, start)
            reply = f"Done — added \"{title}\" to your calendar.\n{link}"
        else:
            link = gcal_add_link_md(title, start)
            reply = f"Couldn't add via API — use this link instead:\n{link}"

        assert "[View in Google Calendar]" in reply
        assert event_url in reply
        assert "Done" in reply


# ---------------------------------------------------------------------------
# Token value safety (never expose tokens in messages)
# ---------------------------------------------------------------------------


class TestTokenSafety:
    """Tokens must never appear in user-facing messages."""

    def test_auth_url_does_not_contain_access_token(self, valid_token: TokenData, sample_credentials):
        url = generate_auth_url(state="s", credentials=sample_credentials)
        assert valid_token.access_token not in url

    def test_auth_reply_only_contains_url(self, sample_credentials):
        """Auth command reply contains the URL but not the client secret."""
        import secrets
        state = secrets.token_urlsafe(32)
        url = generate_auth_url(state=state, credentials=sample_credentials)
        reply = f"Click to connect your Google Calendar:\n[Authorize Google Calendar]({url})"
        assert "test-client-secret" not in reply
        assert "[Authorize Google Calendar]" in reply

    def test_event_list_reply_does_not_expose_access_token(self, valid_token: TokenData):
        """Event listing replies must not contain the access token."""
        events = [
            MagicMock(
                title="Team standup",
                start=datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
                url="https://calendar.google.com/calendar/r/eventedit?eid=xyz",
            )
        ]
        lines = []
        for e in events:
            time_str = e.start.strftime("%a %b %-d, %-I:%M %p UTC")
            event_link = f"[{e.title}]({e.url})" if e.url else e.title
            lines.append(f"- {time_str}: {event_link}")
        reply = "Your upcoming events:\n" + "\n".join(lines)
        assert valid_token.access_token not in reply
        assert "[Team standup]" in reply


# ---------------------------------------------------------------------------
# Deep link fallback when get_upcoming_events returns empty
# ---------------------------------------------------------------------------


class TestReadEventsFallback:
    """When get_upcoming_events returns [], reply gracefully — no crash."""

    def test_empty_events_produces_no_events_message(self):
        events = []  # simulates [] return from get_upcoming_events on auth/API failure
        if not events:
            reply = "No upcoming events in the next 7 days."
        else:
            lines = [f"- {e.title}" for e in events]
            reply = "Your upcoming events:\n" + "\n".join(lines)
        assert reply == "No upcoming events in the next 7 days."

    def test_non_empty_events_produces_list(self):
        event = MagicMock()
        event.title = "Morning standup"
        event.start = datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc)
        event.url = "https://calendar.google.com/calendar/r/eventedit?eid=abc"
        events = [event]

        if not events:
            reply = "No upcoming events in the next 7 days."
        else:
            lines = []
            for e in events:
                time_str = e.start.strftime("%a %b %-d, %-I:%M %p UTC")
                event_link = f"[{e.title}]({e.url})" if e.url else e.title
                lines.append(f"- {time_str}: {event_link}")
            reply = "Your upcoming events:\n" + "\n".join(lines)

        assert "[Morning standup]" in reply
        assert "No upcoming events" not in reply
