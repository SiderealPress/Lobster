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

## SubagentStop transcript handling (CC 2.1.76+)

SubagentStop events in CC 2.1.76+ no longer include an inline `transcript`
field. They only provide `agent_transcript_path` — a path to a JSONL file
containing the subagent's conversation. This hook loads the transcript from
that file path, falling back to the legacy inline `transcript` key for older
CC versions.

## JSONL message format

Each line of the JSONL transcript file has the structure:
    {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

Tool use items are nested under entry["message"]["content"], NOT entry["content"].
`_extract_tool_calls` handles both the JSONL format and the legacy inline
format where content is directly on the message dict.
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


def _load_transcript_from_jsonl(path: str) -> list:
    """Load transcript messages from a JSONL file.

    SubagentStop passes agent_transcript_path (a .jsonl file) rather than an
    inline transcript list. Each line is a JSON object. Returns [] on any error.
    """
    try:
        messages = []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return messages
    except Exception:
        return []


def _extract_tool_calls(transcript: list) -> list[dict]:
    """Return all tool_use blocks from the transcript.

    Handles both JSONL format (CC 2.1.76+) and legacy inline format:

    JSONL format (each line is a JSONL entry):
        {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

    Legacy inline format (transcript is a list of messages):
        {"role": "assistant", "content": [...]}

    Both formats are tried so the hook works regardless of CC version.
    """
    tool_calls = []
    for entry in transcript:
        if not isinstance(entry, dict):
            continue

        # JSONL format: content is under entry["message"]["content"]
        # Legacy format: content is directly under entry["content"]
        nested_msg = entry.get("message")
        if isinstance(nested_msg, dict):
            content = nested_msg.get("content", [])
        else:
            content = entry.get("content", [])

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


def _session_start_time(hook_input: dict, transcript: list) -> float | None:
    """Estimate session start time from the transcript's first message timestamp.

    Falls back to None if no timestamp is available (hook input varies).

    Accepts the already-loaded transcript list (which may have been read from
    a JSONL file) rather than re-reading hook_input["transcript"], which is
    always empty in CC 2.1.76+.
    """
    # Claude Code hook input may carry a top-level timestamp.
    ts = hook_input.get("session_start_time") or hook_input.get("timestamp")
    if ts:
        try:
            return float(ts)
        except (TypeError, ValueError):
            pass

    # Try to find the minimum timestamp inside transcript messages.
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

    # CC 2.1.76+: SubagentStop passes the transcript as a JSONL file at
    # agent_transcript_path rather than inline. Load from the file path,
    # falling back to the legacy inline key for older CC versions.
    transcript_path = hook_input.get("agent_transcript_path", "")
    if transcript_path:
        transcript = _load_transcript_from_jsonl(transcript_path)
    else:
        transcript = hook_input.get("transcript", [])

    tool_calls = _extract_tool_calls(transcript)

    # Fast path: not an auditor session — pass through.
    if not _is_auditor_session(tool_calls):
        sys.exit(0)

    # --- Auditor session detected ---

    # Condition 1: context file was updated during this session.
    session_start = _session_start_time(hook_input, transcript)
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
