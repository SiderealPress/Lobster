#!/usr/bin/env python3
"""
PreToolUse hook: dispatcher pre-tool heartbeat.

Writes the current Unix epoch timestamp to a dedicated heartbeat file when the
current session is the dispatcher. Subagent tool calls are silently ignored so
they cannot keep the health check satisfied while the dispatcher is frozen or dead.

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

Dispatcher-only guard (issue #1897):
- PreToolUse fires for ALL sessions sharing the same Claude Code process,
  including background subagents. Without this guard, a subagent doing heavy
  tool work can keep the heartbeat fresh even if the dispatcher is dead.
- The guard uses is_dispatcher_session() which checks: (0) agent_id fast path
  (subagents always have agent_id in PreToolUse payloads), (1) MCP Claude UUID
  state file, (2) hook marker file (dispatcher-session-id), (3) process-tree
  walk as a last resort. All three state-file paths are written by the SessionStart
  hook that fires for both claude-persistent.sh and claude-interactive.exp launchers.
- Launcher-agnostic: works whether Lobster is started via claude-interactive.exp
  or claude-persistent.sh.

Design
------
- Fires on every PreToolUse (matcher: "")
- Guards on dispatcher session: reads hook input from stdin and calls
  is_dispatcher_session(). Subagent calls exit 0 immediately without file I/O.
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Single integer timestamp — no JSON parsing, no locking, no network
- Silent on failure: health check degrades gracefully when file absent
- < 1ms on warm OS (rename is a kernel atomic op on same-filesystem paths)

File location: ~/lobster-workspace/logs/dispatcher-pre-tool-heartbeat
Content: single Unix epoch integer (e.g. "1713456789\n")

See issue #1786 (thinking-freeze mitigations) and #1695 (inference-gap detection),
and #1897 (subagent-masking fix).
"""

import json
import os
import sys
import time
from pathlib import Path

# Allow imports from the hooks directory (session_role).
_HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(_HOOKS_DIR))

import session_role  # noqa: E402 — path insert must precede this


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
    # Read hook input from stdin (provided by Claude Code on every PreToolUse).
    # If stdin is empty or unparseable, treat as unknown session — skip heartbeat
    # (conservative: better to miss one update than to falsely extend it for a subagent).
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, EOFError):
        sys.exit(0)

    # Guard: only write the heartbeat for the dispatcher session.
    # is_dispatcher_session() uses: agent_id fast path (subagents always have it
    # in PreToolUse payloads) → MCP state files → hook marker file → process-tree.
    if not session_role.is_dispatcher_session(hook_input):
        sys.exit(0)

    try:
        write_heartbeat(HEARTBEAT_FILE)
    except Exception:
        # Never block tool execution.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
