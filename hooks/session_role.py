#!/usr/bin/env python3
"""
Shared utility: dispatcher vs subagent session discrimination.

Provides a single `is_dispatcher(hook_input)` predicate that all hooks can
import to determine whether the current Claude Code session is the Lobster
dispatcher or a background subagent.

## Detection strategy (layered)

1. **Marker file (primary)**: At dispatcher startup the session ID is written to
   `~/messages/config/dispatcher-session-id`. This hook reads that file and
   compares to the `session_id` field in the hook JSON input.
   Match → dispatcher.  Mismatch or file absent → try fallback.

2. **Transcript fallback (secondary)**: Scan the transcript for tool_use blocks
   containing the dispatcher-only tools `wait_for_messages` or `check_inbox`.
   CC 2.1.76+ passes a file path (`transcript_path` for Stop hooks,
   `agent_transcript_path` for SubagentStop hooks) rather than an inline
   `transcript` list. Both file-based and inline forms are tried in order.
   Found → dispatcher.  Not found → subagent.

3. **Default**: If neither signal is available (e.g. a PreToolUse hook with no
   marker file), return False (treat as subagent = safe/conservative).

## Writing the marker file

Call `write_dispatcher_session_id(session_id)` at dispatcher startup.
Typically invoked from a `SessionStart` hook or from the dispatcher bootup
script via a small wrapper.
"""

import json
import os
from pathlib import Path

DISPATCHER_SESSION_FILE = Path(
    os.path.expanduser("~/messages/config/dispatcher-session-id")
)

# Tools that only the dispatcher calls — used as transcript fallback signal.
DISPATCHER_ONLY_TOOLS = {
    "mcp__lobster-inbox__wait_for_messages",
    "mcp__lobster-inbox__check_inbox",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_session_id(hook_input: dict) -> str | None:
    """Return the session_id from hook JSON input, or None if absent."""
    return hook_input.get("session_id") or None


def is_dispatcher(hook_input: dict) -> bool:
    """Return True if the current session is the Lobster dispatcher.

    Checks marker file first; falls back to transcript scan if the marker file
    is absent or unreadable.  Returns False (subagent) when no signal is found.
    """
    session_id = get_session_id(hook_input)

    # --- Primary: marker file ---
    marker_result = _check_marker_file(session_id)
    if marker_result is not None:
        return marker_result

    # --- Secondary: transcript scan ---
    # Try inline transcript first (legacy CC < 2.1.76).
    transcript = hook_input.get("transcript")
    if transcript is not None:
        return _transcript_has_dispatcher_tool(transcript)

    # Try file-based transcript (CC 2.1.76+):
    #   Stop hook       → transcript_path
    #   SubagentStop    → agent_transcript_path
    for key in ("transcript_path", "agent_transcript_path"):
        path = hook_input.get(key)
        if path:
            transcript = _load_transcript_from_jsonl(path)
            if transcript:
                return _transcript_has_dispatcher_tool(transcript)

    # --- Default: no signal → treat as subagent (conservative) ---
    return False


def write_dispatcher_session_id(session_id: str) -> None:
    """Write session_id to the dispatcher marker file.

    Should be called once at dispatcher startup (e.g. from a SessionStart hook
    or a thin wrapper script).  Atomic write via a .tmp rename so concurrent
    readers never see a partial file.

    Silent on any failure — must never crash the caller.
    """
    try:
        DISPATCHER_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = DISPATCHER_SESSION_FILE.with_suffix(".tmp")
        tmp_path.write_text(session_id.strip())
        tmp_path.replace(DISPATCHER_SESSION_FILE)  # atomic on Linux
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_dispatcher_session_id() -> str | None:
    """Return the stored dispatcher session ID, or None on any failure."""
    try:
        if not DISPATCHER_SESSION_FILE.exists():
            return None
        value = DISPATCHER_SESSION_FILE.read_text().strip()
        return value or None
    except OSError:
        return None


def _check_marker_file(session_id: str | None) -> bool | None:
    """Compare session_id against the marker file.

    Returns:
        True   — session_id matches the stored dispatcher ID.
        False  — marker file exists and session_id does NOT match (→ subagent).
        None   — marker file absent or unreadable; caller should try fallback.
    """
    stored = _read_dispatcher_session_id()
    if stored is None:
        return None  # No marker file — can't decide; use fallback.
    if session_id is None:
        return None  # Session ID not in hook input — can't decide; use fallback.
    return session_id == stored


def _load_transcript_from_jsonl(path: str) -> list:
    """Load transcript messages from a JSONL file.

    CC 2.1.76+ Stop hooks pass transcript_path (a .jsonl file) rather than an
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


def _transcript_has_dispatcher_tool(transcript: list) -> bool:
    """Return True if any tool_use block in transcript calls a dispatcher-only tool.

    Handles both JSONL format (CC 2.1.76+) and legacy inline format:

    JSONL format (each line is a JSONL entry):
        {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

    Legacy inline format (transcript is a list of messages):
        {"role": "assistant", "content": [...]}
    """
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        # JSONL format: content is under msg["message"]["content"]
        # Legacy format: content is directly under msg["content"]
        nested_msg = msg.get("message")
        if isinstance(nested_msg, dict):
            content = nested_msg.get("content", [])
        else:
            content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") in DISPATCHER_ONLY_TOOLS:
                return True
    return False
