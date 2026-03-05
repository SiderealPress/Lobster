"""
Google Calendar OAuth 2.0 authorization flow.

Implements the server-side OAuth 2.0 Authorization Code flow for Google Calendar:
- Authorization URL generation (step 1: redirect user to Google)
- Token exchange (step 2: exchange auth code for access + refresh tokens)
- Token refresh (exchange an expired access token for a new one)

Design principles:
- Pure functions wherever possible; side effects isolated to callers
- No credentials or token values appear in logs
- All HTTP errors are converted to domain-specific exceptions
- TokenData is an immutable frozen dataclass

Environment variables (loaded via config.py):
    GOOGLE_CLIENT_ID      — OAuth 2.0 client identifier
    GOOGLE_CLIENT_SECRET  — OAuth 2.0 client secret

Google OAuth endpoints:
    Authorization: https://accounts.google.com/o/oauth2/v2/auth
    Token:         https://oauth2.googleapis.com/token
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import requests

from integrations.google_calendar.config import (
    DEFAULT_SCOPES,
    GoogleOAuthCredentials,
    load_credentials,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google OAuth endpoints (constants — never change between requests)
# ---------------------------------------------------------------------------

_GOOGLE_AUTH_URL: str = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"

# Buffer applied when checking token validity: treat tokens expiring within
# this window as already expired so callers have time to refresh.
_EXPIRY_BUFFER: timedelta = timedelta(minutes=5)

# Timeout for HTTP requests to Google's token endpoint (seconds).
_HTTP_TIMEOUT: int = 10


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenData:
    """Immutable container for Google OAuth token data.

    Attributes:
        access_token:  Short-lived credential for API calls.
        expires_at:    UTC datetime after which access_token is invalid.
        scope:         Space-separated scopes granted by the user.
        refresh_token: Long-lived credential used to obtain new access tokens.
                       May be None when refreshing (Google only sends it on
                       first authorisation or when prompt=consent is used).
    """

    access_token: str
    expires_at: datetime
    scope: str
    refresh_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OAuthError(RuntimeError):
    """Base class for OAuth-related errors."""


class OAuthTokenError(OAuthError):
    """Raised when Google's token endpoint returns an error response."""

    def __init__(self, error: str, description: str = "") -> None:
        self.error = error
        self.description = description
        super().__init__(
            f"Google token error: {error}"
            + (f" — {description}" if description else "")
        )


class OAuthNetworkError(OAuthError):
    """Raised when a network-level error occurs communicating with Google."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _build_auth_params(
    credentials: GoogleOAuthCredentials,
    state: str,
    scopes: tuple[str, ...],
) -> dict[str, str]:
    """Build the query-parameter dict for the Google authorization URL.

    This is a pure function — it has no side effects and produces the same
    output for the same inputs.

    Args:
        credentials: Client credentials (ID, redirect URI, etc.)
        state:       CSRF state token to embed in the URL.
        scopes:      OAuth scopes to request.

    Returns:
        Mapping of query parameter name -> value.
    """
    return {
        "client_id": credentials.client_id,
        "redirect_uri": credentials.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }


def _parse_token_response(raw: dict) -> TokenData:
    """Convert a raw Google token response dict into a TokenData.

    Computes ``expires_at`` from the ``expires_in`` seconds field relative
    to the current UTC time.  This is a pure-ish function (it reads the
    clock via ``datetime.now``), but all external I/O is isolated to
    ``exchange_code_for_tokens`` and ``refresh_access_token``.

    Args:
        raw: Parsed JSON body from Google's token endpoint.

    Returns:
        TokenData with all fields populated.

    Raises:
        OAuthTokenError: If the response contains an ``error`` field.
        KeyError:        If mandatory fields are absent (malformed response).
    """
    if "error" in raw:
        raise OAuthTokenError(
            error=raw["error"],
            description=raw.get("error_description", ""),
        )

    expires_in: int = int(raw["expires_in"])
    expires_at: datetime = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

    return TokenData(
        access_token=raw["access_token"],
        expires_at=expires_at,
        scope=raw.get("scope", ""),
        refresh_token=raw.get("refresh_token"),
    )


def _post_token_endpoint(payload: dict, timeout: int = _HTTP_TIMEOUT) -> dict:
    """POST a payload to Google's token endpoint and return the parsed JSON.

    Side-effecting function; isolates all HTTP I/O for the OAuth token calls.

    Args:
        payload: Form-encoded fields for the token request.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response body (may contain an ``error`` field).

    Raises:
        OAuthNetworkError: On connection errors, timeouts, or non-JSON bodies.
    """
    try:
        response = requests.post(
            _GOOGLE_TOKEN_URL,
            data=payload,
            timeout=timeout,
            # Explicitly set content-type for form posts
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return response.json()
    except requests.exceptions.Timeout as exc:
        raise OAuthNetworkError(
            f"Timeout reaching Google token endpoint after {timeout}s"
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise OAuthNetworkError(
            f"Connection error reaching Google token endpoint: {exc}"
        ) from exc
    except ValueError as exc:
        # requests raises ValueError when .json() fails to parse
        raise OAuthNetworkError(
            "Google token endpoint returned non-JSON response"
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_auth_url(
    state: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    credentials: Optional[GoogleOAuthCredentials] = None,
) -> str:
    """Build the Google OAuth 2.0 authorization URL.

    Sends the user to this URL so they can grant Lobster access to their
    Google Calendar.  After granting access, Google redirects to the
    registered redirect URI with ``code`` and ``state`` query parameters.

    Args:
        state:       A random, unguessable value used to protect against
                     CSRF attacks.  The caller is responsible for generating
                     this (e.g. via ``secrets.token_urlsafe(32)``).
        scopes:      OAuth scopes to request.  Defaults to DEFAULT_SCOPES
                     (calendar.readonly + calendar.events).
        credentials: Optional pre-loaded credentials.  If None, credentials
                     are loaded from environment variables via
                     ``load_credentials()``.

    Returns:
        Fully-formed Google authorization URL as a string.

    Raises:
        GoogleCredentialError: If credentials cannot be loaded from the
                               environment.
    """
    creds = credentials if credentials is not None else load_credentials()
    params = _build_auth_params(creds, state=state, scopes=scopes)
    return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(
    code: str,
    credentials: Optional[GoogleOAuthCredentials] = None,
) -> TokenData:
    """Exchange an authorization code for access and refresh tokens.

    Called after the user grants access and Google redirects back to the
    application with a ``code`` query parameter.

    Args:
        code:        The ``code`` value from Google's redirect callback.
        credentials: Optional pre-loaded credentials.  If None, credentials
                     are loaded from environment variables.

    Returns:
        TokenData with access_token, refresh_token, expires_at, and scope.

    Raises:
        OAuthTokenError:    If Google returns an error (e.g. bad code,
                            expired code, redirect URI mismatch).
        OAuthNetworkError:  If the HTTP request to Google fails.
        GoogleCredentialError: If credentials cannot be loaded.
    """
    creds = credentials if credentials is not None else load_credentials()

    payload = {
        "code": code,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "redirect_uri": creds.redirect_uri,
        "grant_type": "authorization_code",
    }

    log.debug("Exchanging authorization code for tokens (code length=%d)", len(code))

    raw = _post_token_endpoint(payload)
    token = _parse_token_response(raw)

    log.info(
        "Token exchange successful — scope=%r, has_refresh_token=%s",
        token.scope,
        token.refresh_token is not None,
    )
    return token


def refresh_access_token(
    refresh_token: str,
    credentials: Optional[GoogleOAuthCredentials] = None,
) -> TokenData:
    """Obtain a new access token using a refresh token.

    Access tokens are short-lived (~1 hour).  Call this function when
    ``is_token_valid()`` returns False for a stored token.  Google does not
    always return a new refresh token; the returned TokenData.refresh_token
    may be None, in which case callers should retain the original.

    Args:
        refresh_token: The long-lived refresh token from a previous
                       ``exchange_code_for_tokens()`` call.
        credentials:   Optional pre-loaded credentials.  If None, credentials
                       are loaded from environment variables.

    Returns:
        TokenData with a new access_token and expires_at.  refresh_token may
        be None if Google did not issue a new one.

    Raises:
        OAuthTokenError:       If the refresh token has been revoked or is
                               invalid.
        OAuthNetworkError:     If the HTTP request to Google fails.
        GoogleCredentialError: If credentials cannot be loaded.
    """
    creds = credentials if credentials is not None else load_credentials()

    payload = {
        "refresh_token": refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "grant_type": "refresh_token",
    }

    log.debug("Refreshing access token")

    raw = _post_token_endpoint(payload)
    token = _parse_token_response(raw)

    log.info("Token refresh successful — scope=%r", token.scope)
    return token


def is_token_valid(token: TokenData) -> bool:
    """Return True if the access token is still valid with a safety buffer.

    Applies a 5-minute buffer so callers have time to use the token before
    it actually expires.  This is a pure function (reads the system clock).

    Args:
        token: TokenData to check.

    Returns:
        True if the token will remain valid for at least 5 more minutes.
    """
    now = datetime.now(tz=timezone.utc)
    return token.expires_at > now + _EXPIRY_BUFFER
