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
stub record is written to agent_sessions.db with status='running'. This
ensures subagents are visible to the ghost detector even when the dispatcher
forgets to call register_agent (e.g. after context compaction). The stub uses
INSERT OR IGNORE so a racing register_agent call that writes a richer row
first is preserved untouched. DB write failures are logged to stderr but
never block session start (always exits 0).

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
from pathlib import Path

# Allow imports from both the hooks directory (session_role) and src/agents (session_store).
_HOOKS_DIR = Path(__file__).parent
_SRC_DIR = _HOOKS_DIR.parent / "src"
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_SRC_DIR))

import session_role  # noqa: E402 — path insert must precede this
from agents import session_store  # noqa: E402


def _is_dispatcher_session(session_id: str) -> bool:
    """Return True if session_id belongs to the dispatcher.

    - Marker file absent: this must be the dispatcher's first start
      (subagents can only be spawned after the dispatcher has written the file).
    - Marker file exists and session_id matches: dispatcher.
    - Marker file exists and session_id differs: subagent.
    """
    stored = session_role._read_dispatcher_session_id()
    return stored is None or session_id == stored


def _auto_register_subagent(session_id: str) -> None:
    """Write a minimal stub 'running' record to agent_sessions.db.

    Uses INSERT OR IGNORE so a racing register_agent call that writes a richer
    row first is left untouched. init_db() ensures the schema exists; the
    INSERT then uses the module's connection pool without duplicating DDL.
    Failures are logged and silently swallowed.
    """
    from datetime import datetime, timezone

    try:
        session_store.init_db()
        conn = session_store._get_connection(session_store._DEFAULT_DB_PATH)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_sessions
                (id, description, chat_id, status, agent_type, spawned_at)
            VALUES
                (?, 'auto-registered by SessionStart hook', '0', 'running', 'subagent', ?)
            """,
            (session_id, now),
        )
        conn.commit()
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
        session_role.write_dispatcher_session_id(session_id)
    else:
        _auto_register_subagent(session_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
