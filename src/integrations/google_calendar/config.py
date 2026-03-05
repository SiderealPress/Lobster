"""
Google Calendar OAuth credential configuration.

Loads client credentials from environment variables that are injected at
runtime from Lobster's secrets layer (config.env).  No credential values ever
appear in source code.

Environment variables:
    GOOGLE_CLIENT_ID      — OAuth 2.0 client identifier  (required for calendar features)
    GOOGLE_CLIENT_SECRET  — OAuth 2.0 client secret      (required for calendar features)

The integration degrades gracefully: if either variable is absent, all Google
Calendar features are disabled and a single warning is emitted at import time.
Callers should check ``is_enabled()`` before attempting any OAuth flow.

OAuth scopes required for full calendar access:
    https://www.googleapis.com/auth/calendar.readonly      — list/read events
    https://www.googleapis.com/auth/calendar.events        — create/update events

Redirect URI expected by the Google OAuth app:
    https://myownlobster.ai/auth/google/callback
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

#: Minimum scope set for read-only calendar access.
SCOPE_READONLY: str = "https://www.googleapis.com/auth/calendar.readonly"

#: Full scope for creating and modifying events.
SCOPE_EVENTS: str = "https://www.googleapis.com/auth/calendar.events"

#: Default scopes requested during the OAuth flow.
DEFAULT_SCOPES: tuple[str, ...] = (SCOPE_READONLY, SCOPE_EVENTS)


# ---------------------------------------------------------------------------
# Credential dataclass (immutable value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoogleOAuthCredentials:
    """Immutable container for Google OAuth client credentials.

    Attributes:
        client_id:     OAuth 2.0 client identifier from Google Cloud Console.
        client_secret: OAuth 2.0 client secret from Google Cloud Console.
        scopes:        Tuple of OAuth scope strings to request.
        redirect_uri:  Registered redirect URI in Google Cloud Console.
    """

    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    redirect_uri: str


# ---------------------------------------------------------------------------
# Internal helpers (pure functions)
# ---------------------------------------------------------------------------

_ENV_CLIENT_ID = "GOOGLE_CLIENT_ID"
_ENV_CLIENT_SECRET = "GOOGLE_CLIENT_SECRET"
_DEFAULT_REDIRECT_URI = "https://myownlobster.ai/auth/google/callback"


def _read_env(key: str) -> str | None:
    """Return the stripped value of an environment variable, or None if absent/empty."""
    value = os.environ.get(key, "").strip()
    return value if value else None


def _build_credentials(
    client_id: str,
    client_secret: str,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
) -> GoogleOAuthCredentials:
    """Construct a GoogleOAuthCredentials instance from validated values.

    This is a pure factory function — it performs no I/O and has no side effects.

    Args:
        client_id:     OAuth client ID.
        client_secret: OAuth client secret.
        scopes:        OAuth scopes to request.
        redirect_uri:  Redirect URI registered with Google.

    Returns:
        A frozen GoogleOAuthCredentials dataclass.
    """
    return GoogleOAuthCredentials(
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        redirect_uri=redirect_uri,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class GoogleCredentialError(RuntimeError):
    """Raised when required Google credentials are missing from the environment."""


def load_credentials(
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
) -> GoogleOAuthCredentials:
    """Load Google OAuth credentials from environment variables.

    Reads GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from the process
    environment.  These must be set before calling this function (typically
    via Lobster's config.env secrets layer).

    Args:
        scopes:       OAuth scopes to include in the credentials object.
                      Defaults to DEFAULT_SCOPES (readonly + events).
        redirect_uri: OAuth redirect URI.  Defaults to the production
                      myownlobster.ai callback URL.

    Returns:
        A frozen GoogleOAuthCredentials dataclass.

    Raises:
        GoogleCredentialError: If GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET
                               is absent or empty in the environment.
    """
    client_id = _read_env(_ENV_CLIENT_ID)
    client_secret = _read_env(_ENV_CLIENT_SECRET)

    missing: list[str] = []
    if client_id is None:
        missing.append(_ENV_CLIENT_ID)
    if client_secret is None:
        missing.append(_ENV_CLIENT_SECRET)

    if missing:
        raise GoogleCredentialError(
            f"Google Calendar credentials missing from environment: {', '.join(missing)}. "
            "Set these variables in config.env to enable Google Calendar features."
        )

    # Both are confirmed non-None at this point; assert for type narrowing.
    assert client_id is not None
    assert client_secret is not None

    return _build_credentials(client_id, client_secret, scopes, redirect_uri)


def is_enabled() -> bool:
    """Return True if Google Calendar credentials are available in the environment.

    This is a cheap pre-flight check — it does not validate the credentials,
    only that both required environment variables are present and non-empty.
    Use this to gate calendar feature availability at startup or in handlers.

    Returns:
        True if both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set.
        False otherwise (with a warning logged the first time this returns False).

    Example:
        >>> if is_enabled():
        ...     creds = load_credentials()
        ...     # proceed with OAuth flow
        ... else:
        ...     # calendar features unavailable
        ...     pass
    """
    client_id = _read_env(_ENV_CLIENT_ID)
    client_secret = _read_env(_ENV_CLIENT_SECRET)

    if client_id is None or client_secret is None:
        missing = [
            name
            for name, val in (
                (_ENV_CLIENT_ID, client_id),
                (_ENV_CLIENT_SECRET, client_secret),
            )
            if val is None
        ]
        log.warning(
            "Google Calendar integration disabled — missing environment variables: %s. "
            "Set these in config.env to enable calendar features.",
            ", ".join(missing),
        )
        return False

    return True
