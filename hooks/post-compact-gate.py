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

Detection is delegated to session_role.is_dispatcher(), which uses a layered
strategy:

1. Marker file (primary): At dispatcher startup, write-dispatcher-session-id.py
   (a SessionStart hook) writes the session ID to
   ~/messages/config/dispatcher-session-id. This hook reads that file and
   compares it to the session_id in the hook JSON input. Match → dispatcher.

2. Process-tree fallback (secondary): If the marker file is absent or
   unreadable, walk the process tree upward. Two consecutive claude-like
   ancestors before reaching a tmux pane PID → subagent. One or fewer →
   dispatcher. See _is_dispatcher_by_process_tree() below.

3. Env-var-only fallback: If tmux is unavailable, fall back to
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
import subprocess
import sys
import time
from pathlib import Path

# Make hooks/ importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).parent))

SENTINEL_FILE = Path(os.path.expanduser("~/messages/config/compact-pending"))
SENTINEL_TTL_SECONDS = 600  # 10 minutes — treats stale sentinel as harmless
LOG_FILE = Path(os.path.expanduser("~/lobster-workspace/logs/compact-gate.log"))

WAIT_FOR_MESSAGES_TOOL = "mcp__lobster-inbox__wait_for_messages"

CONFIRMATION_TOKEN = "LOBSTER_COMPACTED_REORIENTED"  # noqa: S105 — not a secret, intentional safe word

DENY_REASON_NEEDS_TOKEN = (
    "GATE BLOCKED: Context compaction was just detected. "
    "Read `~/lobster-workspace/.claude/dispatcher.bootup.md` for the confirmation token, "
    "then call `mcp__lobster-inbox__wait_for_messages(confirmation='LOBSTER_COMPACTED_REORIENTED')` directly. "
    "No ToolSearch needed — the MCP schema is pre-registered."
)

DENY_REASON = (
    "GATE BLOCKED: Context compaction was just detected. Your only permitted "
    "action right now is to call `mcp__lobster-inbox__wait_for_messages` by its full name directly — "
    "no ToolSearch needed, the schema is pre-registered. When it returns, you will "
    "receive a compact-reminder system message — read it to re-orient as the "
    "Lobster dispatcher, then resume your main loop normally. Do not retry this "
    "tool call."
)

LOBSTER_TMUX_SESSION = os.environ.get("LOBSTER_TMUX_SESSION", "lobster")


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


def _get_tmux_pane_pids() -> set[str]:
    """Return the set of PIDs for all panes in the lobster tmux session."""
    try:
        result = subprocess.run(
            [
                "tmux", "-L", LOBSTER_TMUX_SESSION,
                "list-panes", "-t", LOBSTER_TMUX_SESSION,
                "-F", "#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if result.returncode == 0 and result.stdout.strip():
            return set(result.stdout.strip().split("\n"))
    except Exception:  # noqa: BLE001
        pass
    return set()


def _get_proc_name(pid: int) -> str:
    """Return the comm (process name) for a given PID, or '' on failure."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def _get_ppid(pid: int) -> int | None:
    """Return the parent PID of a given PID, or None on failure."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            # Format: pid (comm) state ppid ...
            content = f.read()
            # rsplit on ')' to handle commas in comm name
            after_comm = content.rsplit(")", 1)[-1]
            ppid = int(after_comm.split()[1])
            return ppid if ppid > 1 else None
    except (OSError, ValueError, IndexError):
        return None


def _is_claude_process(name: str) -> bool:
    """Return True if the process name looks like a Claude Code binary."""
    return "claude" in name.lower()


def _is_dispatcher_by_process_tree() -> bool:
    """Return True only when this hook is running inside the dispatcher Claude.

    Process-tree fallback used when the session_role marker file is absent.

    Strategy:
      1. Must have LOBSTER_MAIN_SESSION=1 (env var set by claude-persistent.sh).
         This is a necessary condition — if not set, definitely not the dispatcher.
      2. Walk the process tree upward from this hook process. Count consecutive
         'claude' ancestors before reaching a tmux pane PID:
           - 0 or 1 claude ancestor  → dispatcher (hook → dispatcher claude → tmux)
           - 2+ claude ancestors     → subagent (hook → subagent claude → dispatcher claude → tmux)
      3. If the tmux check is unavailable (tmux not running, etc.), fall back
         to the env-var-only check — maintaining prior imprecise behaviour.

    Fails open for non-main-session processes (returns False if uncertain).
    """
    # Necessary condition: env var must be set.
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        return False

    tmux_pids = _get_tmux_pane_pids()
    if not tmux_pids:
        # tmux unavailable — fall back to env-var-only (prior behaviour).
        return True

    claude_ancestor_count = 0
    pid = os.getpid()
    for _ in range(15):  # Safety limit — should never need more than ~5 levels
        ppid = _get_ppid(pid)
        if ppid is None:
            break
        if str(ppid) in tmux_pids:
            # Reached the tmux pane. Dispatcher has ≤1 claude ancestor above
            # this hook; subagents have ≥2.
            return claude_ancestor_count <= 1
        parent_name = _get_proc_name(ppid)
        if _is_claude_process(parent_name):
            claude_ancestor_count += 1
        pid = ppid

    # Could not confirm via process tree — fall back to env var.
    return True


def is_dispatcher_session(hook_input: dict) -> bool:
    """Return True when this hook is running inside the dispatcher Claude.

    Uses session_role.is_dispatcher() (marker-file + transcript) as the primary
    check. Falls back to the process-tree walk when neither signal is available
    (e.g. marker file not yet written on first boot, no transcript in PreToolUse).
    """
    # Primary: marker file + transcript (via session_role).
    # session_role returns False by default when no signal is found; we need to
    # distinguish "definitely subagent" from "no signal" to know when to apply
    # the process-tree fallback. We probe the marker file directly here.
    from session_role import (
        _check_marker_file,
        get_session_id,
        _transcript_has_dispatcher_tool,
        DISPATCHER_SESSION_FILE,
    )

    session_id = get_session_id(hook_input)
    marker_result = _check_marker_file(session_id)

    if marker_result is not None:
        # Marker file exists and gave a definitive answer.
        return marker_result

    # Transcript fallback (Stop hooks only — transcript absent in PreToolUse).
    transcript = hook_input.get("transcript")
    if transcript is not None:
        return _transcript_has_dispatcher_tool(transcript)

    # No session_role signal available — fall back to process-tree.
    return _is_dispatcher_by_process_tree()


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

    # Only enforce for the main dispatcher session.
    if not is_dispatcher_session(data):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

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
    if not sentinel_is_fresh():
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
