"""Bisque Wire Protocol v2 -- token store, bootstrap exchange, session management."""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("lobster-bisque-relay")

# Default session TTL: 7 days
_DEFAULT_SESSION_TTL = 7 * 24 * 60 * 60


class TokenStore:
    """Manages bootstrap tokens (from bisque-chat) and in-memory session tokens.

    Bootstrap tokens are read from disk (the bisque-chat token file).
    Session tokens are generated and held in memory only — on server restart
    clients must re-authenticate.
    """

    def __init__(self, tokens_file: Path, session_ttl: float = _DEFAULT_SESSION_TTL) -> None:
        self._tokens_file = tokens_file
        self._session_ttl = session_ttl
        # session_token -> {email, created_at, last_seen}
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Bootstrap tokens (disk-backed, one-time use)
    # ------------------------------------------------------------------

    def _read_bootstrap_tokens(self) -> dict[str, Any]:
        """Read bootstrap tokens from the bisque-chat token file."""
        try:
            raw = self._tokens_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data.get("bootstrapTokens", {})
        except FileNotFoundError:
            log.warning("Token file not found: %s", self._tokens_file)
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Error reading token file: %s", exc)
            return {}

    def _write_token_store(self, store: dict[str, Any]) -> None:
        """Write the full token store back to disk (to consume bootstrap tokens)."""
        try:
            tmp = self._tokens_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
            tmp.rename(self._tokens_file)
        except OSError as exc:
            log.error("Error writing token file: %s", exc)

    def validate_bootstrap_token(self, token: str) -> tuple[bool, str]:
        """Validate and consume a bootstrap token.

        Returns (True, email) on success, (False, "") on failure.
        The token is consumed (deleted from disk) on successful validation.
        """
        if not token:
            return False, ""

        try:
            raw = self._tokens_file.read_text(encoding="utf-8")
            store = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False, ""

        bootstrap = store.get("bootstrapTokens", {})
        record = bootstrap.get(token)
        if not record:
            return False, ""

        email = record.get("email", "")
        if not email:
            return False, ""

        # Consume the token
        del bootstrap[token]
        store["bootstrapTokens"] = bootstrap
        self._write_token_store(store)

        log.info("Bootstrap token consumed for %s", email)
        return True, email

    # ------------------------------------------------------------------
    # Session tokens (in-memory)
    # ------------------------------------------------------------------

    def create_session(self, email: str) -> str:
        """Create a new session token for the given email."""
        token = secrets.token_urlsafe(48)
        now = time.time()
        self._sessions[token] = {
            "email": email,
            "created_at": now,
            "last_seen": now,
        }
        log.info("Session created for %s", email)
        return token

    def validate_session(self, token: str) -> tuple[bool, str]:
        """Validate a session token.

        Returns (True, email) if valid and not expired, (False, "") otherwise.
        """
        if not token:
            return False, ""

        session = self._sessions.get(token)
        if not session:
            return False, ""

        # Check TTL
        if time.time() - session["last_seen"] > self._session_ttl:
            del self._sessions[token]
            return False, ""

        return True, session["email"]

    def touch_session(self, token: str) -> None:
        """Update last_seen timestamp for a session."""
        session = self._sessions.get(token)
        if session:
            session["last_seen"] = time.time()

    def revoke_session(self, token: str) -> None:
        """Revoke (delete) a session."""
        self._sessions.pop(token, None)

    def cleanup_expired(self) -> int:
        """Remove all expired sessions. Returns count of removed sessions."""
        now = time.time()
        expired = [
            tok for tok, sess in self._sessions.items()
            if now - sess["last_seen"] > self._session_ttl
        ]
        for tok in expired:
            del self._sessions[tok]
        return len(expired)

    @property
    def active_session_count(self) -> int:
        """Number of active (non-expired) sessions."""
        return len(self._sessions)


def handle_auth_exchange(body: dict[str, Any], store: TokenStore) -> tuple[int, dict[str, Any]]:
    """Handle the HTTP POST /auth/exchange endpoint.

    Takes a bootstrap token and returns a session token.

    Returns (status_code, response_dict).
    """
    bootstrap_token = body.get("token", "")
    if not bootstrap_token:
        return 400, {"error": "Missing 'token' field"}

    valid, email = store.validate_bootstrap_token(bootstrap_token)
    if not valid:
        return 401, {"error": "Invalid or expired bootstrap token"}

    session_token = store.create_session(email)
    return 200, {
        "sessionToken": session_token,
        "email": email,
    }
