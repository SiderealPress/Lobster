#!/usr/bin/env python3
"""
PostToolUse hook: dispatcher heartbeat.

Writes a Unix epoch timestamp to ~/lobster-workspace/logs/dispatcher-heartbeat
on every PostToolUse event. The health check reads this file and asks one
question: is this file newer than N seconds (default: 1200s / 20 minutes)?

This replaces the previous multi-signal WFM freshness check that aggregated:
  (a) claude-heartbeat file mtime (written by inbox_server.py on WFM calls)
  (b) last_processed_at in lobster-state.json (written on mark_processed)
  (c) last_thinking_at in lobster-state.json (written by this hook)

The 20-minute threshold is generous enough to cover compaction (1-3+ minutes)
and catchup subagents (10-12 minutes) without any suppression logic.

Design:
- Unconditional: fires on every PostToolUse (no tool-name filtering needed)
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Single value: just a Unix epoch integer, nothing else
- Silent on failure: health check degrades gracefully when file is absent
"""

import os
import sys
import time
from pathlib import Path


WORKSPACE_DIR = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
HEARTBEAT_FILE = WORKSPACE_DIR / "logs" / "dispatcher-heartbeat"


def write_dispatcher_heartbeat(heartbeat_file: Path) -> None:
    """Write current Unix epoch to heartbeat_file atomically."""
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(heartbeat_file) + ".tmp")
    tmp.write_text(str(int(time.time())) + "\n")
    os.rename(str(tmp), str(heartbeat_file))


def main() -> None:
    try:
        write_dispatcher_heartbeat(HEARTBEAT_FILE)
    except Exception:
        # Never block tool execution — health check degrades gracefully if file is absent
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
