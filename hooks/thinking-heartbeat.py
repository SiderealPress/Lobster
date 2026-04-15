#!/usr/bin/env python3
"""
PostToolUse hook: dispatcher heartbeat.

Writes the current Unix epoch timestamp to a single heartbeat file on every
PostToolUse event. The health check reads this file to determine whether the
dispatcher is alive.

Purpose: the dispatcher can spend 10+ minutes in a reasoning/catchup phase
without touching wait_for_messages. Any tool call at all means the dispatcher
is alive. This hook captures that signal via the simplest possible mechanism:
a single file containing a single integer (epoch seconds).

Design:
- Fires on every PostToolUse (no tool-name filtering needed)
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Single integer timestamp — no JSON parsing, no merging, no state file locking
- Silent on failure: health check degrades gracefully when file is absent
- Threshold-based: the health check uses a 20-minute window that naturally
  covers compaction, catchup, and boot transitions without any suppression logic

File location: ~/lobster-workspace/logs/dispatcher-heartbeat
Content: single Unix epoch integer (e.g. "1713456789\n")

Replaces the multi-signal approach (claude-heartbeat file + last_processed_at +
last_thinking_at in lobster-state.json) with a single authoritative signal.
See issue #1483.
"""

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


def write_heartbeat(heartbeat_file: Path) -> None:
    """Write current Unix epoch to heartbeat_file atomically."""
    import time
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = heartbeat_file.with_suffix(".tmp")
    tmp.write_text(str(int(time.time())) + "\n")
    os.rename(str(tmp), str(heartbeat_file))


def main() -> None:
    try:
        write_heartbeat(HEARTBEAT_FILE)
    except Exception:
        # Never block tool execution — health check degrades gracefully when file absent.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
