#!/usr/bin/env python3
"""
PreToolUse hook: pre-tool-call heartbeat for MCP disconnect detection.

Writes last_pretooluse_at (ISO UTC timestamp) to lobster-state.json BEFORE
each tool call. Unlike the PostToolUse thinking-heartbeat which only fires
after a tool call succeeds, this fires before — capturing the signal even
when a tool call fails or hangs (e.g. when the MCP server has restarted and
the dispatcher's MCP connection is broken).

Why this matters (issue #1439):
  When the MCP server crashes mid-session, the dispatcher's wait_for_messages
  call hangs or fails silently. No PostToolUse event fires for a failed call,
  so the thinking-heartbeat stops updating. The health check sees a stale
  heartbeat and may wait the full WFM_STALE_SECONDS (20 min) before detecting
  the frozen dispatcher.

  A PreToolUse heartbeat still fires even when the tool call ultimately fails:
    1. Dispatcher calls wait_for_messages() → PreToolUse fires → heartbeat written
    2. MCP call fails/hangs → no PostToolUse → thinking-heartbeat NOT written
    3. Health check reads last_pretooluse_at — sees dispatcher was trying

  This narrows the detection window for MCP-disconnect freezes, since the
  dispatcher typically retries or enters a loop when MCP calls fail.

Design:
- Unconditional: fires on every PreToolUse (no tool-name filtering needed)
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Merge: read existing JSON, update last_pretooluse_at, write back — no overwrite
- Silent on failure: health check degrades gracefully when field is absent
- No MCP dependency: pure filesystem write, unaffected by MCP restart
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
STATE_FILE = Path(os.environ.get("LOBSTER_STATE_FILE_OVERRIDE", MESSAGES_DIR / "config" / "lobster-state.json"))


def _read_state(path: Path) -> dict:
    """Return existing state dict, or empty dict if file is absent or unparseable."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state_atomic(path: Path, state: dict) -> None:
    """Write state dict atomically: write to .tmp then rename."""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.rename(str(tmp), str(path))


def write_pretooluse_heartbeat(state_file: Path) -> None:
    """Merge last_pretooluse_at into state_file, creating it if absent."""
    state = _read_state(state_file)
    state["last_pretooluse_at"] = datetime.now(timezone.utc).isoformat()
    _write_state_atomic(state_file, state)


def main() -> None:
    try:
        write_pretooluse_heartbeat(STATE_FILE)
    except Exception:
        # Never block tool execution — health check degrades gracefully if field is absent
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
