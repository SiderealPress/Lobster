#!/usr/bin/env python3
"""
SubagentStop hook: ensure lobster-auditor sessions update system-audit.context.md.

For non-auditor sessions this hook is a no-op (exits 0 immediately).

For auditor sessions it enforces one of two exit conditions:
  1. system-audit.context.md was modified during this session
     (mtime >= session start time found in the transcript), OR
  2. the transcript contains the safe word AUDIT_CONTEXT_UNCHANGED
     (agent explicitly confirmed that nothing new was found).

If neither condition is met the hook prints an error message and exits 2,
hard-blocking the session from terminating until the agent complies.

Detection strategy for "is this an auditor session":
  Scan the transcript for a ReadFile tool call whose path contains
  "system-audit.context.md". The auditor definition requires reading this
  file at session start, so its presence is a reliable signal.
"""
import json
import os
import sys
import time
from pathlib import Path

CONTEXT_FILE = Path(os.path.expanduser(
    "~/lobster-user-config/agents/system-audit.context.md"
))

SAFE_WORD = "AUDIT_CONTEXT_UNCHANGED"

# Path fragment used to detect auditor sessions in the transcript.
AUDIT_CONTEXT_FILENAME = "system-audit.context.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_tool_calls(transcript: list) -> list[dict]:
    """Return all tool_use blocks from the transcript."""
    tool_calls = []
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_calls.append(item)
    return tool_calls


def _is_auditor_session(tool_calls: list[dict]) -> bool:
    """Return True if any tool call reads system-audit.context.md.

    The auditor subagent definition requires reading this file at the start of
    every session.  A Read/cat/Bash call whose input references the filename is
    sufficient evidence.
    """
    for call in tool_calls:
        name = call.get("name", "")
        # Check Read tool (Claude Code built-in)
        if name == "Read":
            path = call.get("input", {}).get("file_path", "")
            if AUDIT_CONTEXT_FILENAME in path:
                return True
        # Check Bash tool — auditor might cat/head the file
        if name == "Bash":
            cmd = call.get("input", {}).get("command", "")
            if AUDIT_CONTEXT_FILENAME in cmd:
                return True
    return False


def _safe_word_in_transcript(tool_calls: list[dict]) -> bool:
    """Return True if write_result was called with AUDIT_CONTEXT_UNCHANGED."""
    for call in tool_calls:
        if call.get("name") != "mcp__lobster-inbox__write_result":
            continue
        inp = call.get("input", {})
        text = inp.get("text", "")
        if SAFE_WORD in text:
            return True
    return False


def _session_start_time(hook_input: dict) -> float | None:
    """Estimate session start time from the transcript's first message timestamp.

    Falls back to None if no timestamp is available (hook input varies).
    """
    # Claude Code hook input may carry a top-level timestamp or we can parse
    # the transcript for the earliest role='user' message timestamp.
    ts = hook_input.get("session_start_time") or hook_input.get("timestamp")
    if ts:
        try:
            return float(ts)
        except (TypeError, ValueError):
            pass

    # Try to find the minimum timestamp inside transcript messages.
    transcript = hook_input.get("transcript", [])
    min_ts = None
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        t = msg.get("timestamp")
        if t:
            try:
                t_float = float(t)
                if min_ts is None or t_float < min_ts:
                    min_ts = t_float
            except (TypeError, ValueError):
                continue
    return min_ts


def _context_file_updated_since(since: float | None) -> bool:
    """Return True if system-audit.context.md was modified at or after `since`.

    If `since` is None (unknown session start), we cannot verify via mtime —
    return False so the safe word remains the only exit path.
    """
    if since is None:
        return False
    try:
        mtime = CONTEXT_FILE.stat().st_mtime
        # Allow a 1-second clock skew margin.
        return mtime >= (since - 1.0)
    except OSError:
        # File doesn't exist yet — not updated.
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # Unreadable input — never block

    transcript = hook_input.get("transcript", [])
    tool_calls = _extract_tool_calls(transcript)

    # Fast path: not an auditor session — pass through.
    if not _is_auditor_session(tool_calls):
        sys.exit(0)

    # --- Auditor session detected ---

    # Condition 1: context file was updated during this session.
    session_start = _session_start_time(hook_input)
    if _context_file_updated_since(session_start):
        sys.exit(0)

    # Condition 2: transcript contains the explicit safe word.
    if _safe_word_in_transcript(tool_calls):
        sys.exit(0)

    # Neither condition met — block exit.
    print(
        "Error: lobster-auditor session ended without updating "
        "system-audit.context.md. "
        "Either update the file with your findings, or include "
        f"{SAFE_WORD!r} as the first line of your write_result call "
        "if nothing new was found."
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
