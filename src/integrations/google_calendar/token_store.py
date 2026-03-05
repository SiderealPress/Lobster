"""
Per-user Google OAuth token persistence.

Stores and retrieves TokenData to/from JSON files in
``~/messages/config/gcal-tokens/{user_id}.json``.  Each file is mode 600
(owner read/write only) so that token values are never world-readable.

Token schema on disk::

    {
        "access_token":  "<string>",
        "expires_at":    "<ISO 8601 UTC>",
        "scope":         "<space-separated scopes>",
        "refresh_token": "<string or null>"
    }

Design principles:
- Side effects (file I/O) are isolated to ``save_token`` and ``load_token``.
- ``is_token_valid`` is a pure function (delegates to oauth.is_token_valid).
- ``get_valid_token`` composes the above: load → check → maybe refresh → save.
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

from integrations.google_calendar.config import GoogleOAuthCredentials
from integrations.google_calendar.oauth import (
    OAuthError,
    TokenData,
    is_token_valid,
    refresh_access_token,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

_HOME: Path = Path.home()
_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_TOKEN_DIR: Path = _MESSAGES_DIR / "config" / "gcal-tokens"

# File permissions: owner read+write only (octal 0o600)
_TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR


# ---------------------------------------------------------------------------
# Serialisation helpers (pure functions)
# ---------------------------------------------------------------------------


def _token_to_dict(token: TokenData) -> dict:
    """Convert a TokenData to a JSON-serialisable dict.

    Uses ISO 8601 UTC format for ``expires_at`` so the stored representation
    is unambiguous and human-readable.

    Args:
        token: Immutable TokenData to serialise.

    Returns:
        Dict ready for ``json.dumps``.
    """
    return {
        "access_token": token.access_token,
        "expires_at": token.expires_at.isoformat(),
        "scope": token.scope,
        "refresh_token": token.refresh_token,
    }


def _dict_to_token(data: dict) -> TokenData:
    """Reconstruct a TokenData from a deserialised JSON dict.

    Args:
        data: Dict from ``json.loads`` matching the token schema.

    Returns:
        Frozen TokenData instance.

    Raises:
        KeyError:  If mandatory fields are absent.
        ValueError: If ``expires_at`` is not a valid ISO datetime string.
    """
    expires_at = datetime.fromisoformat(data["expires_at"])
    # Ensure the datetime is timezone-aware (legacy files may lack tzinfo)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    return TokenData(
        access_token=data["access_token"],
        expires_at=expires_at,
        scope=data.get("scope", ""),
        refresh_token=data.get("refresh_token"),
    )


def _token_path(user_id: str, token_dir: Path = _TOKEN_DIR) -> Path:
    """Return the absolute path to a user's token file.

    This is a pure function: given the same inputs it always returns the
    same path without touching the filesystem.

    Args:
        user_id:   Identifier for the user (e.g. Telegram chat_id as str).
        token_dir: Directory that holds per-user token files.

    Returns:
        Path object for ``{token_dir}/{user_id}.json``.
    """
    # Sanitise user_id to prevent directory traversal: keep only safe chars
    safe_id = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    if not safe_id:
        raise ValueError(f"user_id {user_id!r} produces an empty filename after sanitisation")
    return token_dir / f"{safe_id}.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_token(
    user_id: str,
    token: TokenData,
    token_dir: Path = _TOKEN_DIR,
) -> None:
    """Persist a user's OAuth token to disk.

    The token file is written atomically (write to a temp file then rename)
    and set to mode 600 immediately after creation, before any data is written.

    Args:
        user_id:   Unique identifier for the user.
        token:     TokenData to persist.
        token_dir: Directory in which to store the token file.  Defaults to
                   ``~/messages/config/gcal-tokens/``.

    Side effects:
        Creates ``token_dir`` if it does not exist.
        Writes (or overwrites) ``{token_dir}/{user_id}.json``.
        Sets file permissions to 0o600.
    """
    token_dir.mkdir(parents=True, exist_ok=True)
    path = _token_path(user_id, token_dir)

    payload = json.dumps(_token_to_dict(token), indent=2)

    # Write to a sibling temp file, set permissions, then rename atomically.
    tmp_path = path.with_suffix(".json.tmp")
    try:
        # Open with restrictive permissions from the start
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.rename(str(tmp_path), str(path))
    except Exception:
        # Clean up temp file if anything goes wrong
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    log.info("Token saved for user_id=%r at %s", user_id, path)


def load_token(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
) -> Optional[TokenData]:
    """Load a user's OAuth token from disk.

    Args:
        user_id:   Unique identifier for the user.
        token_dir: Directory containing token files.

    Returns:
        TokenData if a valid token file exists, or None if:
        - No token file exists for this user.
        - The file is malformed or cannot be parsed.

    Side effects:
        Reads ``{token_dir}/{user_id}.json`` if it exists.
    """
    path = _token_path(user_id, token_dir)

    if not path.exists():
        log.debug("No token file found for user_id=%r", user_id)
        return None

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        token = _dict_to_token(data)
        log.debug("Token loaded for user_id=%r", user_id)
        return token
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning(
            "Failed to parse token file for user_id=%r (%s): %s",
            user_id, path, exc,
        )
        return None


def get_valid_token(
    user_id: str,
    token_dir: Path = _TOKEN_DIR,
    credentials: Optional[GoogleOAuthCredentials] = None,
) -> Optional[TokenData]:
    """Return a valid access token for the user, refreshing if necessary.

    Composes ``load_token``, ``is_token_valid``, ``refresh_access_token``,
    and ``save_token`` into a single convenience function:

    1. Load token from disk.
    2. If no token → return None.
    3. If token is still valid → return it.
    4. If token is expired → attempt refresh using the stored refresh_token.
    5. Save the refreshed token and return it.
    6. If refresh fails (revoked, network error) → log the error, return None.

    Args:
        user_id:     Unique identifier for the user.
        token_dir:   Directory containing token files.
        credentials: Optional pre-loaded Google credentials.  Passed through
                     to ``refresh_access_token`` if a refresh is needed.

    Returns:
        A valid TokenData, or None if no valid token is available.

    Side effects:
        May write a refreshed token back to disk via ``save_token``.
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
    # If it's absent in the refreshed response, carry forward the original.
    if refreshed.refresh_token is None:
        refreshed = TokenData(
            access_token=refreshed.access_token,
            expires_at=refreshed.expires_at,
            scope=refreshed.scope,
            refresh_token=token.refresh_token,
        )

    save_token(user_id, refreshed, token_dir)
    return refreshed
