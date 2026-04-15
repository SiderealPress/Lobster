#!/usr/bin/env python3
"""
SessionStart hook: write the dispatcher session ID to the marker file.

Fires on SessionStart (non-compact) for ALL sessions that inherit
LOBSTER_MAIN_SESSION=1 (the dispatcher and all subagents it spawns).
Only the dispatcher path performs any action; subagent sessions are
silently ignored.

## Dispatcher path

When the marker file is absent (fresh dispatcher start), the current
session_id matches the stored dispatcher ID, or the stored dispatcher session
has ended (its .jsonl file is gone from ~/.claude/projects/), writes
session_id to ~/messages/config/dispatcher-session-id. This file is the
primary signal that other hooks use to distinguish the dispatcher from
subagents.

## Subagent sessions

Subagent sessions are not acted on by this hook. The correct discipline is for
the dispatcher to call `register_agent` before spawning via `Task()`. A
SessionStart-based stub cannot know the real `chat_id` — it would always write
`chat_id='0'`, which is useless for notification and creates ghost session rows
that cause reconciler noise. The ghost detector (`agent-monitor.py`) can find
unregistered sessions via the filesystem without needing a DB stub.

## Dispatcher DB registration (issue #781)

When the hook determines the current session IS the dispatcher, it also writes
a row to agent_sessions.db with agent_type='dispatcher'. This row is permanent
insurance: even if a future crash causes _is_dispatcher_session() to
misclassify a restart as a subagent, the reconciler's dispatcher-type skip
guard (added in the same issue) will still prevent agent_failed from being
emitted for any session tagged 'dispatcher'.

## Stale marker recovery

If the dispatcher crashes or is killed without a clean `lobster stop`, the
marker file retains the dead session's ID. On the next start, the new
dispatcher's session_id won't match and would normally be misclassified as a
subagent. This hook detects that the stored session's JSONL file has not been
modified recently (idle for longer than JSONL_MAX_IDLE_SECONDS, default 3
hours) and correctly classifies the new session as the replacement dispatcher.

Note: Claude Code never deletes JSONL files for ended sessions, so presence
alone cannot distinguish a live session from a dead one.  Using mtime (last
modification time) as a proxy for liveness is reliable because active
dispatcher sessions append to their JSONL file on every message.  A JSONL
file that has not been modified for 3+ hours belongs to an idle or dead
session.  The primary defence against this scenario is `lobster stop` (and
`restart`) clearing the marker file; this check is a secondary safety net.

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
import time
from pathlib import Path

# Allow imports from both the hooks directory (session_role) and src/agents (session_store).
_HOOKS_DIR = Path(__file__).parent
_SRC_DIR = _HOOKS_DIR.parent / "src"
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_SRC_DIR))

import session_role  # noqa: E402 — path insert must precede this
from agents import session_store  # noqa: E402

# A JSONL file not modified for longer than this is treated as belonging to an
# idle or dead session.  Active dispatcher sessions append to their JSONL on
# every message, so 3 hours of silence indicates the session has ended.
JSONL_MAX_IDLE_SECONDS = 3 * 60 * 60  # 3 hours


def _stored_session_is_alive(stored_session_id: str) -> bool:
    """Return True if the stored dispatcher session appears to be active.

    Claude Code stores each session's conversation as
    ~/.claude/projects/<workspace-slug>/<session-id>.jsonl.  Claude never
    deletes these files, so presence alone does not mean the session is live.
    Instead, we check the file's modification time: a JSONL file not written
    to for longer than JSONL_MAX_IDLE_SECONDS belongs to an idle or dead
    session.

    Falls back to True (conservative / assume alive) if the project directory
    cannot be determined or the JSONL file is not found via glob.
    """
    try:
        projects_dir = Path(os.path.expanduser("~/.claude/projects"))
        if not projects_dir.is_dir():
            return True  # can't determine — assume alive (conservative)
        # Search all workspace subdirectories for the session file.
        for jsonl in projects_dir.glob(f"*/{stored_session_id}.jsonl"):
            # File found — check whether it has been modified recently.
            age_seconds = time.time() - jsonl.stat().st_mtime
            return age_seconds < JSONL_MAX_IDLE_SECONDS
        # No JSONL file found anywhere — the stored session ended (or was never
        # started here); treat as dead.
        return False
    except Exception:  # noqa: BLE001
        return True  # conservative fallback


def _is_dispatcher_session(session_id: str) -> bool:
    """Return True if session_id belongs to the dispatcher.

    Decision table:
    - Marker file absent: this must be the dispatcher's first start
      (subagents can only be spawned after the dispatcher has written the file).
    - Marker file exists and session_id matches: still the same dispatcher.
    - Marker file exists, session_id differs, stored session still alive:
      this is a subagent of the running dispatcher.
    - Marker file exists, session_id differs, stored session is gone:
      the old dispatcher ended without a clean stop (e.g. crash/OOM); treat
      this new session as the replacement dispatcher.
    """
    stored = session_role._read_dispatcher_session_id()
    if stored is None:
        return True  # no marker — must be the first dispatcher start
    if session_id == stored:
        return True  # same session reattaching (e.g. after compact)
    # Different session ID.  If the stored session is still alive, this is a
    # subagent.  If it has ended (JSONL gone), this is the new dispatcher.
    return not _stored_session_is_alive(stored)


def _register_dispatcher_session(session_id: str) -> None:
    """Write a 'running' row to agent_sessions.db tagged as agent_type='dispatcher'.

    The reconciler skips sessions with agent_type='dispatcher', so this row
    prevents any future ghost-session cascade even if the marker-file check
    mis-fires.  Uses INSERT OR IGNORE so a richer row written by a concurrent
    caller (rare) is left untouched.  Failures are logged and silently swallowed.
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
                (?, 'Lobster dispatcher (registered by SessionStart hook)', '0',
                 'running', 'dispatcher', ?)
            """,
            (session_id, now),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[write-dispatcher-session-id] dispatcher register failed: {exc}",
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
        # Issue #781 Fix 2: also tag this session in agent_sessions.db as
        # 'dispatcher' so the reconciler never emits agent_failed for it.
        _register_dispatcher_session(session_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
