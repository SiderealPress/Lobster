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
- Guards on dispatcher session: reads hook input from stdin, checks agent_id field.
  Subagent calls exit 0 immediately.
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Single integer timestamp — no JSON parsing, no merging, no state file locking
- Silent on failure: health check degrades gracefully when file is absent
- Threshold-based: the health check uses a 15-minute window that naturally
  covers compaction, catchup, and boot transitions without any suppression logic

Dispatcher-only guard (issue #1897):
- PostToolUse fires for ALL sessions sharing the same Claude Code process,
  including background subagents. Without this guard, a subagent doing heavy
  tool work can keep the heartbeat fresh even if the dispatcher is dead.
- The guard reads hook_input["agent_id"]. Claude Code injects agent_id only into
  subagent payloads — the dispatcher session never has it. If agent_id is present
  and non-empty, the session is a subagent and the heartbeat write is skipped.

Why agent_id and NOT is_dispatcher_session():
- is_dispatcher_session() uses state file I/O and a process-tree walk (via tmux)
  which introduce I/O latency and external process dependencies on every tool call.
- The agent_id field check is a single dict lookup: no file reads, no subprocess
  spawns, no imported helpers.
- Fail-open: when stdin is empty or unparseable, the heartbeat IS written (not
  skipped). The dispatcher never sets agent_id, so unparseable input cannot be a
  subagent payload. This preserves the liveness signal during early-boot or
  abnormal stdin conditions.
- Works for both launchers (claude-interactive.exp in debug mode and
  claude-persistent.sh in standard mode): agent_id injection is done by Claude
  Code itself, not by any launcher-specific logic.

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


WORKSPACE_DIR = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
HEARTBEAT_FILE = Path(
    os.environ.get(
        "LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE",
        WORKSPACE_DIR / "logs" / "dispatcher-heartbeat",
    )
)

# Sentinel threshold used in tests — not read here, but documents the expected value.
# The health check uses DISPATCHER_HEARTBEAT_STALE_SECONDS = 1200 (20 minutes).
DISPATCHER_HEARTBEAT_STALE_SECONDS = 1200

# Field injected by Claude Code into subagent hook payloads (absent for dispatcher).
# Named constant for test clarity — see issue #1897.
AGENT_ID_FIELD = "agent_id"


def write_heartbeat(heartbeat_file: Path) -> None:
    """Write current Unix epoch to heartbeat_file atomically."""
    import time
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = heartbeat_file.with_suffix(".tmp")
    tmp.write_text(str(int(time.time())) + "\n")
    os.rename(str(tmp), str(heartbeat_file))


def is_subagent(hook_input: dict) -> bool:
    """Return True when this hook is running inside a subagent session.

    Claude Code injects agent_id only into subagent hook payloads.
    The dispatcher session never carries agent_id.
    A truthy (non-empty) agent_id means subagent; absent or falsy means dispatcher.
    """
    return bool(hook_input.get(AGENT_ID_FIELD))


def main() -> None:
    # Read hook input from stdin (provided by Claude Code on every PostToolUse).
    # If stdin is empty or unparseable, treat as dispatcher — write the heartbeat.
    # Fail-open: the dispatcher cannot set agent_id, so we cannot falsely attribute
    # an unparseable payload to a subagent.
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, EOFError):
        hook_input = {}

    # Guard: skip heartbeat for subagent sessions.
    # agent_id is present only in subagent payloads — check it without any I/O.
    if is_subagent(hook_input):
        sys.exit(0)

    try:
        write_heartbeat(HEARTBEAT_FILE)
    except Exception:
        # Never block tool execution — health check degrades gracefully when file absent.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
