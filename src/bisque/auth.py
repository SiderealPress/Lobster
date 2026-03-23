"""Bisque Wire Protocol v2 -- token store, bootstrap exchange, session management."""

from __future__ import annotations

import fcntl
import json
import logging
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("lobster-bisque-relay")


@contextmanager
def _locked_file(path: Path, mode: str = "r+"):
    """Open a file with an exclusive flock, creating it if needed.

    P1.2: Prevents concurrent writes from corrupting the token store.
    The lock is held for the duration of the context and released on exit.
    Falls back silently on platforms that do not support fcntl.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the file if it does not exist (needed before first write)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    with path.open(mode, encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
        except (OSError, AttributeError):
            pass  # Non-POSIX platforms — best-effort
        try:
            yield fh
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except (OSError, AttributeError):
                pass

# Default session TTL: 365 days (long-lived — avoids constant re-auth)
_DEFAULT_SESSION_TTL = 365 * 24 * 60 * 60


class TokenStore:
    """Manages bootstrap tokens (from bisque-chat) and disk-persisted session tokens.

    Bootstrap tokens are read from disk (the bisque-chat token file).
    Session tokens are persisted under the ``sessionTokens`` key of the same file
    so that relay restarts do not invalidate existing authenticated sessions.
    """

    def __init__(self, tokens_file: Path, session_ttl: float = _DEFAULT_SESSION_TTL) -> None:
        self._tokens_file = tokens_file
        self._session_ttl = session_ttl
        # session_token -> {email, created_at, last_seen}
        self._sessions: dict[str, dict[str, Any]] = {}
        # P3.7: Debounced session persistence — dirty flag + 5s flush timer
        self._dirty = False
        self._flush_timer: threading.Timer | None = None
        self._flush_lock = threading.Lock()
        self._load_sessions()

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
        """Write the full token store back to disk atomically.

        P1.2: Caller must already hold _locked_file or equivalent exclusive lock.
        Uses write-to-temp + rename for atomicity.
        """
        try:
            tmp = self._tokens_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
            tmp.rename(self._tokens_file)
        except OSError as exc:
            log.error("Error writing token file: %s", exc)

    # ------------------------------------------------------------------
    # Session persistence helpers
    # ------------------------------------------------------------------

    def _load_sessions(self) -> None:
        """Load persisted session tokens from disk into memory.

        Reads the ``sessionTokens`` dict from the token file and populates
        ``self._sessions``, discarding any entries that are already expired
        so stale sessions don't accumulate across restarts.
        """
        try:
            raw = self._tokens_file.read_text(encoding="utf-8")
            store = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            log.warning("Could not load sessions from disk: %s", exc)
            return

        now = time.time()
        loaded = 0
        skipped = 0
        for tok, sess in store.get("sessionTokens", {}).items():
            # Only accept entries that have the fields we write
            if not isinstance(sess, dict) or "email" not in sess or "last_seen" not in sess:
                skipped += 1
                continue
            # Discard already-expired sessions
            if now - sess["last_seen"] > self._session_ttl:
                skipped += 1
                continue
            self._sessions[tok] = {
                "email": sess["email"],
                "created_at": sess.get("created_at", now),
                "last_seen": sess["last_seen"],
            }
            loaded += 1

        log.info("Loaded %d active session(s) from disk (%d expired/skipped)", loaded, skipped)

    def _persist_sessions(self) -> None:
        """Write the current in-memory session map back to the token file.

        P1.2 + P3.7: Uses an exclusive flock to prevent concurrent writes from
        corrupting the store. Merges with existing file content so bootstrap
        tokens are not lost.

        Callers that trigger this on every connect (e.g., touch_session) should
        use _schedule_persist() instead for debounced writes.
        """
        try:
            with _locked_file(self._tokens_file) as fh:
                fh.seek(0)
                try:
                    store: dict[str, Any] = json.loads(fh.read())
                except (json.JSONDecodeError, ValueError):
                    store = {}

                store["sessionTokens"] = {
                    tok: {
                        "email": sess["email"],
                        "created_at": sess["created_at"],
                        "last_seen": sess["last_seen"],
                    }
                    for tok, sess in self._sessions.items()
                }
                self._write_token_store(store)
        except OSError as exc:
            log.error("Error persisting sessions: %s", exc)

    def validate_bootstrap_token(self, token: str) -> tuple[bool, str]:
        """Validate and consume a bootstrap token.

        P1.2: Holds an exclusive flock for the entire read-validate-delete-write
        cycle to prevent concurrent exchanges from consuming the same token twice.

        P1.4: Handles both the canonical schema (createdAt/expiresAt ISO strings,
        used flag) and the legacy schema (created_at float, no expiry field).

        Returns (True, email) on success, (False, "") on failure.
        The token is consumed (deleted from disk) on successful validation.
        """
        from datetime import datetime, timezone

        if not token:
            return False, ""

        try:
            with _locked_file(self._tokens_file) as fh:
                fh.seek(0)
                try:
                    store = json.loads(fh.read())
                except (json.JSONDecodeError, ValueError):
                    return False, ""

                bootstrap = store.get("bootstrapTokens", {})
                record = bootstrap.get(token)
                if not record:
                    return False, ""

                email = record.get("email", "")
                if not email:
                    return False, ""

                # P1.4: Check used flag (canonical schema)
                if record.get("used", False):
                    log.warning("Bootstrap token already used for %s", email)
                    return False, ""

                # P1.4: Check expiry — canonical schema uses expiresAt (ISO string);
                # legacy schema has no expiry field (treat as valid).
                expires_at_raw = record.get("expiresAt")
                if expires_at_raw:
                    try:
                        expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
                        if datetime.now(timezone.utc) > expires_at:
                            log.warning("Bootstrap token expired for %s (expired %s)", email, expires_at_raw)
                            return False, ""
                    except (ValueError, TypeError) as exc:
                        log.warning("Could not parse expiresAt for token: %s", exc)
                        # Treat unparseable expiry as invalid for security
                        return False, ""

                # Consume the token atomically (while lock is held)
                del bootstrap[token]
                store["bootstrapTokens"] = bootstrap
                self._write_token_store(store)

                log.info("Bootstrap token consumed for %s", email)
                return True, email

        except OSError as exc:
            log.error("Error validating bootstrap token: %s", exc)
            return False, ""

    # ------------------------------------------------------------------
    # Session tokens (in-memory)
    # ------------------------------------------------------------------

    def create_session(self, email: str) -> str:
        """Create a new session token for the given email and persist it to disk."""
        token = secrets.token_urlsafe(48)
        now = time.time()
        self._sessions[token] = {
            "email": email,
            "created_at": now,
            "last_seen": now,
        }
        log.info("Session created for %s", email)
        self._persist_sessions()
        return token

    def validate_session(self, token: str) -> tuple[bool, str]:
        """Validate a session token.

        Returns (True, email) if valid and not expired, (False, "") otherwise.
        Expired sessions are removed from memory and disk on detection.
        """
        if not token:
            return False, ""

        session = self._sessions.get(token)
        if not session:
            return False, ""

        # Check TTL
        if time.time() - session["last_seen"] > self._session_ttl:
            del self._sessions[token]
            self._persist_sessions()
            return False, ""

        return True, session["email"]

    # ------------------------------------------------------------------
    # P3.7: Debounced session persistence
    # ------------------------------------------------------------------

    def _schedule_persist(self, delay: float = 5.0) -> None:
        """Schedule a debounced session persist.

        If called repeatedly within `delay` seconds, only one disk write occurs.
        This prevents a burst of touch_session calls (e.g. reconnect storm) from
        causing a burst of synchronous file I/O.
        """
        with self._flush_lock:
            self._dirty = True
            if self._flush_timer is not None:
                self._flush_timer.cancel()
            self._flush_timer = threading.Timer(delay, self._flush_if_dirty)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def _flush_if_dirty(self) -> None:
        """Flush pending session changes to disk if dirty."""
        with self._flush_lock:
            if not self._dirty:
                return
            self._dirty = False
            self._flush_timer = None
        try:
            self._persist_sessions()
            log.debug("Flushed session store to disk (debounced)")
        except Exception as exc:
            log.error("Debounced session flush failed: %s", exc)

    def touch_session(self, token: str) -> None:
        """Update last_seen timestamp for a session.

        P3.7: Uses debounced write — schedules a persist in 5s rather than
        writing synchronously on every WebSocket connection.
        """
        session = self._sessions.get(token)
        if session:
            session["last_seen"] = time.time()
            self._schedule_persist()

    def revoke_session(self, token: str) -> None:
        """Revoke (delete) a session and remove it from disk."""
        self._sessions.pop(token, None)
        self._persist_sessions()

    def cleanup_expired(self) -> int:
        """Remove all expired sessions and persist the result. Returns count removed."""
        now = time.time()
        expired = [
            tok for tok, sess in self._sessions.items()
            if now - sess["last_seen"] > self._session_ttl
        ]
        for tok in expired:
            del self._sessions[tok]
        if expired:
            self._persist_sessions()
        return len(expired)

    @property
    def active_session_count(self) -> int:
        """Number of active (non-expired) sessions."""
        return len(self._sessions)


def create_bootstrap_token(email: str, store: TokenStore, ttl_seconds: float = 24 * 60 * 60) -> str:
    """Create and persist a one-time bootstrap token for the given email.

    P1.4: Writes the canonical schema shared with bisque-chat TypeScript:
      {email, createdAt (ISO string), expiresAt (ISO string), used: false}

    The token is written to the token file (bootstrapTokens dict) so the relay
    can issue it independently of the bisque-chat Next.js app.

    Returns the raw bootstrap token string.
    """
    from datetime import datetime, timezone

    token = secrets.token_urlsafe(32)
    now_dt = datetime.now(timezone.utc)
    created_at = now_dt.isoformat()
    expires_at = datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc).isoformat()

    # P1.2: Hold exclusive lock while writing to prevent concurrent corruption
    with _locked_file(store._tokens_file) as fh:
        fh.seek(0)
        try:
            store_data: dict[str, Any] = json.loads(fh.read())
        except (json.JSONDecodeError, ValueError):
            store_data = {}

        bootstrap = store_data.setdefault("bootstrapTokens", {})
        bootstrap[token] = {
            "email": email,
            "createdAt": created_at,
            "expiresAt": expires_at,
            "used": False,
        }
        store._write_token_store(store_data)

    log.info("Bootstrap token created for %s (expires %s)", email, expires_at)
    return token


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
