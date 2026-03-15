#!/usr/bin/env python3
"""
SessionStart hook: write the dispatcher session ID to the marker file.

Fires on SessionStart (non-compact) for the main Lobster dispatcher session.
Writes the current session_id to ~/messages/config/dispatcher-session-id so
that other hooks can quickly identify whether they are running inside the
dispatcher or a background subagent.

Only writes when LOBSTER_MAIN_SESSION=1 is set — the env var established by
claude-persistent.sh to mark the dispatcher process. Subagents inherit this
var but are detected later via the marker file comparison in session_role.py.

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

# Inline the write logic to avoid import path complexity in hook context.
DISPATCHER_SESSION_FILE = Path(
    os.path.expanduser("~/messages/config/dispatcher-session-id")
)


def _write_session_id(session_id: str) -> None:
    """Atomically write session_id to the dispatcher marker file."""
    try:
        DISPATCHER_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = DISPATCHER_SESSION_FILE.with_suffix(".tmp")
        tmp_path.write_text(session_id.strip())
        tmp_path.replace(DISPATCHER_SESSION_FILE)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    # Only write for the main dispatcher process.
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    session_id = data.get("session_id", "").strip()
    if not session_id:
        sys.exit(0)

    _write_session_id(session_id)
    sys.exit(0)


if __name__ == "__main__":
    main()
