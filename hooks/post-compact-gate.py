#!/usr/bin/env python3
"""
PreToolUse gate: after context compaction, block all tool calls until
wait_for_messages is called. This forces the dispatcher back into its
main loop before doing anything else.

The sentinel file ~/messages/config/compact-pending is written by
on-compact.py when a compaction occurs. This hook passes when:
  1. No sentinel file exists (normal operation), OR
  2. Not the dispatcher session (see session_role.is_dispatcher()), OR
  3. The sentinel is older than SENTINEL_TTL_SECONDS (stale / post-hibernation), OR
  4. The tool being called IS wait_for_messages (let it through, delete sentinel).

The TTL (10 minutes) eliminates the stuck-sentinel failure mode: if the
dispatcher hibernates or crashes while the sentinel is active, the next boot
finds a stale sentinel and the gate passes immediately. No deadlock.

## Dispatcher vs subagent detection

Detection is performed by is_dispatcher_session(), which uses a layered strategy:

0. agent_id field (fast path): Claude Code injects agent_id into PreToolUse
   payloads only for subagent sessions.  It is absent for the top-level
   dispatcher.  If present → immediately return False (subagent).  No
   filesystem I/O required.  This is the primary check for the common case.
   See issue #1152.

1. MCP state file: The running MCP server writes the dispatcher session ID to
   ~/lobster-workspace/data/dispatcher-session-id.  NOTE: this file stores an
   HTTP MCP session ID, not a CC UUID; it will never match the hook session_id
   field in practice (namespace mismatch — see issue #1151).  Retained for
   belt-and-suspenders; effectively a no-op in hook context.

2. Hook marker file (secondary): At dispatcher startup, on-compact.py writes the
   session ID to ~/messages/config/dispatcher-session-id.  This is used by
   is_dispatcher_session() for PreToolUse hooks during active processing
   (after the startup flag has been consumed by inject-bootup-context.py).

3. Process-tree fallback: If neither state file is present or gives a definitive
   answer, walk the process tree upward.  Two consecutive claude-like ancestors
   before reaching a tmux pane PID → subagent.  One or fewer → dispatcher.
   See _is_dispatcher_by_process_tree() below.

4. Env-var-only fallback: If tmux is unavailable, fall back to
   LOBSTER_MAIN_SESSION=1 alone.

## Settings.json configuration

Add this to ~/.claude/settings.json under "hooks" → "PreToolUse":

    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "python3 $HOME/lobster/hooks/post-compact-gate.py",
          "timeout": 5
        }
      ]
    }

Note: $HOME is expanded by the shell at runtime, so this works for any username.

The empty string matcher fires on every tool call.
"""

import json
import os
import sys
import time
from pathlib import Path

# Make hooks/ importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).parent))

from session_role import is_dispatcher_session  # noqa: E402 — after sys.path insertion

SENTINEL_FILE = Path(os.path.expanduser("~/messages/config/compact-pending"))
SENTINEL_TTL_SECONDS = 600  # 10 minutes — treats stale sentinel as harmless
LOG_FILE = Path(os.path.expanduser("~/lobster-workspace/logs/compact-gate.log"))

WAIT_FOR_MESSAGES_TOOL = "mcp__lobster-inbox__wait_for_messages"

# ToolSearch is a read-only schema fetch.  It is always allowed through the gate
# even when compact-pending is active, because the dispatcher needs it to load the
# deferred wait_for_messages schema before it can call wait_for_messages.
# HTTP MCP servers in Claude Code register all tools as deferred by default, so
# ToolSearch is the only path to make wait_for_messages callable.  Blocking it
# causes a deadlock: the gate says "call WFM", but WFM is unresolvable without
# ToolSearch first.  See issue #1914 staging infinite-compaction-loop bug.
TOOL_SEARCH_TOOL = "ToolSearch"

CONFIRMATION_TOKEN = "LOBSTER_COMPACTED_REORIENTED"  # noqa: S105 — not a secret, intentional safe word

DENY_REASON_NEEDS_TOKEN = (
    "GATE BLOCKED: Context compaction was just detected. "
    "If you need the wait_for_messages schema, call ToolSearch first (it is allowed). "
    "Then call `mcp__lobster-inbox__wait_for_messages(confirmation='LOBSTER_COMPACTED_REORIENTED')` directly. "
    "Confirmation token: LOBSTER_COMPACTED_REORIENTED"
)

DENY_REASON = (
    "GATE BLOCKED: Context compaction was just detected. Your only permitted "
    "actions right now are: (1) ToolSearch to load the wait_for_messages schema if needed, "
    "then (2) call `mcp__lobster-inbox__wait_for_messages(confirmation='LOBSTER_COMPACTED_REORIENTED')` directly. "
    "When it returns, you will receive a compact-reminder system message — read it to re-orient "
    "as the Lobster dispatcher, then resume your main loop normally. "
    "Do not retry this tool call."
)

def log_gate_event(tool_name: str, action: str) -> None:
    """Append a JSON log line to compact-gate.log. Silent on any failure."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = json.dumps({"ts": ts, "tool": tool_name, "action": action}) + "\n"
        with LOG_FILE.open("a") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


def sentinel_is_fresh() -> bool:
    """Return True if the sentinel exists and is recent enough to enforce."""
    if not SENTINEL_FILE.exists():
        return False
    try:
        age = time.time() - SENTINEL_FILE.stat().st_mtime
        return 0 <= age < SENTINEL_TTL_SECONDS  # Guard against clock skew / negative age
    except OSError:
        return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # Fast path: agent_id is present only in subagent PreToolUse payloads.
    # The dispatcher never has agent_id.  Exit immediately without any file I/O.
    # NOTE: agent_id is NOT available in SessionStart hooks; this check is only
    # valid here in PreToolUse context.  See issue #1152.
    if data.get("agent_id"):
        sys.exit(0)

    # Only enforce for the main dispatcher session.
    if not is_dispatcher_session(data):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # ToolSearch is always allowed through even when the sentinel is active.
    # HTTP MCP servers register all tools as deferred; ToolSearch is the only way
    # to fetch the wait_for_messages schema so the dispatcher can call it.
    # Blocking ToolSearch creates a deadlock: gate says "call WFM", but WFM is
    # unresolvable without schema pre-load.  ToolSearch is read-only — no side
    # effects and no risk of bypassing the intent of the gate.
    if tool_name == TOOL_SEARCH_TOOL:
        log_gate_event(tool_name, "allowed-schema-fetch")
        sys.exit(0)

    # If the tool IS wait_for_messages, handle based on sentinel state.
    if tool_name == WAIT_FOR_MESSAGES_TOOL:
        if sentinel_is_fresh():
            # Sentinel is active — require the confirmation token.
            confirmation = tool_input.get("confirmation", "")
            if confirmation == CONFIRMATION_TOKEN:
                # Correct token: clear the sentinel and allow through.
                try:
                    SENTINEL_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                log_gate_event(tool_name, "cleared")
                sys.exit(0)
            else:
                # No or wrong token: deny with instructions.
                log_gate_event(tool_name, "blocked-needs-token")
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": DENY_REASON_NEEDS_TOKEN,
                    }
                }))
                sys.exit(0)
        else:
            # No active sentinel: wait_for_messages passes through normally
            # regardless of confirmation param.
            log_gate_event(tool_name, "cleared")
            sys.exit(0)

    # If sentinel is absent or stale, allow everything through.
    # Delete a stale sentinel so it doesn't linger as an orphan file.
    if not sentinel_is_fresh():
        if SENTINEL_FILE.exists():
            try:
                SENTINEL_FILE.unlink(missing_ok=True)
                log_gate_event(tool_name, "stale-sentinel-deleted")
            except OSError:
                pass
        sys.exit(0)

    # Sentinel is fresh and tool is not wait_for_messages — deny.
    log_gate_event(tool_name, "blocked")
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
