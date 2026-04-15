"""
Agent Session Store — SQLite-backed replacement for pending-agents.json

Provides a persistent, queryable store for background agent sessions.
Replaces the JSON file approach with a WAL-mode SQLite database that:
  - Survives restarts without data loss
  - Records full session history (running, completed, failed)
  - Supports concurrent read/write from multiple processes (WAL mode)
  - Performs all writes synchronously but fast (<1ms local SQLite write)

DB location: ~/messages/config/agent_sessions.db
(resolved from LOBSTER_MESSAGES env var, same convention as pending-agents.json)

Design principles:
  - Pure functions for queries; side effects isolated to write functions
  - All public functions are synchronous — safe to call from async contexts
    (SQLite local writes are <1ms; use run_in_executor if profiling shows blocking)
  - Graceful degradation: init_db() is idempotent, missing DB auto-creates
  - WAL mode eliminates reader/writer blocking across processes
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_DEFAULT_DB_PATH = _MESSAGES_DIR / "config" / "agent_sessions.db"

# Module-level connection cache (one connection per DB path per process)
# Using a dict keyed by resolved path to support test overrides.
_connections: dict[str, sqlite3.Connection] = {}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Core table DDL — used for both fresh installs and as documentation of full schema.
# On fresh installs this creates the complete table. On existing DBs, the
# CREATE TABLE IF NOT EXISTS is a no-op (existing table unchanged); the ALTER
# TABLE migrations in _MIGRATION_STMTS then add any missing columns.
_SCHEMA_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT,
    agent_type          TEXT,
    description         TEXT NOT NULL,
    chat_id             TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'telegram',
    status              TEXT NOT NULL DEFAULT 'running',
    output_file         TEXT,
    timeout_minutes     INTEGER,
    input_summary       TEXT,
    result_summary      TEXT,
    parent_id           TEXT,
    spawned_at          TEXT NOT NULL,
    completed_at        TEXT,
    last_seen_at        TEXT,
    notified_at         TEXT,
    trigger_message_id  TEXT,
    trigger_snippet     TEXT,
    reply_message_ids   TEXT,
    stop_reason         TEXT,
    idempotency         TEXT DEFAULT 'unknown',
    task_origin         TEXT DEFAULT 'user'
);
"""

# Reports table DDL — stores user-filed /report slash command records.
# Unified into agent_sessions.db to keep all operational data in one place.
_SCHEMA_REPORTS_TABLE = """
CREATE TABLE IF NOT EXISTS reports (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id               TEXT NOT NULL UNIQUE,
    description             TEXT NOT NULL,
    chat_id                 TEXT NOT NULL,
    source                  TEXT NOT NULL DEFAULT 'telegram',
    recent_messages_json    TEXT,
    agent_session_ids_json  TEXT,
    snapshot_state_json     TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    status                  TEXT NOT NULL DEFAULT 'open'
);
"""

# Reports table indexes
_SCHEMA_REPORTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_reports_chat_id   ON reports (chat_id);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_status    ON reports (status);
"""

# Indexes — run after all columns are guaranteed to exist (i.e., after migrations)
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_status ON agent_sessions (status);
CREATE INDEX IF NOT EXISTS idx_spawned_at ON agent_sessions (spawned_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_id ON agent_sessions (task_id);
CREATE INDEX IF NOT EXISTS idx_notified ON agent_sessions (notified_at);
"""

# Safe ALTER TABLE migrations for existing databases (idempotent via try/except).
# Each statement fails silently if the column already exists (fresh install).
_MIGRATION_STMTS = [
    "ALTER TABLE agent_sessions ADD COLUMN notified_at TEXT",
    "ALTER TABLE agent_sessions ADD COLUMN trigger_message_id TEXT",
    "ALTER TABLE agent_sessions ADD COLUMN trigger_snippet TEXT",
    "ALTER TABLE agent_sessions ADD COLUMN reply_message_ids TEXT",
    "ALTER TABLE agent_sessions ADD COLUMN stop_reason TEXT",
    "ALTER TABLE agent_sessions ADD COLUMN idempotency TEXT DEFAULT 'unknown'",
    "ALTER TABLE agent_sessions ADD COLUMN task_origin TEXT DEFAULT 'user'",
]

# Additive migrations for the reports table (BIS-85 multi-instance prep).
# Wrapped in try/except so they are no-ops on fresh DBs that already have the
# columns from the CREATE TABLE statement once this schema is finalised.
_REPORTS_MIGRATION_STMTS = [
    "ALTER TABLE reports ADD COLUMN instance_id TEXT",
    "ALTER TABLE reports ADD COLUMN forwarded_at TEXT",
]

_MIGRATION_JSON_PATH = _MESSAGES_DIR / "config" / "pending-agents.json"


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _get_connection(path: Path) -> sqlite3.Connection:
    """Return (and cache) a sqlite3 connection for the given DB path.

    Uses WAL journal mode for concurrent read/write without blocking.
    Sets row_factory to sqlite3.Row so callers get dict-like access.
    """
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
# Public: init
# ---------------------------------------------------------------------------

def init_db(path: Path | None = None) -> None:
    """Initialize the SQLite database and run schema migrations.

    Idempotent: safe to call multiple times. Creates the DB file and tables
    if they do not exist. Also runs safe ALTER TABLE migrations for new columns
    (each wrapped in try/except so they are no-ops on fresh DBs that already
    have the column from the CREATE TABLE statement). Also runs one-time
    migration from pending-agents.json if that file exists and has not already
    been migrated.

    Args:
        path: Override the default DB path. Primarily used in tests.
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    # Step 1: Create the table if it does not exist.
    # On fresh installs this creates the complete schema including all new columns.
    # On existing DBs the IF NOT EXISTS guard is a no-op (table kept as-is).
    conn.executescript(_SCHEMA_CREATE_TABLE)
    conn.commit()

    # Step 1b: Create the reports table (idempotent — IF NOT EXISTS guard).
    conn.executescript(_SCHEMA_REPORTS_TABLE)
    conn.commit()

    # Step 2: Safe ALTER TABLE migrations for existing DBs — each is idempotent.
    # SQLite raises OperationalError if the column already exists; we ignore it.
    # Order matters: run before creating indexes that reference the new columns.
    for stmt in _MIGRATION_STMTS:
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:
            pass  # Column already exists (or other non-fatal DDL error) — no-op

    # Step 2b: Additive reports table migrations (BIS-85 multi-instance prep).
    for stmt in _REPORTS_MIGRATION_STMTS:
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:
            pass  # Column already exists — no-op

    # Step 3: Create indexes — run after migrations so all referenced columns exist.
    conn.executescript(_SCHEMA_INDEXES)
    conn.commit()

    # Step 3b: Create reports indexes.
    conn.executescript(_SCHEMA_REPORTS_INDEXES)
    conn.commit()

    # One-time JSON migration
    json_path = resolved.parent / "pending-agents.json"
    _migrate_json_to_sqlite(json_path, conn)


# ---------------------------------------------------------------------------
# Public: writes
# ---------------------------------------------------------------------------

def session_start(
    id: str,
    description: str,
    chat_id: str | int,
    agent_type: str | None = None,
    source: str = "telegram",
    output_file: str | None = None,
    timeout_minutes: int | None = None,
    task_id: str | None = None,
    parent_id: str | None = None,
    input_summary: str | None = None,
    trigger_message_id: str | None = None,
    trigger_snippet: str | None = None,
    idempotency: str | None = None,
    task_origin: str | None = None,
    path: Path | None = None,
) -> None:
    """Record a newly-spawned agent session.

    Inserts a new row with status='running'. If an entry with the same id
    already exists, it is replaced (idempotent for duplicate spawns).

    Args:
        id:                 Unique agent identifier (uuid or synthetic slug).
        description:        Human-readable summary of what the agent is doing.
        chat_id:            Destination chat for result relay (stored as TEXT).
        agent_type:         Agent subtype string ('functional-engineer', etc.).
        source:             Messaging platform ('telegram', 'slack', etc.).
        output_file:        Full path to /tmp/.../*.output for liveness detection.
        timeout_minutes:    Expected maximum runtime.
        task_id:            Logical task identifier for auto-unregister matching.
        parent_id:          Parent session ID for nested agents (NULL = top-level).
        input_summary:      First ~200 chars of task prompt (optional).
        trigger_message_id: Inbox message_id that caused this spawn (causality).
        trigger_snippet:    First 200 chars of the triggering message text (PII).
        idempotency:        Re-run safety: 'safe' | 'unsafe' | 'unknown' (default).
                            'safe'    — task is read-only/idempotent; safe to re-run
                                        automatically after an interrupted restart.
                            'unsafe'  — task has side effects (writes, sends, posts);
                                        requires explicit user approval to re-run.
                            'unknown' — caller did not classify the task (default).
        task_origin:        Origin of this task: 'user' | 'scheduled' | 'internal'.
                            'user'      — triggered by a real user message (Telegram, Slack).
                            'scheduled' — triggered by a scheduled job or cron task.
                            'internal'  — system-initiated, no user involved (reconciler,
                                          health check, session management, etc.).
                            Defaults to 'user' when not specified.
        path:               DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    now = datetime.now(timezone.utc).isoformat()
    snippet = trigger_snippet[:200] if trigger_snippet else None
    idempotency_val = idempotency if idempotency in ("safe", "unsafe", "unknown") else "unknown"
    task_origin_val = task_origin if task_origin in ("user", "scheduled", "internal") else "user"

    conn.execute(
        """
        INSERT OR REPLACE INTO agent_sessions
            (id, task_id, agent_type, description, chat_id, source, status,
             output_file, timeout_minutes, input_summary, result_summary,
             parent_id, spawned_at, completed_at, last_seen_at,
             notified_at, trigger_message_id, trigger_snippet, reply_message_ids,
             idempotency, task_origin)
        VALUES
            (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, ?, ?, NULL, NULL,
             NULL, ?, ?, NULL, ?, ?)
        """,
        (
            id,
            task_id,
            agent_type,
            description,
            str(chat_id),
            source,
            output_file,
            timeout_minutes,
            input_summary,
            parent_id,
            now,
            trigger_message_id,
            snippet,
            idempotency_val,
            task_origin_val,
        ),
    )
    conn.commit()


def session_end(
    id_or_task_id: str,
    status: str,
    result_summary: str | None = None,
    stop_reason: str | None = None,
    path: Path | None = None,
) -> None:
    """Mark an agent session as completed or failed.

    Matches on either the `id` column or the `task_id` column, whichever
    is found first. This allows callers to use either the registered agent_id
    or the task_id that was passed to write_result.

    Idempotent: calling on a non-existent session is a no-op.

    Args:
        id_or_task_id:  The id or task_id of the session to end.
        status:         Final status: 'completed' | 'failed' | 'dead'.
        result_summary: Optional short summary of the outcome.
        stop_reason:    Why the agent stopped: 'end_turn', 'tool_use',
                        'max_turns', 'error', 'killed', etc. NULL for
                        sessions that ended before this column existed.
        path:           DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    now = datetime.now(timezone.utc).isoformat()

    # Match on id or task_id.  Accept both 'running' (registered by the
    # dispatcher via session_start) and 'starting' (auto-registered by the
    # PostToolUse hook before the agent has fully initialised).  This closes
    # the lifecycle gap where rows inserted as 'starting' were never updated
    # because the old WHERE clause only matched 'running'.
    conn.execute(
        """
        UPDATE agent_sessions
        SET status = ?, completed_at = ?, result_summary = ?, stop_reason = ?
        WHERE (id = ? OR task_id = ?) AND status IN ('running', 'starting')
        """,
        (status, now, result_summary, stop_reason, id_or_task_id, id_or_task_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public: causality and notification writes
# ---------------------------------------------------------------------------

def set_trigger(
    agent_id: str,
    trigger_message_id: str,
    trigger_snippet: str,
    path: Path | None = None,
) -> None:
    """Write causality fields to an existing session.

    Records the inbox message_id that caused the agent to be spawned, and the
    first 200 chars of the triggering message text (PII — stays in this private
    store, never forwarded unless LOBSTER_WIRE_REDACT_PII=false).

    Args:
        agent_id:           The session id (or task_id) to update.
        trigger_message_id: The inbox message_id that caused this spawn
                            (e.g. "1773541796785_6036").
        trigger_snippet:    First 200 chars of the triggering message text.
        path:               DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    snippet = trigger_snippet[:200] if trigger_snippet else ""
    conn.execute(
        """
        UPDATE agent_sessions
        SET trigger_message_id = ?, trigger_snippet = ?
        WHERE id = ? OR task_id = ?
        """,
        (trigger_message_id, snippet, agent_id, agent_id),
    )
    conn.commit()


def set_notified(
    agent_id: str,
    path: Path | None = None,
) -> None:
    """Write the current UTC timestamp to notified_at for the given session.

    Idempotent: calling on an already-notified session is a no-op (it just
    overwrites with a new timestamp, which is harmless).

    Args:
        agent_id: The session id (or task_id) to mark as notified.
        path:     DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE agent_sessions
        SET notified_at = ?
        WHERE id = ? OR task_id = ?
        """,
        (now, agent_id, agent_id),
    )
    conn.commit()


def append_reply_message_id(
    agent_id: str,
    message_id: str,
    path: Path | None = None,
) -> None:
    """Append a sent reply message_id to the session's reply_message_ids JSON array.

    Creates the array if it does not yet exist. This tracks which outbound
    messages were sent back to the user about this agent task.

    Args:
        agent_id:   The session id (or task_id) to update.
        message_id: The outbound message_id to append.
        path:       DB path override (for tests).
    """
    import json as _json

    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    # Read current value
    cursor = conn.execute(
        "SELECT reply_message_ids FROM agent_sessions WHERE id = ? OR task_id = ? LIMIT 1",
        (agent_id, agent_id),
    )
    row = cursor.fetchone()
    if row is None:
        return  # Session not found — no-op

    current_raw = row[0]
    try:
        current_list = _json.loads(current_raw) if current_raw else []
        if not isinstance(current_list, list):
            current_list = []
    except (ValueError, TypeError):
        current_list = []

    current_list.append(message_id)
    new_raw = _json.dumps(current_list)

    conn.execute(
        """
        UPDATE agent_sessions
        SET reply_message_ids = ?
        WHERE id = ? OR task_id = ?
        """,
        (new_raw, agent_id, agent_id),
    )
    conn.commit()


def cleanup_stale_running_sessions(
    server_start_time: datetime,
    path: Path | None = None,
) -> list[str]:
    """Reconcile pre-existing 'running' rows on server startup.

    After a restart, agents whose output files have ``stop_reason=end_turn``
    are marked completed.  Agents whose output files are missing are marked
    dead.  Agents whose output files exist with ``stop_reason=tool_use`` (or
    no stop_reason yet) are left running — Claude Code agents are independent
    processes that survive MCP server restarts.

    Previous versions of this function used an mtime heuristic: if the output
    file's mtime predated the server start time, the agent was assumed dead.
    That assumption is incorrect — Claude Code agents are NOT child processes
    of the MCP server and can outlive a restart.  Using mtime caused false
    positives where healthy agents were killed immediately after every server
    restart.

    Decision rules for each ``status='running'`` row:

    1. ``output_file`` is set and target JSONL is missing on disk → dead
       (file was cleaned up or never created; no live agent can be writing it)
    2. ``output_file`` is set and ``stop_reason=end_turn`` → completed
       (agent finished before or during the restart window)
    3. ``output_file`` is set and ``stop_reason=tool_use`` (or empty) → leave running
       (agent may still be alive; normal reconciler loop will time it out if needed)
    4. ``output_file`` absent, elapsed > timeout → dead (fallback heuristic)
    5. ``output_file`` absent, elapsed ≤ timeout → leave running

    Args:
        server_start_time: UTC datetime when the current server process started
                           (kept for logging; no longer used for mtime comparison).
        path:              DB path override (for tests).

    Returns:
        List of agent IDs that were marked dead or completed.
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    cursor = conn.execute(
        """
        SELECT id, output_file, spawned_at, timeout_minutes
        FROM agent_sessions
        WHERE status IN ('running', 'starting')
        """
    )
    rows = cursor.fetchall()

    changed_ids: list[str] = []
    completed_at = server_start_time.isoformat()

    for row in rows:
        agent_id: str = row["id"]
        output_file: str | None = row["output_file"]
        spawned_at_raw: str | None = row["spawned_at"]
        timeout_minutes: int | None = row["timeout_minutes"]

        if output_file:
            # Read the stop_reason from the output file to determine liveness.
            # "missing" → file gone, safe to mark dead.
            # "done"    → agent finished, mark completed.
            # "running" → agent may still be alive, leave it running.
            file_status = _read_stop_reason_from_path(Path(output_file))

            if file_status == "missing":
                # Apply a 2-minute grace period before marking dead for a missing
                # output file.  The file may not yet exist if the agent was just
                # spawned (output_file paths are created by Claude Code after the
                # first tool turn, not at spawn time), or if the filesystem is in
                # a transient state immediately after an MCP server restart.
                # Only mark dead once we are certain the file should exist.
                elapsed_seconds: float = 0.0
                if spawned_at_raw:
                    try:
                        spawned_dt = datetime.fromisoformat(spawned_at_raw)
                        if spawned_dt.tzinfo is None:
                            spawned_dt = spawned_dt.replace(tzinfo=timezone.utc)
                        elapsed_seconds = (server_start_time - spawned_dt).total_seconds()
                    except (ValueError, TypeError):
                        elapsed_seconds = 0.0

                _MISSING_GRACE_SECONDS = 120  # 2-minute grace period
                if elapsed_seconds < _MISSING_GRACE_SECONDS:
                    log.debug(
                        "[startup-cleanup] session %r output_file missing but only "
                        "%.0fs old — within %ds grace period, leaving running",
                        agent_id,
                        elapsed_seconds,
                        _MISSING_GRACE_SECONDS,
                    )
                else:
                    conn.execute(
                        """
                        UPDATE agent_sessions
                        SET status = 'dead',
                            completed_at = ?,
                            result_summary = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (
                            completed_at,
                            "Marked dead at startup: output_file missing",
                            agent_id,
                        ),
                    )
                    changed_ids.append(agent_id)
                    log.warning(
                        "[startup-cleanup] session %r marked dead: output_file missing "
                        "(elapsed %.0fs > %ds grace period)",
                        agent_id,
                        elapsed_seconds,
                        _MISSING_GRACE_SECONDS,
                    )

            elif file_status == "done":
                conn.execute(
                    """
                    UPDATE agent_sessions
                    SET status = 'completed',
                        completed_at = ?,
                        result_summary = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (
                        completed_at,
                        "Marked completed at startup: stop_reason=end_turn",
                        agent_id,
                    ),
                )
                changed_ids.append(agent_id)
                log.info(
                    "[startup-cleanup] session %r marked completed: stop_reason=end_turn",
                    agent_id,
                )

            else:
                # file_status == "running": output file exists with tool_use or
                # no stop_reason yet. Agent may still be alive. Leave it running
                # so the reconciler's normal liveness loop handles it.
                log.debug(
                    "[startup-cleanup] session %r left running: output_file exists "
                    "with stop_reason=tool_use (agent may still be alive)",
                    agent_id,
                )

        else:
            # No output_file registered — fall back to elapsed-time heuristic
            if spawned_at_raw:
                try:
                    spawned_dt = datetime.fromisoformat(spawned_at_raw)
                    if spawned_dt.tzinfo is None:
                        spawned_dt = spawned_dt.replace(tzinfo=timezone.utc)
                    elapsed_minutes = (server_start_time - spawned_dt).total_seconds() / 60
                    limit_minutes = timeout_minutes if timeout_minutes else 120
                    if elapsed_minutes > limit_minutes:
                        conn.execute(
                            """
                            UPDATE agent_sessions
                            SET status = 'dead',
                                completed_at = ?,
                                result_summary = ?
                            WHERE id = ? AND status = 'running'
                            """,
                            (
                                completed_at,
                                (
                                    f"Marked dead at startup: no output_file and "
                                    f"elapsed {elapsed_minutes:.0f}m exceeds limit {limit_minutes}m"
                                ),
                                agent_id,
                            ),
                        )
                        changed_ids.append(agent_id)
                        log.warning(
                            "[startup-cleanup] session %r marked dead: no output_file, "
                            "elapsed %.0fm > limit %dm",
                            agent_id,
                            elapsed_minutes,
                            limit_minutes,
                        )
                except (ValueError, TypeError):
                    pass
            else:
                # No output_file and no spawned_at — cannot determine age.
                # This row stays 'running' and will never be auto-cleaned by this
                # function. Log a warning so the condition is visible rather than
                # silently ignored.
                log.warning(
                    "[startup-cleanup] session %r has no output_file and no "
                    "spawned_at — cannot determine age, leaving as 'running'. "
                    "Manual cleanup may be required.",
                    agent_id,
                )

    if changed_ids:
        conn.commit()

    return changed_ids


def get_unnotified_completed(
    since_hours: int = 24,
    path: Path | None = None,
) -> list[dict]:
    """Return completed/dead sessions where notified_at IS NULL.

    Used by the startup sweep to re-send notifications that were enqueued but
    not delivered before a crash or restart.

    Args:
        since_hours: Only return sessions completed within this many hours.
                     Prevents re-notifying ancient sessions on a fresh install.
        path:        DB path override (for tests).

    Returns:
        List of session dicts (same format as get_active_sessions).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    import datetime as _dt
    cutoff_dt = datetime.now(timezone.utc) - _dt.timedelta(hours=since_hours)
    cutoff_str = cutoff_dt.isoformat()

    cursor = conn.execute(
        """
        SELECT * FROM agent_sessions
        WHERE status IN ('completed', 'dead')
          AND notified_at IS NULL
          AND completed_at >= ?
        ORDER BY completed_at ASC
        """,
        (cutoff_str,),
    )
    rows = cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Public: queries (pure — no side effects)
# ---------------------------------------------------------------------------

def get_active_sessions(
    path: Path | None = None,
    chat_id: str | None = None,
) -> list[dict]:
    """Return all currently running sessions with elapsed time.

    Args:
        path: Optional DB path override.
        chat_id: When provided, filter to sessions for this chat_id only.

    Returns:
        List of dicts with all session columns plus elapsed_seconds.
        Uses SELECT * so new columns (e.g. stop_reason) are included
        automatically without requiring updates to this function.
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    if chat_id is not None:
        cursor = conn.execute(
            """
            SELECT * FROM agent_sessions
            WHERE status IN ('running', 'starting')
              AND chat_id = ?
            ORDER BY spawned_at ASC
            """,
            (chat_id,),
        )
    else:
        cursor = conn.execute(
            """
            SELECT * FROM agent_sessions
            WHERE status IN ('running', 'starting')
            ORDER BY spawned_at ASC
            """
        )
    rows = cursor.fetchall()
    now = datetime.now(timezone.utc)

    return [_row_to_active_dict(row, now) for row in rows]


def get_session_history(
    limit: int = 50,
    status: str | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Return historical session records, newest first.

    Args:
        limit:  Maximum number of rows to return.
        status: If provided, filter by this status value.
        path:   DB path override (for tests).

    Returns:
        List of dicts with all session fields.
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    if status is not None:
        cursor = conn.execute(
            """
            SELECT * FROM agent_sessions
            WHERE status = ?
            ORDER BY spawned_at DESC
            LIMIT ?
            """,
            (status, limit),
        )
    else:
        cursor = conn.execute(
            """
            SELECT * FROM agent_sessions
            ORDER BY spawned_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    rows = cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


def find_session(
    id_or_task_id: str,
    path: Path | None = None,
) -> dict | None:
    """Return the session matching id or task_id, or None if not found.

    Args:
        id_or_task_id: The id or task_id to look up.
        path:          DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    cursor = conn.execute(
        """
        SELECT * FROM agent_sessions
        WHERE id = ? OR task_id = ?
        ORDER BY spawned_at DESC
        LIMIT 1
        """,
        (id_or_task_id, id_or_task_id),
    )
    row = cursor.fetchone()
    return _row_to_dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Private helpers — pure transformations
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _row_to_active_dict(row: sqlite3.Row, now: datetime) -> dict:
    """Convert a running session row to a dict with elapsed_seconds."""
    d = dict(row)
    spawned_at = d.get("spawned_at", "")
    elapsed: int | None = None
    if spawned_at:
        try:
            spawned_dt = datetime.fromisoformat(spawned_at)
            if spawned_dt.tzinfo is None:
                spawned_dt = spawned_dt.replace(tzinfo=timezone.utc)
            elapsed = int((now - spawned_dt).total_seconds())
        except (ValueError, TypeError):
            pass
    d["elapsed_seconds"] = elapsed
    return d


def _elapsed_minutes_str(elapsed_seconds: int | None) -> str:
    """Format elapsed seconds as a human-readable minutes string."""
    if elapsed_seconds is None:
        return "?"
    minutes = elapsed_seconds // 60
    if minutes < 1:
        return "just now"
    return f"{minutes}m ago"


def format_active_sessions_block(sessions: list[dict]) -> str:
    """Format a list of active sessions as a compact context block.

    Distinguishes user-facing agents from system agents (chat_id=0 or None).
    System agents are background tasks (scheduled jobs, startup-catchup, etc.)
    that do not correspond to a user conversation in the IDE panel.

    Produces output like:
        [1 agent running, 1 system]
        - functional-engineer: "Implement GSD phase plan" (chat: 8305714125, 12m ago)
        - subagent: "startup-catchup" (system, 3m ago)

    Returns an empty string if sessions is empty.
    """
    if not sessions:
        return ""

    # Split into user agents (real chat_id) and system agents (chat_id=0 or None)
    user_sessions = [s for s in sessions if s.get("chat_id") not in (None, 0, "0", "")]
    system_sessions = [s for s in sessions if s.get("chat_id") in (None, 0, "0", "")]

    user_count = len(user_sessions)
    system_count = len(system_sessions)

    user_label = "agent" if user_count == 1 else "agents"
    header = f"[{user_count} {user_label} running"
    if system_count > 0:
        sys_label = "system" if system_count == 1 else "systems"
        header += f", {system_count} {sys_label}"
    header += "]"

    lines = [header]
    for s in user_sessions + system_sessions:
        agent_type = s.get("agent_type") or "agent"
        desc = s.get("description", "")
        # Truncate long descriptions
        if len(desc) > 60:
            desc = desc[:57] + "..."
        chat_id = s.get("chat_id")
        elapsed = _elapsed_minutes_str(s.get("elapsed_seconds"))
        if chat_id in (None, 0, "0", ""):
            lines.append(f'- {agent_type}: "{desc}" (system, {elapsed})')
        else:
            lines.append(f'- {agent_type}: "{desc}" (chat: {chat_id}, {elapsed})')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON migration (one-time, idempotent)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ground-truth scanner — reads Claude Code JSONL output files
# ---------------------------------------------------------------------------

# Default tasks symlink directory where Claude Code writes *.output symlinks.
# NOTE: This path was historically hardcoded with the wrong username (-home-admin-
# instead of the actual user). The reconciler now uses check_output_file_status()
# for per-session checks based on the output_file stored in the DB, which avoids
# this path entirely. scan_agent_outputs() is kept for tests and legacy callers.
_CLAUDE_TASKS_DIR = Path("/tmp/claude-1000/-home-admin-lobster-workspace/tasks")

# Compiled stop_reason pattern — shared by scan_agent_outputs and check_output_file_status
import re as _re
_STOP_REASON_RE = _re.compile(rb'"stop_reason"\s*:\s*"([^"]+)"')


def _read_stop_reason_from_path(output_path: Path) -> str:
    """Read the last stop_reason from a .output symlink or JSONL file.

    Returns one of:
      ``"running"``  — file exists, last stop_reason is "tool_use" or not yet written
      ``"done"``     — last stop_reason is "end_turn"
      ``"missing"``  — symlink target does not exist or file is unreadable
    """
    try:
        real_path = output_path.resolve()
    except OSError:
        return "missing"

    if not real_path.exists():
        return "missing"

    try:
        with open(real_path, "rb") as f:
            try:
                f.seek(-4096, 2)
            except OSError:
                f.seek(0)
            tail = f.read()
    except OSError:
        return "missing"

    if not tail:
        return "running"

    matches = _STOP_REASON_RE.findall(tail)
    if not matches:
        return "running"

    last_reason = matches[-1].decode("utf-8", errors="replace")
    return "done" if last_reason == "end_turn" else "running"


def check_output_file_status(output_file: str) -> str:
    """Check liveness of a single agent by reading its output_file path directly.

    This is the preferred method for the reconciler: it reads the path stored
    in the ``output_file`` DB column, which is always correct regardless of
    username or session layout. It avoids the directory-scan approach that
    relied on a hardcoded (and often wrong) default tasks path.

    Args:
        output_file: Full path to the agent's .output symlink as stored in DB.

    Returns:
        ``"running"``  — agent is still executing (last stop_reason is tool_use
                         or no stop_reason written yet).
        ``"done"``     — agent has finished (last stop_reason is end_turn).
        ``"missing"``  — output file does not exist (agent not yet started,
                         was killed, or output_file was not registered).
    """
    if not output_file:
        return "missing"
    return _read_stop_reason_from_path(Path(output_file))


def get_output_file_mtime(output_file: str) -> float | None:
    """Return the mtime (seconds since epoch) of the resolved output file.

    Follows symlinks so that the mtime reflects when the underlying JSONL file
    was last written, not when the symlink was created.

    Returns None when ``output_file`` is empty, the path does not exist, or
    stat() fails for any reason — callers should treat None as "unknown" and
    apply the conservative (long) threshold.

    Pure function: reads filesystem metadata only, no side effects.
    """
    if not output_file:
        return None
    try:
        real_path = Path(output_file).resolve()
        return real_path.stat().st_mtime
    except OSError:
        return None


def scan_agent_outputs(
    tasks_dir: Path | None = None,
) -> dict[str, str]:
    """Scan a Claude Code tasks directory and return liveness status per agent.

    Each background Task spawned by Claude Code creates a symlink (or file) at:
        <tasks_dir>/<agent-id>.output

    The symlink resolves to a JSONL file in ~/.claude/projects/.../subagents/.
    Claude Code writes a ``stop_reason`` field into these JSONL lines:
      - ``"end_turn"``  → agent has definitively finished
      - ``"tool_use"``  → agent is mid-turn, actively running

    We read only the last 4 KB of each file (fast, constant-time) and scan for
    the last occurrence of ``"stop_reason":``.

    NOTE: The default tasks_dir (_CLAUDE_TASKS_DIR) uses a hardcoded legacy
    path that may not match the current deployment. Prefer check_output_file_status()
    for per-session checks when the output_file path is stored in the DB.

    Args:
        tasks_dir: Override the default tasks directory (for testing).

    Returns:
        A dict mapping agent_id → status string, where status is one of:
          ``"running"``  — file exists, last stop_reason is "tool_use" or not yet written
          ``"done"``     — last stop_reason is "end_turn"
          ``"missing"``  — symlink target JSONL does not exist yet (agent just spawned)
    """
    resolved_dir = tasks_dir if tasks_dir is not None else _CLAUDE_TASKS_DIR

    result: dict[str, str] = {}

    if not resolved_dir.exists():
        return result

    for output_path in resolved_dir.glob("*.output"):
        agent_id = output_path.stem
        result[agent_id] = _read_stop_reason_from_path(output_path)

    return result


# ---------------------------------------------------------------------------
# JSON migration (one-time, idempotent)
# ---------------------------------------------------------------------------

def _migrate_json_to_sqlite(json_path: Path, conn: sqlite3.Connection) -> int:
    """One-time migration: read pending-agents.json and insert entries into SQLite.

    Runs on init_db(). Idempotent: if pending-agents.json.migrated exists,
    migration is skipped. After a successful migration, renames
    pending-agents.json to pending-agents.json.migrated.

    Args:
        json_path: Path to pending-agents.json (may not exist).
        conn:      Open SQLite connection to write into.

    Returns:
        Number of records migrated (0 if skipped or nothing to migrate).
    """
    import json as _json

    migrated_marker = json_path.with_suffix(".json.migrated")
    if migrated_marker.exists():
        return 0  # Already migrated
    if not json_path.exists():
        return 0  # Nothing to migrate

    try:
        raw = json_path.read_text(encoding="utf-8")
        data = _json.loads(raw)
    except Exception:
        return 0  # Unreadable — skip migration, leave file in place

    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        return 0

    agents = data["agents"]
    count = 0
    now = datetime.now(timezone.utc).isoformat()

    for agent in agents:
        agent_id = agent.get("id")
        if not agent_id:
            continue

        # Skip if already in DB (e.g., re-running init_db after partial migration)
        existing = conn.execute(
            "SELECT 1 FROM agent_sessions WHERE id = ?", (agent_id,)
        ).fetchone()
        if existing:
            continue

        conn.execute(
            """
            INSERT INTO agent_sessions
                (id, task_id, description, chat_id, source, status,
                 output_file, timeout_minutes, spawned_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                agent_id,
                agent.get("task_id"),
                agent.get("description", ""),
                str(agent.get("chat_id", "")),
                agent.get("source", "telegram"),
                agent.get("output_file"),
                agent.get("timeout_minutes"),
                agent.get("started_at") or now,
            ),
        )
        count += 1

    if count > 0:
        conn.commit()

    # Rename the JSON file to mark migration complete
    try:
        json_path.rename(migrated_marker)
    except OSError:
        pass  # Rename failed — not fatal, migration data is in DB

    return count


# ---------------------------------------------------------------------------
# Public: reports
# ---------------------------------------------------------------------------

def _next_report_id(conn: sqlite3.Connection) -> str:
    """Return the next sequential RPT-NNN id, collision-safe.

    Reads the report_id of the last-inserted row (by rowid order, which is
    insertion order) and increments its numeric suffix. Using the stored
    RPT-NNN string rather than MAX(id) means deletions cannot cause
    collisions: even if row 5 is deleted, the next call will still read
    RPT-005 from some other row and produce RPT-006.
    """
    row = conn.execute(
        "SELECT report_id FROM reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "RPT-001"
    last = row[0]  # e.g. "RPT-042"
    n = int(last.split("-")[1]) + 1
    return f"RPT-{n:03d}"


def create_report(
    description: str,
    chat_id: str | int,
    source: str = "telegram",
    recent_messages: list | None = None,
    active_session_ids: list[str] | None = None,
    snapshot_state: dict | None = None,
    instance_id: str | None = None,
    path: Path | None = None,
) -> dict:
    """Insert a new report record and return its data dict.

    Captures a point-in-time snapshot of recent messages and active agent
    sessions alongside the user-supplied description. The report_id is
    auto-generated as a sequential RPT-NNN identifier.

    Args:
        description:        User-provided problem description.
        chat_id:            Chat that filed the report (stored as TEXT).
        source:             Messaging platform the report came from.
        recent_messages:    Last N messages from conversation history (list of dicts).
        active_session_ids: IDs of agent sessions active at report time.
        snapshot_state:     Any additional ambient state to capture (arbitrary dict).
        instance_id:        Observability token or hostname identifying the Lobster
                            instance that filed the report. Used by BIS-85 forwarder
                            for multi-instance routing. NULL means unknown/single-instance.
        path:               DB path override (for tests).

    Returns:
        Dict with keys: report_id, id, description, chat_id, source,
        instance_id, created_at, status.
    """
    import json as _json

    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    now = datetime.now(timezone.utc).isoformat()

    report_id = _next_report_id(conn)

    recent_json = _json.dumps(recent_messages) if recent_messages is not None else None
    session_ids_json = _json.dumps(active_session_ids) if active_session_ids is not None else None
    snapshot_json = _json.dumps(snapshot_state) if snapshot_state is not None else None

    conn.execute(
        """
        INSERT INTO reports
            (report_id, description, chat_id, source,
             recent_messages_json, agent_session_ids_json,
             snapshot_state_json, created_at, status, instance_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            report_id,
            description,
            str(chat_id),
            source,
            recent_json,
            session_ids_json,
            snapshot_json,
            now,
            instance_id,
        ),
    )
    conn.commit()

    return {
        "report_id": report_id,
        "description": description,
        "chat_id": str(chat_id),
        "source": source,
        "instance_id": instance_id,
        "created_at": now,
        "status": "open",
    }


def get_report(
    report_id: str,
    path: Path | None = None,
) -> dict | None:
    """Return the full report record for the given report_id, or None.

    Args:
        report_id: The RPT-NNN identifier string.
        path:      DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    cursor = conn.execute(
        "SELECT * FROM reports WHERE report_id = ? LIMIT 1",
        (report_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def list_reports(
    chat_id: str | int | None = None,
    status: str | None = None,
    limit: int = 20,
    path: Path | None = None,
) -> list[dict]:
    """Return reports newest-first, optionally filtered by chat_id and status.

    Args:
        chat_id: If provided, restrict to reports from this chat.
        status:  If provided, restrict to reports with this status.
        limit:   Maximum number of rows to return.
        path:    DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    conditions: list[str] = []
    params: list = []

    if chat_id is not None:
        conditions.append("chat_id = ?")
        params.append(str(chat_id))
    if status is not None:
        conditions.append("status = ?")
        params.append(status)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    cursor = conn.execute(
        f"""
        SELECT id, report_id, description, chat_id, source, created_at, status
        FROM reports
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    )
    rows = cursor.fetchall()
    return [dict(r) for r in rows]
