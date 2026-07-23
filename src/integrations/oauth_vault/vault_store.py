"""
Generic per-user OAuth token persistence, parameterized by an explicit
``token_dir`` (BIS-732 / Slice 4).

This module is the storage layer proven identically three times in
``google_calendar/token_store.py``, ``gmail/token_store.py``, and
``google_workspace/token_store.py`` — same on-disk JSON schema, same
atomic-write-then-rename, same 0600 file permissions — with the one
per-integration difference (the token directory) taken as an explicit
parameter instead of a module-level constant.

This module has NO knowledge of "providers" as a registry concept — that
lives in ``oauth_vault.client``, which resolves a provider name to a
``token_dir`` and calls into this module. Keeping this module provider-
agnostic means it is trivially testable and reusable for any future token
kind (not just Google OAuth), since it depends only on the shared
``TokenData`` shape.

Token schema on disk (unchanged from the three predecessors)::

    {
        "access_token":  "<string>",
        "expires_at":    "<ISO 8601 UTC>",
        "scope":         "<space-separated scopes>",
        "refresh_token": "<string or null>"
    }

Design principles
-----------------
- Side effects (file I/O) are isolated to dedicated private/public functions.
- Serialization helpers (``_token_to_dict`` / ``_dict_to_token``) are pure.
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

from integrations.google_calendar.oauth import TokenData

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage locations
# ---------------------------------------------------------------------------

_HOME: Path = Path.home()
_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages")))

#: Root directory for providers that have no pre-existing legacy token
#: directory to alias to. New providers (not yet migrated from a bespoke
#: token_store.py) should live under here as
#: ``oauth-tokens/<provider>/<chat_id>.json``. Providers being cut over from
#: an existing bespoke store (e.g. "calendar" in this slice) instead alias
#: their default token_dir to their existing legacy directory — see
#: ``oauth_vault.client.PROVIDERS`` — so the read path introduced here and
#: the not-yet-migrated write path (myownlobster.ai push endpoints,
#: callback_server.py) keep agreeing on where a real token lives.
VAULT_ROOT: Path = _MESSAGES_DIR / "config" / "oauth-tokens"

# File permissions: owner read+write only (octal 0o600)
_TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR


def provider_token_dir(provider: str, vault_root: Path = VAULT_ROOT) -> Path:
    """Return the default fresh-start token directory for a provider.

    Only meaningful for providers with no legacy directory to alias to.
    Providers cut over from an existing bespoke token_store.py should pass
    their own explicit ``token_dir`` instead (see ``oauth_vault.client``).
    """
    return vault_root / provider


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


def _token_path(user_id: str, token_dir: Path) -> Path:
    """Return the absolute path to a user's token file.

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


def save_token(user_id: str, token: TokenData, token_dir: Path) -> None:
    """Persist a user's OAuth token to a local JSON file (mode 0o600).

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
    log.info("oauth_vault: token saved for user_id=%r at %s", user_id, path)


def load_token(user_id: str, token_dir: Path) -> Optional[TokenData]:
    """Load a user's token from the local JSON file.

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
        log.warning("oauth_vault: failed to parse token file for user_id=%r: %s", user_id, exc)
        return None
