#!/usr/bin/env python3
"""
SessionStart hook: write the dispatcher session ID to the marker file,
and auto-register subagent sessions into agent_sessions.db.

Fires on SessionStart (non-compact) for ALL sessions that inherit
LOBSTER_MAIN_SESSION=1 (the dispatcher and all subagents it spawns).

## Dispatcher path

When the marker file is absent (fresh dispatcher start) or the current
session_id matches the stored dispatcher ID, writes session_id to
~/messages/config/dispatcher-session-id. This file is the primary signal
that other hooks use to distinguish the dispatcher from subagents.

## Subagent path

When the marker file exists and the current session_id does NOT match the
stored dispatcher ID, this session is a subagent. In that case, a minimal
record is written to agent_sessions.db with status='running'. This ensures
subagents are visible to the ghost detector even when the dispatcher forgets
to call register_agent (e.g. after context compaction). The record is
intentionally sparse — description, agent_type, and output_file can be
filled in later by the dispatcher's register_agent call, which uses INSERT
OR REPLACE and will overwrite this stub. DB write failures are logged to
stderr but never block session start (always exits 0).

## settings.json configuration

Add this to ~/.claude/settings.json under "hooks" → "SessionStart":

    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "python3 $HOME/lobster/hooks/write-dispatcher-session-id.py",
          "timeout": 5
        }
      ]
    }

The empty-string matcher fires on every SessionStart event (both fresh starts
and compact events). The compact variant is already handled by on-compact.py
via a "compact" matcher — the two hooks can coexist safely.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Inline the write logic to avoid import path complexity in hook context.
DISPATCHER_SESSION_FILE = Path(
    os.path.expanduser("~/messages/config/dispatcher-session-id")
)

_MESSAGES_DIR = Path(
    os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")
)
_DEFAULT_DB_PATH = _MESSAGES_DIR / "config" / "agent_sessions.db"

# Override DISPATCHER_SESSION_FILE to respect LOBSTER_MESSAGES when set.
# In production, LOBSTER_MESSAGES defaults to ~/messages so the path is
# identical to the hardcoded constant above. Tests can override it.
if "LOBSTER_MESSAGES" in os.environ:
    DISPATCHER_SESSION_FILE = _MESSAGES_DIR / "config" / "dispatcher-session-id"


def _read_stored_dispatcher_id() -> str | None:
    """Return the session_id stored in the dispatcher marker file, or None."""
    try:
        if not DISPATCHER_SESSION_FILE.exists():
            return None
        value = DISPATCHER_SESSION_FILE.read_text().strip()
        return value or None
    except OSError:
        return None


def _is_dispatcher_session(session_id: str) -> bool:
    """Return True if session_id belongs to the dispatcher.

    Logic:
    - If the marker file is absent, this must be the dispatcher's first start
      (subagents can only be spawned after the dispatcher has written the file).
    - If the marker file exists and session_id matches → dispatcher.
    - If the marker file exists and session_id differs → subagent.
    """
    stored = _read_stored_dispatcher_id()
    if stored is None:
        # No marker file yet — this is the dispatcher writing it for the first time.
        return True
    return session_id == stored


def _write_session_id(session_id: str) -> None:
    """Atomically write session_id to the dispatcher marker file."""
    try:
        DISPATCHER_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = DISPATCHER_SESSION_FILE.with_suffix(".tmp")
        tmp_path.write_text(session_id.strip())
        tmp_path.replace(DISPATCHER_SESSION_FILE)
    except Exception:  # noqa: BLE001
        pass


def _auto_register_subagent(session_id: str) -> None:
    """Write a minimal 'running' record to agent_sessions.db for a subagent.

    This is the auto-registration path: the hook writes a sparse row so the
    ghost detector can see the session even if the dispatcher never calls
    register_agent. Any richer fields (description, agent_type, output_file,
    chat_id, task_id) will be filled in by the dispatcher's register_agent
    call, which uses INSERT OR REPLACE and will overwrite this stub.

    Uses INSERT OR IGNORE so that a racing register_agent call that arrives
    before this hook completes wins cleanly (richer row is preserved).

    Failures are logged to stderr and silently swallowed — this must never
    block session start.
    """
    import sqlite3

    try:
        db_path = _DEFAULT_DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Ensure the table exists (idempotent DDL — safe even if the schema
            # has extra columns added via ALTER TABLE migrations in session_store,
            # because we INSERT only named columns below).
            conn.execute(
                """
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
                    reply_message_ids   TEXT
                )
                """
            )
            # INSERT OR IGNORE: if register_agent already wrote a richer row,
            # leave it untouched. If this is the first write, create the stub.
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_sessions
                    (id, description, chat_id, status, agent_type, spawned_at)
                VALUES
                    (?, 'auto-registered by SessionStart hook', '', 'running', 'unknown', ?)
                """,
                (session_id, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[write-dispatcher-session-id] subagent auto-register failed: {exc}",
            file=sys.stderr,
        )


def main() -> None:
    # Only run for sessions that inherit LOBSTER_MAIN_SESSION=1.
    # This env var is set by claude-persistent.sh for the dispatcher process;
    # all subagents it spawns inherit it. Sessions started outside Lobster
    # (e.g. a developer's personal Claude Code) will not have this set.
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    session_id = data.get("session_id", "").strip()
    if not session_id:
        sys.exit(0)

    if _is_dispatcher_session(session_id):
        _write_session_id(session_id)
    else:
        _auto_register_subagent(session_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
