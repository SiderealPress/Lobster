#!/usr/bin/env python3
"""
PreToolUse gate: after context compaction, block all tool calls until
wait_for_messages is called. This forces the dispatcher back into its
main loop before doing anything else.

The sentinel file ~/messages/config/compact-pending is written by
on-compact.py when a compaction occurs. This hook passes when:
  1. No sentinel file exists (normal operation), OR
  2. LOBSTER_MAIN_SESSION != "1" (not the dispatcher — skip for subagents), OR
  3. The sentinel is older than SENTINEL_TTL_SECONDS (stale / post-hibernation), OR
  4. The tool being called IS wait_for_messages (let it through, delete sentinel).

The TTL (10 minutes) eliminates the stuck-sentinel failure mode: if the
dispatcher hibernates or crashes while the sentinel is active, the next boot
finds a stale sentinel and the gate passes immediately. No deadlock.

## Settings.json configuration

Add this to ~/.claude/settings.json under "hooks" → "PreToolUse":

    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "python3 /home/lobster/lobster/hooks/post-compact-gate.py",
          "timeout": 5
        }
      ]
    }

The empty string matcher fires on every tool call.
"""

import json
import os
import sys
import time
from pathlib import Path

SENTINEL_FILE = Path(os.path.expanduser("~/messages/config/compact-pending"))
SENTINEL_TTL_SECONDS = 600  # 10 minutes — treats stale sentinel as harmless

WAIT_FOR_MESSAGES_TOOL = "mcp__lobster-inbox__wait_for_messages"

DENY_REASON = (
    "Context was just compacted. You must call wait_for_messages() first to "
    "re-enter the dispatcher main loop and read the compact reminder. "
    "Do not perform any other action until you have called wait_for_messages()."
)


def is_main_session() -> bool:
    """Return True only for the dispatcher process, not subagents."""
    return os.environ.get("LOBSTER_MAIN_SESSION", "") == "1"


def sentinel_is_fresh() -> bool:
    """Return True if the sentinel exists and is recent enough to enforce."""
    if not SENTINEL_FILE.exists():
        return False
    try:
        age = time.time() - SENTINEL_FILE.stat().st_mtime
        return age < SENTINEL_TTL_SECONDS
    except OSError:
        return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Only enforce for the main dispatcher session.
    if not is_main_session():
        sys.exit(0)

    tool_name = data.get("tool_name", "")

    # If the tool IS wait_for_messages, clear the sentinel and allow it through.
    if tool_name == WAIT_FOR_MESSAGES_TOOL:
        try:
            SENTINEL_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        sys.exit(0)

    # If sentinel is absent or stale, allow everything through.
    if not sentinel_is_fresh():
        sys.exit(0)

    # Sentinel is fresh and tool is not wait_for_messages — deny.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
