"""
Atomic message claim and dispatcher lock — SQLite-backed.

Replaces filesystem rename() as the claim arbiter with SQLite INSERT OR FAIL
on a UNIQUE PRIMARY KEY. Two concurrent callers: one commits, one gets
IntegrityError and returns False — no agent spawned, no duplicate delivery.

All tables live in the existing agent_sessions.db so no new DB file is needed.

Design:
- Pure functions for queries; side effects isolated to write operations
- WAL mode (inherited from the session_store connection) enables concurrent
  reads without blocking writes
- Thread-safe: sqlite3 connections are created with check_same_thread=False
  and WAL mode serializes concurrent writes at the database level
- Graceful degradation: open_claims_db() is idempotent, auto-creates tables
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("lobster-mcp")

# ---------------------------------------------------------------------------
# DB path resolution — same convention as session_store.py
# ---------------------------------------------------------------------------

_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_DEFAULT_DB_PATH = _MESSAGES_DIR / "config" / "agent_sessions.db"

# Module-level connection cache — one connection per resolved DB path.
# Keyed by resolved path to support test overrides (same pattern as session_store.py).
_connections: dict[str, sqlite3.Connection] = {}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_MESSAGE_CLAIMS = """
CREATE TABLE IF NOT EXISTS message_claims (
    message_id  TEXT PRIMARY KEY,
    claimed_by  TEXT NOT NULL,
    claimed_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'processing'
);
"""
# status values: 'processing' | 'processed' | 'failed'

_SCHEMA_DISPATCHER_LOCK = """
CREATE TABLE IF NOT EXISTS dispatcher_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    session_id  TEXT NOT NULL,
    locked_at   TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _get_connection(path: Path) -> sqlite3.Connection:
    """Return (and cache) a WAL-mode sqlite3 connection for path."""
    key = str(path.resolve())
    if key not in _connections:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _connections[key] = conn
    return _connections[key]


def _close_connection(path: Path) -> None:
    """Close and remove cached connection for path. Used in tests."""
    key = str(path.resolve())
    if key in _connections:
        try:
            _connections[key].close()
        except Exception:
            pass
        del _connections[key]


# ---------------------------------------------------------------------------
# Public: AtomicClaimDB
# ---------------------------------------------------------------------------

class AtomicClaimDB:
    """Thin wrapper around SQLite claim operations.

    Instantiate with a DB path (defaults to agent_sessions.db).
    All methods are synchronous — safe to call from async contexts
    (local SQLite writes are <1ms).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _DEFAULT_DB_PATH
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        return _get_connection(self._path)

    def _ensure_schema(self) -> None:
        """Create claim tables if they do not already exist. Idempotent."""
        try:
            conn = self._conn()
            conn.executescript(_SCHEMA_MESSAGE_CLAIMS)
            conn.executescript(_SCHEMA_DISPATCHER_LOCK)
            conn.commit()
        except Exception as exc:
            log.warning(f"[claims] Schema init failed (non-fatal): {exc}")

    # ------------------------------------------------------------------
    # Message claim operations
    # ------------------------------------------------------------------

    def claim(self, message_id: str, session_id: str = "dispatcher") -> bool:
        """Attempt to claim message_id for session_id.

        Returns True if the claim was granted (INSERT succeeded).
        Returns False if the message is already claimed (IntegrityError).

        Thread-safe: SQLite serializes concurrent INSERTs under WAL mode.
        Two callers racing on the same message_id: one wins, one returns False.
        """
        try:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT INTO message_claims (message_id, claimed_by, claimed_at) "
                    "VALUES (?, ?, ?)",
                    (
                        message_id,
                        session_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False
        except Exception as exc:
            # Log but allow the caller to proceed — degrades to old behaviour
            # rather than blocking all message processing on a DB error.
            log.warning(f"[claims] claim() failed unexpectedly for {message_id!r}: {exc}")
            return True  # fail-open: allow the rename to proceed

    def release(self, message_id: str) -> None:
        """Delete the claim row for message_id (used by stale recovery)."""
        try:
            conn = self._conn()
            with conn:
                conn.execute(
                    "DELETE FROM message_claims WHERE message_id=?",
                    (message_id,),
                )
        except Exception as exc:
            log.warning(f"[claims] release() failed for {message_id!r}: {exc}")

    def update_status(self, message_id: str, status: str) -> None:
        """Update claim status to 'processed' or 'failed'.

        No-op if the row does not exist (e.g. old messages claimed before
        this migration ran).
        """
        try:
            conn = self._conn()
            with conn:
                conn.execute(
                    "UPDATE message_claims SET status=? WHERE message_id=?",
                    (status, message_id),
                )
        except Exception as exc:
            log.warning(
                f"[claims] update_status({status!r}) failed for {message_id!r}: {exc}"
            )

    def is_claimed(self, message_id: str) -> bool:
        """Return True if an active claim row exists for message_id."""
        try:
            conn = self._conn()
            row = conn.execute(
                "SELECT 1 FROM message_claims WHERE message_id=?",
                (message_id,),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Dispatcher lock operations (Phase 2)
    # ------------------------------------------------------------------

    def acquire_dispatcher_lock(self, session_id: str) -> bool:
        """Attempt to take the single-dispatcher lock for session_id.

        Uses INSERT OR REPLACE so that:
        - If no lock exists: inserts a new row and returns True.
        - If the lock is held by a *different* active session: returns False.
        - If the lock is stale (held by a session that is no longer active):
          replaces it and returns True.

        "Active" is determined by the caller — pass the current HTTP session ID.
        This method does not know about session liveness; liveness checks belong
        in inbox_server.py where the session manager is available.

        For Phase 2 enforcement, inbox_server calls check_dispatcher_lock() first
        to determine if an existing lock is stale, then calls this method.
        """
        try:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO dispatcher_lock (id, session_id, locked_at) "
                    "VALUES (1, ?, ?)",
                    (session_id, datetime.now(timezone.utc).isoformat()),
                )
            return True
        except Exception as exc:
            log.warning(f"[claims] acquire_dispatcher_lock() failed: {exc}")
            return True  # fail-open

    def get_dispatcher_lock(self) -> dict | None:
        """Return the current lock row as a dict, or None if no lock exists."""
        try:
            conn = self._conn()
            row = conn.execute(
                "SELECT session_id, locked_at FROM dispatcher_lock WHERE id=1"
            ).fetchone()
            if row is None:
                return None
            return {"session_id": row["session_id"], "locked_at": row["locked_at"]}
        except Exception:
            return None

    def release_dispatcher_lock(self, session_id: str) -> None:
        """Release the dispatcher lock if held by session_id."""
        try:
            conn = self._conn()
            with conn:
                conn.execute(
                    "DELETE FROM dispatcher_lock WHERE id=1 AND session_id=?",
                    (session_id,),
                )
        except Exception as exc:
            log.warning(f"[claims] release_dispatcher_lock() failed: {exc}")

    def force_replace_dispatcher_lock(self, session_id: str) -> None:
        """Unconditionally replace the dispatcher lock (stale lock takeover)."""
        try:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO dispatcher_lock (id, session_id, locked_at) "
                    "VALUES (1, ?, ?)",
                    (session_id, datetime.now(timezone.utc).isoformat()),
                )
        except Exception as exc:
            log.warning(f"[claims] force_replace_dispatcher_lock() failed: {exc}")
