#!/usr/bin/env python3
"""
PreToolUse gate: while compact-catchup or startup-catchup is in-flight,
block substantive dispatcher tool calls and redirect to wait_for_messages.

## Design (Option B)

Queries agent_sessions.db directly to determine whether a catchup session is
still running. No flag file required — reuses the existing IPC mechanism.

A session is "active" when:
  1. task_id is 'startup-catchup' OR starts with 'compact-catchup'
  2. status = 'running'
  3. spawned_at is within CATCHUP_WINDOW_SECONDS (120 s) of now

The 120-second window prevents this hook from blocking indefinitely if a
catchup session record is never updated to a terminal status (e.g. the
subagent crashed before calling write_result). After 120 seconds the hook
stops gating — the system degrades gracefully rather than deadlocking.

## DB query

    SELECT spawned_at
    FROM agent_sessions
    WHERE (task_id = 'startup-catchup' OR task_id LIKE 'compact-catchup%')
      AND status = 'running'
    ORDER BY spawned_at DESC
    LIMIT 1

Only the most recent matching row is checked (ORDER BY spawned_at DESC LIMIT 1).

## Fail-open policy

If the DB file is missing, locked, corrupt, or the query fails for any other
reason: exit(0). This hook must never block the dispatcher due to its own
infrastructure failures.

## Allow-list (always passes through, even during active catchup)

  - mcp__lobster-inbox__wait_for_messages
  - mcp__lobster-inbox__check_inbox
  - mcp__lobster-inbox__mark_processing
  - mcp__lobster-inbox__mark_processed
  - mcp__lobster-inbox__mark_failed
  - mcp__lobster-inbox__send_reply
  - mcp__lobster-inbox__claim_and_ack

## Session discrimination

Only the dispatcher session is gated. Subagents are identified by the presence
of the agent_id field (absent from dispatcher PreToolUse payloads), which is
the fast-path exit. The full layered detection from session_role.py is then
applied: state files → process-tree walk.

## Settings.json configuration

Add to hooks → PreToolUse:

    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "python3 $HOME/lobster/hooks/catchup-gate.py",
          "timeout": 5
        }
      ]
    }
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Make hooks/ importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).parent))

from session_role import is_dispatcher_session  # noqa: E402 — after sys.path insertion

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# DB path: check canonical workspace location.
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
AGENT_SESSIONS_DB = _WORKSPACE / "data" / "agent_sessions.db"

# A catchup session must have been spawned within this window to trigger the gate.
CATCHUP_WINDOW_SECONDS = 120

LOG_FILE = _WORKSPACE / "logs" / "catchup-gate.log"

# SQL query: most recent running catchup session, most-recent-first.
_QUERY = (
    "SELECT spawned_at FROM agent_sessions "
    "WHERE (task_id='startup-catchup' OR task_id LIKE 'compact-catchup%') "
    "AND status='running' "
    "ORDER BY spawned_at DESC LIMIT 1"
)

# Tools that always pass through while catchup is in flight.
ALWAYS_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "mcp__lobster-inbox__wait_for_messages",
    "mcp__lobster-inbox__check_inbox",
    "mcp__lobster-inbox__mark_processing",
    "mcp__lobster-inbox__mark_processed",
    "mcp__lobster-inbox__mark_failed",
    "mcp__lobster-inbox__send_reply",
    "mcp__lobster-inbox__claim_and_ack",
})

BLOCK_MESSAGE = (
    "Catching up — give me 120 seconds. "
    "compact-catchup or startup-catchup is still running. "
    "The dispatcher must not take substantive actions until catchup completes. "
    "Call `mcp__lobster-inbox__wait_for_messages` to resume the main loop and "
    "await the catchup write_result."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(tool_name: str, action: str) -> None:
    """Append a JSON log line to catchup-gate.log. Silent on any failure."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = json.dumps({"ts": ts, "tool": tool_name, "action": action}) + "\n"
        with LOG_FILE.open("a") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


def _catchup_is_active() -> bool:
    """Return True iff a running catchup session was spawned within the window.

    Queries agent_sessions.db directly. Returns False on any error (fail open).
    """
    db_path = AGENT_SESSIONS_DB
    if not db_path.exists():
        return False  # No DB → fail open

    try:
        # timeout=1 avoids blocking on a locked DB.
        conn = sqlite3.connect(str(db_path), timeout=1)
        try:
            cursor = conn.execute(_QUERY)
            row = cursor.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — sqlite3.OperationalError, DatabaseError, etc.
        return False  # Fail open on any DB error

    if row is None:
        return False  # No matching running session

    spawned_at_str: str = row[0]
    try:
        # Parse ISO8601 UTC: "2026-04-25T12:34:56Z"
        # calendar.timegm interprets the struct_time as UTC (unlike mktime which
        # uses local time), so no manual offset adjustment is needed.
        import calendar
        spawned_ts_utc = calendar.timegm(
            time.strptime(spawned_at_str, "%Y-%m-%dT%H:%M:%SZ")
        )
        age = time.time() - spawned_ts_utc
        return 0 <= age < CATCHUP_WINDOW_SECONDS
    except (ValueError, OverflowError):
        return False  # Unparseable timestamp → fail open


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Fast path: agent_id is injected by CC only for subagent PreToolUse payloads.
    # Dispatcher payloads never have agent_id.
    if data.get("agent_id"):
        sys.exit(0)

    # Only enforce for the dispatcher session.
    if not is_dispatcher_session(data):
        sys.exit(0)

    tool_name = data.get("tool_name", "")

    # Allow-listed tools always pass through.
    if tool_name in ALWAYS_ALLOWED_TOOLS:
        sys.exit(0)

    # Check the DB. If no active catchup, allow everything.
    if not _catchup_is_active():
        sys.exit(0)

    # Active catchup: block substantive tools.
    _log(tool_name, "blocked")
    print(BLOCK_MESSAGE, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
