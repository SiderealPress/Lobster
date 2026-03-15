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

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id              TEXT PRIMARY KEY,
    task_id         TEXT,
    agent_type      TEXT,
    description     TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'telegram',
    status          TEXT NOT NULL DEFAULT 'running',
    output_file     TEXT,
    timeout_minutes INTEGER,
    input_summary   TEXT,
    result_summary  TEXT,
    parent_id       TEXT,
    spawned_at      TEXT NOT NULL,
    completed_at    TEXT,
    last_seen_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_status ON agent_sessions (status);
CREATE INDEX IF NOT EXISTS idx_spawned_at ON agent_sessions (spawned_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_id ON agent_sessions (task_id);
"""

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
    if they do not exist. Also runs one-time migration from pending-agents.json
    if that file exists and has not already been migrated.

    Args:
        path: Override the default DB path. Primarily used in tests.
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    conn.executescript(_SCHEMA_SQL)
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
    path: Path | None = None,
) -> None:
    """Record a newly-spawned agent session.

    Inserts a new row with status='running'. If an entry with the same id
    already exists, it is replaced (idempotent for duplicate spawns).

    Args:
        id:              Unique agent identifier (uuid or synthetic slug).
        description:     Human-readable summary of what the agent is doing.
        chat_id:         Destination chat for result relay (stored as TEXT).
        agent_type:      Agent subtype string ('functional-engineer', etc.).
        source:          Messaging platform ('telegram', 'slack', etc.).
        output_file:     Full path to /tmp/.../*.output for liveness detection.
        timeout_minutes: Expected maximum runtime.
        task_id:         Logical task identifier for auto-unregister matching.
        parent_id:       Parent session ID for nested agents (NULL = top-level).
        input_summary:   First ~200 chars of task prompt (optional).
        path:            DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT OR REPLACE INTO agent_sessions
            (id, task_id, agent_type, description, chat_id, source, status,
             output_file, timeout_minutes, input_summary, result_summary,
             parent_id, spawned_at, completed_at, last_seen_at)
        VALUES
            (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, ?, ?, NULL, NULL)
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
        ),
    )
    conn.commit()


def session_end(
    id_or_task_id: str,
    status: str,
    result_summary: str | None = None,
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
        path:           DB path override (for tests).
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)
    now = datetime.now(timezone.utc).isoformat()

    # Try matching on id first, then task_id
    conn.execute(
        """
        UPDATE agent_sessions
        SET status = ?, completed_at = ?, result_summary = ?
        WHERE (id = ? OR task_id = ?) AND status = 'running'
        """,
        (status, now, result_summary, id_or_task_id, id_or_task_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public: queries (pure — no side effects)
# ---------------------------------------------------------------------------

def get_active_sessions(path: Path | None = None) -> list[dict]:
    """Return all currently running sessions with elapsed time.

    Returns:
        List of dicts with keys: id, task_id, agent_type, description,
        chat_id, source, status, output_file, timeout_minutes, parent_id,
        spawned_at, elapsed_seconds.
    """
    resolved = path if path is not None else _DEFAULT_DB_PATH
    conn = _get_connection(resolved)

    cursor = conn.execute(
        """
        SELECT id, task_id, agent_type, description, chat_id, source, status,
               output_file, timeout_minutes, parent_id, spawned_at
        FROM agent_sessions
        WHERE status = 'running'
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

    Produces output like:
        [2 agents running]
        - functional-engineer: "Implement GSD phase plan for BIS-51" (chat: 6645894734, 12m ago)
        - general-purpose: "Archive link for Drew" (chat: 6645894734, 2m ago)

    Returns an empty string if sessions is empty.
    """
    if not sessions:
        return ""
    count = len(sessions)
    label = "agent" if count == 1 else "agents"
    lines = [f"[{count} {label} running]"]
    for s in sessions:
        agent_type = s.get("agent_type") or "agent"
        desc = s.get("description", "")
        # Truncate long descriptions
        if len(desc) > 60:
            desc = desc[:57] + "..."
        chat_id = s.get("chat_id", "?")
        elapsed = _elapsed_minutes_str(s.get("elapsed_seconds"))
        lines.append(f'- {agent_type}: "{desc}" (chat: {chat_id}, {elapsed})')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON migration (one-time, idempotent)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ground-truth scanner — reads Claude Code JSONL output files
# ---------------------------------------------------------------------------

# Default tasks symlink directory where Claude Code writes *.output symlinks
_CLAUDE_TASKS_DIR = Path("/tmp/claude-1000/-home-admin-lobster-workspace/tasks")


def scan_agent_outputs(
    tasks_dir: Path | None = None,
) -> dict[str, str]:
    """Scan Claude Code agent output files and return their liveness status.

    Each background Task spawned by Claude Code creates a symlink (or file) at:
        /tmp/claude-1000/-home-admin-lobster-workspace/tasks/<agent-id>.output

    The symlink resolves to a JSONL file in ~/.claude/projects/.../subagents/.
    Claude Code writes a ``stop_reason`` field into these JSONL lines:
      - ``"end_turn"``  → agent has definitively finished
      - ``"tool_use"``  → agent is mid-turn, actively running

    We read only the last 4 KB of each file (fast, constant-time) and scan for
    the last occurrence of ``"stop_reason":``.

    Args:
        tasks_dir: Override the default tasks directory (for testing).

    Returns:
        A dict mapping agent_id → status string, where status is one of:
          ``"running"``  — file exists, last stop_reason is "tool_use" or not yet written
          ``"done"``     — last stop_reason is "end_turn"
          ``"missing"``  — symlink target JSONL does not exist yet (agent just spawned)
    """
    import re

    resolved_dir = tasks_dir if tasks_dir is not None else _CLAUDE_TASKS_DIR

    result: dict[str, str] = {}

    if not resolved_dir.exists():
        return result

    # Pattern to extract the last stop_reason value in the tail bytes
    _stop_reason_re = re.compile(rb'"stop_reason"\s*:\s*"([^"]+)"')

    for output_path in resolved_dir.glob("*.output"):
        # agent_id is the filename stem (e.g. "a24c111e0daad91f7" from "a24c111e0daad91f7.output")
        agent_id = output_path.stem

        # Resolve symlink to real JSONL path (or use the file itself)
        try:
            real_path = output_path.resolve()
        except OSError:
            result[agent_id] = "missing"
            continue

        if not real_path.exists():
            result[agent_id] = "missing"
            continue

        # Read last 4 KB — constant-time regardless of file size
        try:
            with open(real_path, "rb") as f:
                try:
                    f.seek(-4096, 2)
                except OSError:
                    # File smaller than 4 KB; read from start
                    f.seek(0)
                tail = f.read()
        except OSError:
            result[agent_id] = "missing"
            continue

        if not tail:
            # Empty file → agent just spawned, no output yet
            result[agent_id] = "running"
            continue

        # Find the *last* stop_reason in the tail
        matches = _stop_reason_re.findall(tail)
        if not matches:
            # No stop_reason written yet → still running
            result[agent_id] = "running"
            continue

        last_reason = matches[-1].decode("utf-8", errors="replace")
        if last_reason == "end_turn":
            result[agent_id] = "done"
        else:
            # "tool_use" or any other value → still running
            result[agent_id] = "running"

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
