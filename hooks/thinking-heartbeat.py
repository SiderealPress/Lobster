#!/usr/bin/env python3
"""
PostToolUse hook: dispatcher heartbeat.

Writes the current Unix epoch timestamp to a single heartbeat file when the
current session is the dispatcher. Subagent tool calls are silently ignored so
they cannot keep the health check satisfied while the dispatcher is frozen or dead.

Purpose: the dispatcher can spend 10+ minutes in a reasoning/catchup phase
without touching wait_for_messages. Any tool call at all means the dispatcher
is alive. This hook captures that signal via the simplest possible mechanism:
a single file containing a single integer (epoch seconds).

Design:
- Fires on every PostToolUse (no tool-name filtering needed)
- Guards on dispatcher session: reads hook input from stdin, checks session_id
  against the stored dispatcher session ID. Subagent calls exit 0 immediately.
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Single integer timestamp — no JSON parsing, no merging, no state file locking
- Silent on failure: health check degrades gracefully when file is absent
- Threshold-based: the health check uses a 15-minute window that naturally
  covers compaction, catchup, and boot transitions without any suppression logic

Dispatcher-only guard (issue #1897):
- PostToolUse fires for ALL sessions sharing the same Claude Code process,
  including background subagents. Without this guard, a subagent doing heavy
  tool work can keep the heartbeat fresh even if the dispatcher is dead.
- The guard reads hook_input["session_id"] and compares it against the
  dispatcher-session-id file written by write-dispatcher-session-id.py at
  dispatcher startup (SessionStart hook). If the session IDs do not match,
  the current caller is a subagent and the heartbeat write is skipped.
- Launcher-agnostic: works whether Lobster is started via claude-interactive.exp
  or claude-persistent.sh, because both launchers trigger the same SessionStart
  hook that writes the dispatcher session ID file.

File location: ~/lobster-workspace/logs/dispatcher-heartbeat
Content: single Unix epoch integer (e.g. "1713456789\n")

Replaces the multi-signal approach (claude-heartbeat file + last_processed_at +
last_thinking_at in lobster-state.json) with a single authoritative signal.
See issue #1483, #1897.
"""

import json
import os
import sys
from pathlib import Path

# Allow imports from the hooks directory (session_role).
_HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(_HOOKS_DIR))

import session_role  # noqa: E402 — path insert must precede this


WORKSPACE_DIR = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
HEARTBEAT_FILE = Path(
    os.environ.get(
        "LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE",
        WORKSPACE_DIR / "logs" / "dispatcher-heartbeat",
    )
)

# Sentinel threshold used in tests — not read here, but documents the expected value.
# The health check uses DISPATCHER_HEARTBEAT_STALE_SECONDS = 900 (15 minutes).
DISPATCHER_HEARTBEAT_STALE_SECONDS = 900


def write_heartbeat(heartbeat_file: Path) -> None:
    """Write current Unix epoch to heartbeat_file atomically."""
    import time
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = heartbeat_file.with_suffix(".tmp")
    tmp.write_text(str(int(time.time())) + "\n")
    os.rename(str(tmp), str(heartbeat_file))


def main() -> None:
    # Read hook input from stdin (provided by Claude Code on every PostToolUse).
    # If stdin is empty or unparseable, treat as unknown session — skip heartbeat
    # (conservative: better to miss one update than to falsely extend it for a subagent).
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, EOFError):
        sys.exit(0)

    # Guard: only write the heartbeat for the dispatcher session.
    # is_dispatcher() compares hook_input["session_id"] against the stored
    # dispatcher session ID file. Returns False for subagents and when the
    # session ID is unavailable. Fails open (returns True) on I/O errors.
    if not session_role.is_dispatcher(hook_input):
        sys.exit(0)

    try:
        write_heartbeat(HEARTBEAT_FILE)
    except Exception:
        # Never block tool execution — health check degrades gracefully when file absent.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
