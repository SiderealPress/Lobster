"""
Per-user Google OAuth token persistence — multi-backend edition.

Supports two OAuth backends:

1. **myownlobster** (default for hosted instances): fetches tokens from the
   myownlobster.ai internal API (``GET /api/internal/calendar-tokens``).
   Refreshed tokens are written back to the API (``PUT``) and cached locally
   so repeated calls are fast.

2. **local** (for self-hosted Lobster instances): reads from JSON files in
   ``~/messages/config/gcal-tokens/{user_id}.json``, exactly as before.

Backend selection is driven by ``~/messages/config/calendar-config.json``::

    {
      "oauth_backend": "myownlobster",   // or "local"
      "myownlobster": {
        "api_base": "https://myownlobster.ai",
        "token_endpoint": "/api/internal/calendar-tokens"
      },
      "local": {
        "token_dir": "~/messages/config/gcal-tokens/"
      }
    }

The ``LOBSTER_INTERNAL_SECRET`` environment variable must be set for the
myownlobster backend. This secret is also configured on the myownlobster.ai
server and authenticates inter-service calls.

Token schema on disk (local cache and local backend)::

    {
        "access_token":  "<string>",
        "expires_at":    "<ISO 8601 UTC>",
        "scope":         "<space-separated scopes>",
        "refresh_token": "<string or null>"
    }

Design principles:
- Side effects (file I/O, HTTP) are isolated to dedicated functions.
- ``is_token_valid`` is a pure function (delegates to oauth.is_token_valid).
- ``get_valid_token`` composes load → check → maybe refresh → persist.
- No token values are written to logs.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from integrations.google_calendar.config import GoogleOAuthCredentials
from integrations.google_calendar.oauth import (
    OAuthError,
    TokenData,
    is_token_valid,
    refresh_access_token,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage locations
# ---------------------------------------------------------------------------

_HOME: Path = Path.home()
_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages")))
_TOKEN_DIR: Path = _MESSAGES_DIR / "config" / "gcal-tokens"
_CALENDAR_CONFIG_PATH: Path = _MESSAGES_DIR / "config" / "calendar-config.json"

# File permissions: owner read+write only (octal 0o600)
_TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR

# HTTP timeout for internal API calls (seconds)
_HTTP_TIMEOUT: int = 10


# ---------------------------------------------------------------------------
# Calendar config loader (pure function once file is read)
# ---------------------------------------------------------------------------


def _load_calendar_config() -> dict:
    """Load the calendar config file.

    Returns the parsed dict, or a default ``local`` config if the file is
    absent or malformed.
    """
    if not _CALENDAR_CONFIG_PATH.exists():
        return {"oauth_backend": "local"}
    try:
        return json.loads(_CALENDAR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to parse calendar-config.json: %s — defaulting to local backend", exc)
        return {"oauth_backend": "local"}


# ---------------------------------------------------------------------------
# Serialisation helpers (pure functions)
# ---------------------------------------------------------------------------


def _token_to_dict(token: TokenData) -> dict:
    """Convert a TokenData to a JSON-serialisable dict."""
    return {
        "access_token": token.access_token,
        "expires_at": token.expires_at.isoformat(),
        "scope": token.scope,
        "refresh_token": token.refresh_token,
    }


def _dict_to_token(data: dict) -> TokenData:
    """Reconstruct a TokenData from a deserialised JSON dict."""
    expires_at = datetime.fromisoformat(data["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return TokenData(
        access_token=data["access_token"],
        expires_at=expires_at,
        scope=data.get("scope", ""),
        refresh_token=data.get("refresh_token"),
    )


def _token_path(user_id: str, token_dir: Path = _TOKEN_DIR) -> Path:
    """Return the absolute path to a user's local token cache file.

    Pure function: no filesystem access.
    """
    safe_id = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    if not safe_id:
        raise ValueError(f"user_id {user_id!r} produces an empty filename after sanitisation")
    return token_dir / f"{safe_id}.json"


# ---------------------------------------------------------------------------
# Local file backend (side-effecting)
# ---------------------------------------------------------------------------


def _save_token_local(
    user_id: str,
    token: TokenData,
    token_dir: Path = _TOKEN_DIR,
) -> None:
    """Persist a user's OAuth token to a local JSON file (mode 600)."""
    token_dir.mkdir(parents=True, exist_ok=True)
    path = _token_path(user_id, token_dir)
    payload = json.dumps(_token_to_dict(token), indent=2)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.rename(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    log.info("Token cached locally for user_id=%r at %s", user_id, path)


def _load_token_local(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
) -> Optional[TokenData]:
    """Load a user's token from the local cache file."""
    path = _token_path(user_id, token_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _dict_to_token(data)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("Failed to parse local token file for user_id=%r: %s", user_id, exc)
        return None


# ---------------------------------------------------------------------------
# myownlobster backend (side-effecting)
# ---------------------------------------------------------------------------


def _internal_auth_header() -> dict[str, str]:
    """Return the Authorization header for the internal API call.

    Reads LOBSTER_INTERNAL_SECRET from the environment.
    """
    secret = os.environ.get("LOBSTER_INTERNAL_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "LOBSTER_INTERNAL_SECRET is not set in the environment. "
            "Add it to config.env to enable the myownlobster calendar backend."
        )
    return {"Authorization": f"Bearer {secret}"}


def _fetch_token_from_api(
    user_id: str,
    api_base: str,
    token_endpoint: str,
) -> Optional[TokenData]:
    """Fetch a Google Calendar token from the myownlobster internal API.

    Args:
        user_id:        Telegram chat_id as a string.
        api_base:       Base URL, e.g. ``https://myownlobster.ai``.
        token_endpoint: Path, e.g. ``/api/internal/calendar-tokens``.

    Returns:
        TokenData if found, None if 404 or on any error.
    """
    url = f"{api_base.rstrip('/')}{token_endpoint}"
    try:
        headers = _internal_auth_header()
    except RuntimeError as exc:
        log.error("myownlobster backend: %s", exc)
        return None

    try:
        resp = requests.get(
            url,
            params={"telegram_chat_id": user_id},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("myownlobster API unreachable: %s", exc)
        return None

    if resp.status_code == 404:
        log.info("No token in myownlobster DB for user_id=%r", user_id)
        return None

    if not resp.ok:
        log.warning(
            "myownlobster API returned %d for user_id=%r",
            resp.status_code, user_id,
        )
        return None

    try:
        data = resp.json()
        return _dict_to_token(data)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("Failed to parse myownlobster API response: %s", exc)
        return None


def _write_token_to_api(
    user_id: str,
    token: TokenData,
    api_base: str,
    token_endpoint: str,
) -> None:
    """Write a refreshed access token back to the myownlobster internal API.

    Only updates access_token and expires_at — the refresh_token is managed
    by the OAuth flow and not overwritten here.

    Failures are logged but not raised so callers are not blocked.
    """
    url = f"{api_base.rstrip('/')}{token_endpoint}"
    try:
        headers = _internal_auth_header()
    except RuntimeError as exc:
        log.error("myownlobster backend write: %s", exc)
        return

    payload = {
        "telegram_chat_id": user_id,
        "access_token": token.access_token,
        "expires_at": token.expires_at.isoformat(),
    }

    try:
        resp = requests.put(
            url,
            json=payload,
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
        if resp.ok:
            log.info("Refreshed token written back to myownlobster for user_id=%r", user_id)
        else:
            log.warning(
                "myownlobster token write-back returned %d for user_id=%r",
                resp.status_code, user_id,
            )
    except requests.exceptions.RequestException as exc:
        log.warning("myownlobster token write-back failed for user_id=%r: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_token(
    user_id: str,
    token: TokenData,
    token_dir: Path = _TOKEN_DIR,
) -> None:
    """Persist a user's OAuth token.

    Saves to local file regardless of backend (the local file is always used
    as a fast cache).  When the myownlobster backend is active, also writes
    back to the API.

    Args:
        user_id:   Unique identifier for the user (Telegram chat_id as str).
        token:     TokenData to persist.
        token_dir: Local token cache directory.
    """
    _save_token_local(user_id, token, token_dir)

    config = _load_calendar_config()
    if config.get("oauth_backend") == "myownlobster":
        mol_cfg = config.get("myownlobster", {})
        api_base = mol_cfg.get("api_base", "https://myownlobster.ai")
        token_endpoint = mol_cfg.get("token_endpoint", "/api/internal/calendar-tokens")
        _write_token_to_api(user_id, token, api_base, token_endpoint)


def load_token(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
) -> Optional[TokenData]:
    """Load a user's OAuth token from the configured backend.

    Strategy:
    1. Check local cache first (avoids unnecessary API round-trips).
    2. If cache miss or token is expired, fetch from the configured backend.
    3. If backend returns a valid token, cache it locally and return it.

    Args:
        user_id:   Unique identifier for the user.
        token_dir: Local token cache directory.

    Returns:
        TokenData if a valid token exists, else None.
    """
    config = _load_calendar_config()
    backend = config.get("oauth_backend", "local")

    # --- Local backend: just read the file ---
    if backend == "local":
        local_cfg = config.get("local", {})
        raw_dir = local_cfg.get("token_dir", str(_TOKEN_DIR))
        resolved_dir = Path(os.path.expanduser(raw_dir))
        return _load_token_local(user_id, resolved_dir)

    # --- myownlobster backend ---
    # Check local cache first to avoid an API round-trip on every call.
    cached = _load_token_local(user_id, token_dir)
    if cached is not None and is_token_valid(cached):
        log.debug("Token for user_id=%r served from local cache", user_id)
        return cached

    mol_cfg = config.get("myownlobster", {})
    api_base = mol_cfg.get("api_base", "https://myownlobster.ai")
    token_endpoint = mol_cfg.get("token_endpoint", "/api/internal/calendar-tokens")

    log.info("Fetching token from myownlobster API for user_id=%r", user_id)
    token = _fetch_token_from_api(user_id, api_base, token_endpoint)

    if token is not None:
        # Cache it locally so the next call is fast
        _save_token_local(user_id, token, token_dir)

    return token


def get_valid_token(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
    credentials: Optional[GoogleOAuthCredentials] = None,
) -> Optional[TokenData]:
    """Return a valid access token for the user, refreshing if necessary.

    Workflow:
    1. Load token via the configured backend (with local cache).
    2. If no token → return None.
    3. If token is still valid → return it.
    4. If token is expired → attempt refresh using the stored refresh_token.
    5. Persist the refreshed token (local cache + backend write-back).
    6. If refresh fails → log and return None.

    Args:
        user_id:     Unique identifier for the user (Telegram chat_id as str).
        token_dir:   Local token cache directory.
        credentials: Optional pre-loaded Google credentials.  Passed to
                     ``refresh_access_token`` when a refresh is needed.

    Returns:
        A valid TokenData, or None if no valid token is available.
    """
    token = load_token(user_id, token_dir)
    if token is None:
        return None

    if is_token_valid(token):
        return token

    # Token is expired — attempt refresh
    if token.refresh_token is None:
        log.warning(
            "Token for user_id=%r is expired and has no refresh_token; "
            "user must re-authenticate.",
            user_id,
        )
        return None

    log.info("Access token expired for user_id=%r — attempting refresh.", user_id)

    try:
        refreshed = refresh_access_token(
            refresh_token=token.refresh_token,
            credentials=credentials,
        )
    except OAuthError as exc:
        log.error(
            "Token refresh failed for user_id=%r: %s — user must re-authenticate.",
            user_id, exc,
        )
        return None

    # Google may not return a new refresh_token on every refresh.
    if refreshed.refresh_token is None:
        refreshed = TokenData(
            access_token=refreshed.access_token,
            expires_at=refreshed.expires_at,
            scope=refreshed.scope,
            refresh_token=token.refresh_token,
        )

    # Persist: local cache + backend write-back
    save_token(user_id, refreshed, token_dir)
    return refreshed
