"""
Atomic message claim operations backed by SQLite.

Two concurrent callers attempting to claim the same message_id will race on the
INSERT.  SQLite serializes writes; the loser gets IntegrityError and must not
proceed.  The winner moves the message file as a consequence of a won claim.

DB path: ~/lobster-workspace/data/message-claims.db  (separate from agent_sessions.db
so it can be schema-evolved independently and keeps the hot write path isolated).

Schema
------
message_claims
    message_id  TEXT PRIMARY KEY  — enforces the exclusive-ownership invariant
    claimed_at  TEXT NOT NULL     — ISO-8601 UTC timestamp
    session_id  TEXT              — HTTP session that won the claim (may be NULL)

dispatcher_lock
    id          INTEGER PRIMARY KEY CHECK (id = 1)  — single-row constraint
    session_id  TEXT NOT NULL
    locked_at   TEXT NOT NULL

Public API
----------
claim_message(message_id, session_id=None) -> bool
    True  — this caller won the claim (INSERT committed)
    False — another caller already holds the claim (IntegrityError)

release_claim(message_id) -> None
    Delete the claim row so the message can be re-claimed (used by stale recovery
    and mark_processed / mark_failed).

acquire_dispatcher_lock(session_id) -> bool
    True  — lock acquired (INSERT OR REPLACE committed successfully and this
            session_id now appears in the lock row)
    False — a different session already holds the lock

release_dispatcher_lock(session_id) -> None
    Delete the lock row only if this session_id currently holds it.

is_dispatcher_locked_by(session_id) -> bool
    True if the given session_id currently holds the dispatcher lock.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import os
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "message-claims.db"


# Module-level connection cache — one connection per DB path per process.
_connections: dict[str, sqlite3.Connection] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_claims (
    message_id  TEXT PRIMARY KEY,
    claimed_at  TEXT NOT NULL,
    session_id  TEXT
);

CREATE TABLE IF NOT EXISTS dispatcher_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    session_id  TEXT NOT NULL,
    locked_at   TEXT NOT NULL
);
"""


def _get_connection(path: Path) -> sqlite3.Connection:
    """Return (and cache) a sqlite3 connection for *path*.

    WAL journal mode allows concurrent readers while a writer holds the lock,
    which matters here because mark_processed / mark_failed reads before it
    writes.  The connection is cached at the module level: SQLite connections
    are not thread-safe by default, but all callers in inbox_server.py run in
    the same asyncio event loop on the main thread, so a single cached
    connection is correct.
    """
    key = str(path)
    if key not in _connections:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _connections[key] = conn
    return _connections[key]


# ---------------------------------------------------------------------------
# Module-level DB path (can be overridden in tests via _set_db_path)
# ---------------------------------------------------------------------------

_DB_PATH: Path | None = None


def _set_db_path(path: Path) -> None:
    """Override the DB path — used by tests to redirect writes to tmp_path."""
    global _DB_PATH, _connections
    _DB_PATH = path
    # Clear connection cache so the next call opens a fresh connection at the
    # new path rather than reusing the previous one.
    _connections = {}


def _get_db() -> sqlite3.Connection:
    path = _DB_PATH if _DB_PATH is not None else _default_db_path()
    return _get_connection(path)


# ---------------------------------------------------------------------------
# Claim operations
# ---------------------------------------------------------------------------

def claim_message(message_id: str, session_id: str | None = None) -> bool:
    """Attempt to claim *message_id* for exclusive processing.

    Uses BEGIN EXCLUSIVE + INSERT to guarantee that at most one caller can
    receive True for a given message_id in the lifetime of the claim row.

    Returns
    -------
    True   — this caller won the claim
    False  — another caller already holds the claim (IntegrityError on INSERT)
    """
    db = _get_db()
    claimed_at = datetime.now(timezone.utc).isoformat()
    try:
        with db:  # autocommit on success, rollback on IntegrityError
            db.execute(
                "INSERT INTO message_claims (message_id, claimed_at, session_id) "
                "VALUES (?, ?, ?)",
                (message_id, claimed_at, session_id),
            )
        log.debug(f"[claims] claimed: {message_id} session={session_id}")
        return True
    except sqlite3.IntegrityError:
        log.debug(f"[claims] already_claimed: {message_id}")
        return False


def release_claim(message_id: str) -> None:
    """Release the claim on *message_id* so it can be re-claimed.

    Called by:
    - _recover_stale_processing: before moving a message back to inbox/
    - handle_mark_processed: after moving to processed/
    - handle_mark_failed: after moving to failed/

    Safe to call when no claim row exists (DELETE is a no-op).
    """
    db = _get_db()
    try:
        with db:
            db.execute(
                "DELETE FROM message_claims WHERE message_id = ?",
                (message_id,),
            )
        log.debug(f"[claims] released: {message_id}")
    except Exception as exc:
        log.warning(f"[claims] release_claim failed for {message_id}: {exc}")


# ---------------------------------------------------------------------------
# Dispatcher lock operations (Phase 2)
# ---------------------------------------------------------------------------

def acquire_dispatcher_lock(session_id: str) -> bool:
    """Attempt to acquire the dispatcher lock for *session_id*.

    Uses INSERT OR REPLACE (upsert) inside a BEGIN EXCLUSIVE transaction so
    that the check-then-insert is atomic.  A second dispatcher calling this
    while the first holds the lock will get False if a different session_id
    already holds the single-row lock.

    Returns
    -------
    True   — this session_id now holds the lock
    False  — a different session_id holds the lock
    """
    db = _get_db()
    locked_at = datetime.now(timezone.utc).isoformat()
    try:
        with db:
            # Check whether a different session already holds the lock
            row = db.execute(
                "SELECT session_id FROM dispatcher_lock WHERE id = 1"
            ).fetchone()
            if row is not None and row[0] != session_id:
                log.warning(
                    f"[claims] dispatcher_lock held by {row[0]!r}; "
                    f"refusing {session_id!r}"
                )
                return False
            # Either no lock or same session renewing — upsert
            db.execute(
                "INSERT OR REPLACE INTO dispatcher_lock (id, session_id, locked_at) "
                "VALUES (1, ?, ?)",
                (session_id, locked_at),
            )
        log.debug(f"[claims] dispatcher_lock acquired: {session_id}")
        return True
    except Exception as exc:
        log.warning(f"[claims] acquire_dispatcher_lock failed: {exc}")
        # Fail open: if the DB is unavailable, allow the dispatcher to proceed
        # so the system degrades gracefully rather than hard-blocking startup.
        return True


def release_dispatcher_lock(session_id: str) -> None:
    """Release the dispatcher lock held by *session_id*.

    No-op if *session_id* does not currently hold the lock.
    """
    db = _get_db()
    try:
        with db:
            db.execute(
                "DELETE FROM dispatcher_lock WHERE id = 1 AND session_id = ?",
                (session_id,),
            )
        log.debug(f"[claims] dispatcher_lock released: {session_id}")
    except Exception as exc:
        log.warning(f"[claims] release_dispatcher_lock failed: {exc}")


def is_dispatcher_locked_by(session_id: str) -> bool:
    """Return True if *session_id* currently holds the dispatcher lock."""
    db = _get_db()
    try:
        row = db.execute(
            "SELECT session_id FROM dispatcher_lock WHERE id = 1"
        ).fetchone()
        return row is not None and row[0] == session_id
    except Exception:
        return False
