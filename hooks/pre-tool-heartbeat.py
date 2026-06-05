#!/usr/bin/env python3
"""
PreToolUse hook: dispatcher pre-tool heartbeat.

Writes the current Unix epoch timestamp to a dedicated heartbeat file on every
PreToolUse event. This complements thinking-heartbeat.py (PostToolUse) and narrows
the detection window for inference-gap cases (issue #1695).

Purpose
-------
thinking-heartbeat.py fires after each tool call completes. For long-running tools
(e.g., wait_for_messages with a multi-hour timeout), the post-tool signal goes stale
even though the dispatcher is alive and about to execute a tool. The pre-tool
heartbeat fires *before* the tool runs, so:

  - A stale pre-tool heartbeat + fresh post-tool heartbeat => tool is running (OK)
  - A stale pre-tool heartbeat + stale post-tool heartbeat => dispatcher frozen (BAD)
  - A fresh pre-tool heartbeat + stale post-tool heartbeat => tool is running (OK)

In practice, the health check can use the pre-tool heartbeat as a lower bound on
dispatcher liveness: if neither signal has been updated in N seconds, the dispatcher
is frozen.

For the inference-gap case (#1695, #1786): lowering the PostToolUse heartbeat
threshold (currently 1200s) risks false positives during legitimate long tool calls.
The pre-tool heartbeat lets us reduce that threshold safely — it confirms the
dispatcher called the tool even if post-tool hasn't fired yet.

Design
------
- Fires on every PreToolUse (matcher: "")
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Single integer timestamp — no JSON parsing, no locking, no network
- Silent on failure: health check degrades gracefully when file absent
- < 1ms on warm OS (rename is a kernel atomic op on same-filesystem paths)

File location: ~/lobster-workspace/logs/dispatcher-pre-tool-heartbeat
Content: single Unix epoch integer (e.g. "1713456789\\n")

See issue #1786 (thinking-freeze mitigations) and #1695 (inference-gap detection).
"""

import os
import sys
import time
from pathlib import Path


WORKSPACE_DIR = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
HEARTBEAT_FILE = Path(
    os.environ.get(
        "LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE",
        WORKSPACE_DIR / "logs" / "dispatcher-pre-tool-heartbeat",
    )
)


def write_heartbeat(heartbeat_file: Path) -> None:
    """Write current Unix epoch to heartbeat_file atomically."""
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = heartbeat_file.with_suffix(".tmp")
    tmp.write_text(str(int(time.time())) + "\n")
    os.rename(str(tmp), str(heartbeat_file))


def main() -> None:
    try:
        write_heartbeat(HEARTBEAT_FILE)
    except Exception:
        # Never block tool execution.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
