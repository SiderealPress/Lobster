"""
Per-user Google Workspace OAuth token persistence — local-disk edition.

Tokens are stored as JSON files at:
    ~/messages/config/workspace-tokens/{user_id}.json

This module is the Google Workspace counterpart of
``integrations.gmail.token_store`` and ``integrations.google_calendar.token_store``.
The three share the same on-disk schema and the same refresh-proxy pattern;
the only differences are the token directory path and the refresh endpoint name.

Refresh flow
------------
When an access token is expired, this module calls the myownlobster.ai
``/api/internal/refresh-workspace-token`` endpoint (functionally identical to
``/api/internal/refresh-gmail-token`` — both proxy a Google OAuth refresh call
using the server-side GCP credentials).

The refresh proxy URL is read from
``~/messages/config/workspace-config.json``::

    {
      "myownlobster_api_base": "https://myownlobster.ai"
    }

If that key is absent, ``https://myownlobster.ai`` is used as a default.

Token schema on disk::

    {
        "access_token":  "<string>",
        "expires_at":    "<ISO 8601 UTC>",
        "scope":         "<space-separated scopes>",
        "refresh_token": "<string or null>"
    }

Design principles
-----------------
- Side effects (file I/O, HTTP) are isolated to dedicated private functions.
- ``is_token_valid`` is a pure function (delegates to
  ``google_calendar.oauth.is_token_valid``).
- ``get_valid_token`` composes load -> check -> maybe refresh -> persist.
- No token values are written to logs.
- Token file isolation: workspace-tokens/ is separate from gmail-tokens/ and
  gcal-tokens/; OAuth for one scope never touches the other.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from integrations.google_calendar.oauth import (
    OAuthError,
    TokenData,
    is_token_valid,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage locations
# ---------------------------------------------------------------------------

_HOME: Path = Path.home()
_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages")))
_TOKEN_DIR: Path = _MESSAGES_DIR / "config" / "workspace-tokens"
_WORKSPACE_CONFIG_PATH: Path = _MESSAGES_DIR / "config" / "workspace-config.json"

# File permissions: owner read+write only (octal 0o600)
_TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR

# HTTP timeout for refresh proxy calls (seconds)
_HTTP_TIMEOUT: int = 10

# Default refresh proxy base URL (GCP secrets live here)
_DEFAULT_API_BASE: str = "https://myownlobster.ai"
_REFRESH_ENDPOINT: str = "/api/internal/refresh-workspace-token"


# ---------------------------------------------------------------------------
# Workspace config loader
# ---------------------------------------------------------------------------


def _load_workspace_config() -> dict:
    """Return the parsed workspace-config.json, or an empty dict if absent.

    Pure-ish function: reads one file; returns a stable default on error.
    """
    if not _WORKSPACE_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_WORKSPACE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to parse workspace-config.json: %s", exc)
        return {}


def _myownlobster_api_base() -> str:
    """Return the myownlobster API base URL from config, or the default."""
    config = _load_workspace_config()
    return config.get("myownlobster_api_base", _DEFAULT_API_BASE).rstrip("/")


# ---------------------------------------------------------------------------
# Auth header helper
# ---------------------------------------------------------------------------


def _internal_auth_header() -> dict[str, str]:
    """Return the Authorization header for internal API calls.

    Reads LOBSTER_INTERNAL_SECRET from the environment.

    Raises:
        RuntimeError: If LOBSTER_INTERNAL_SECRET is not set.
    """
    secret = os.environ.get("LOBSTER_INTERNAL_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "LOBSTER_INTERNAL_SECRET is not set. "
            "Add it to config.env to enable token refresh via myownlobster."
        )
    return {"Authorization": f"Bearer {secret}"}


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
    """Return the absolute path to a user's workspace token file.

    Pure function: no filesystem access.

    Args:
        user_id:   Telegram chat_id as a string.
        token_dir: Directory holding per-user token files.

    Returns:
        Absolute Path to ``{token_dir}/{safe_user_id}.json``.

    Raises:
        ValueError: If the sanitised user_id would produce an empty filename.
    """
    safe_id = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    if not safe_id:
        raise ValueError(
            f"user_id {user_id!r} produces an empty filename after sanitisation"
        )
    return token_dir / f"{safe_id}.json"


# ---------------------------------------------------------------------------
# Local file I/O (side-effecting)
# ---------------------------------------------------------------------------


def _save_token_local(
    user_id: str,
    token: TokenData,
    token_dir: Path = _TOKEN_DIR,
) -> None:
    """Persist a user's workspace OAuth token to a local JSON file (mode 0o600).

    Uses an atomic write (write to .tmp, then rename) to avoid corruption
    if the process is interrupted mid-write.

    Args:
        user_id:   Unique identifier for the user.
        token:     TokenData to persist.
        token_dir: Directory for token files.
    """
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
    log.info("Workspace token saved locally for user_id=%r at %s", user_id, path)


def _load_token_local(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
) -> Optional[TokenData]:
    """Load a user's workspace token from the local JSON file.

    Args:
        user_id:   Unique identifier for the user.
        token_dir: Directory for token files.

    Returns:
        TokenData if the file exists and is valid JSON, else None.
    """
    path = _token_path(user_id, token_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _dict_to_token(data)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("Failed to parse local workspace token for user_id=%r: %s", user_id, exc)
        return None


# ---------------------------------------------------------------------------
# Refresh proxy (calls myownlobster.ai — side-effecting)
# ---------------------------------------------------------------------------


def _refresh_token_via_proxy(refresh_token: str) -> Optional[TokenData]:
    """Obtain a new workspace access token by calling the myownlobster refresh proxy.

    myownlobster.ai holds the GCP client_id + client_secret and proxies the
    refresh call to Google, returning only the new access_token and expires_in.

    Args:
        refresh_token: The long-lived refresh token.

    Returns:
        A new TokenData (refresh_token preserved from caller), or None on error.
    """
    api_base = _myownlobster_api_base()
    url = f"{api_base}{_REFRESH_ENDPOINT}"

    try:
        headers = _internal_auth_header()
    except RuntimeError as exc:
        log.error("Workspace token refresh proxy: %s", exc)
        return None

    try:
        resp = requests.post(
            url,
            json={"refresh_token": refresh_token},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("Workspace token refresh proxy unreachable: %s", exc)
        return None

    if not resp.ok:
        log.warning(
            "Workspace token refresh proxy returned %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        return None

    try:
        data = resp.json()
        access_token = data["access_token"]
        expires_in = int(data["expires_in"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("Workspace token refresh proxy returned unexpected payload: %s", exc)
        return None

    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

    log.info("Workspace token refresh via proxy succeeded.")
    return TokenData(
        access_token=access_token,
        expires_at=expires_at,
        scope="",  # scope not returned by refresh proxy; preserved from disk
        refresh_token=None,  # caller must preserve the original refresh_token
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_token(
    user_id: str,
    token: TokenData,
    token_dir: Path = _TOKEN_DIR,
) -> None:
    """Persist a user's Google Workspace OAuth token to local disk.

    This is the sole write path. The local file is the canonical store.

    Args:
        user_id:   Unique identifier for the user (Telegram chat_id as str).
        token:     TokenData to persist.
        token_dir: Local token directory (injectable for testing).
    """
    _save_token_local(user_id, token, token_dir)


def load_token(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
) -> Optional[TokenData]:
    """Load a user's Google Workspace OAuth token from local disk.

    Args:
        user_id:   Unique identifier for the user.
        token_dir: Local token directory (injectable for testing).

    Returns:
        TokenData if the file exists and is parseable, else None.
    """
    return _load_token_local(user_id, token_dir)


def get_valid_token(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
) -> Optional[TokenData]:
    """Return a valid Google Workspace access token for the user, refreshing if necessary.

    Workflow:
    1. Load token from local disk.
    2. If no token -> return None (user must authenticate via consent link).
    3. If token is still valid -> return it.
    4. If token is expired -> call myownlobster refresh proxy.
    5. Persist the refreshed token (preserving the original refresh_token).
    6. If refresh fails -> log and return None.

    Args:
        user_id:   Unique identifier for the user (Telegram chat_id as str).
        token_dir: Local token directory (injectable for testing).

    Returns:
        A valid TokenData, or None if no valid token is available.
    """
    token = _load_token_local(user_id, token_dir)
    if token is None:
        log.info("No local workspace token found for user_id=%r.", user_id)
        return None

    if is_token_valid(token):
        return token

    # Token is expired — attempt refresh via myownlobster proxy
    if token.refresh_token is None:
        log.warning(
            "Workspace token for user_id=%r is expired and has no refresh_token; "
            "user must re-authenticate.",
            user_id,
        )
        return None

    log.info("Workspace access token expired for user_id=%r — refreshing via proxy.", user_id)

    refreshed_partial = _refresh_token_via_proxy(token.refresh_token)
    if refreshed_partial is None:
        log.error(
            "Workspace token refresh failed for user_id=%r — user must re-authenticate.",
            user_id,
        )
        return None

    # Merge: preserve scope and refresh_token from the stored token
    refreshed = TokenData(
        access_token=refreshed_partial.access_token,
        expires_at=refreshed_partial.expires_at,
        scope=token.scope,           # preserve original scope
        refresh_token=token.refresh_token,  # Google doesn't return new refresh_token here
    )

    _save_token_local(user_id, refreshed, token_dir)
    return refreshed
