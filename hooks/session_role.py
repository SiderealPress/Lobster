#!/usr/bin/env python3
"""
Shared utility: dispatcher vs subagent session discrimination.

## Session-context API — `is_dispatcher(hook_input)`

For use in **SessionStart / SubagentStop / Stop hooks**.

Simplified detection (issue #1908): checks the launcher-written startup flag
file at ~/lobster-workspace/data/dispatcher-startup-flag. The launcher
(claude-persistent.sh) writes its subshell PID to this file before exec-ing
claude. If the file exists and the PID is still alive (kill -0), the session
is the dispatcher. The flag is deleted by inject-bootup-context.py after
detection so subagents never see it.

2. **Transcript fallback (secondary)**: Scan the transcript for tool_use blocks
   containing the dispatcher-only tools `wait_for_messages` or `check_inbox`.
   CC 2.1.76+ passes a file path (`transcript_path` for Stop hooks,
   `agent_transcript_path` for SubagentStop hooks) rather than an inline
   `transcript` list. Both file-based and inline forms are tried in order.
   Found → dispatcher.  Not found → subagent.

For use in **PreToolUse hooks** where an `agent_id` field is injected by
CC for subagent sessions (absent for the dispatcher), and where the
process-tree can supplement the state-file check when the system is very
early in boot (before `session_start` has been called).

Strategy: agent_id fast path → MCP state files → process-tree walk →
env-var-only fallback.

Use `is_dispatcher_session` in PreToolUse hooks that guard dispatcher-only
behaviour (e.g. post-compact-gate).  Use `is_dispatcher` for SessionStart /
SubagentStop / Stop hooks.

NOTE: is_dispatcher_session() is intentionally left unchanged from the pre-
simplification version (issue #1908 MUST-FIX). It is still needed by PreToolUse
hooks during active processing when the startup flag has already been consumed.
"""

import os
import subprocess
from pathlib import Path

# Tmux session name for the process-tree fallback used by is_dispatcher_session().
_LOBSTER_TMUX_SESSION = os.environ.get("LOBSTER_TMUX_SESSION", "lobster")

# Tertiary: hook marker file (kept for on-compact.py compatibility).
DISPATCHER_SESSION_FILE = Path(
    os.path.expanduser("~/messages/config/dispatcher-session-id")
)

# Tools that only the dispatcher calls — kept for reference / external callers.
DISPATCHER_ONLY_TOOLS = {
    "mcp__lobster-inbox__wait_for_messages",
    "mcp__lobster-inbox__check_inbox",
}


def _get_startup_flag_file() -> Path:
    """Return the dispatcher startup flag file path, resolved at call time.

    Reads LOBSTER_WORKSPACE on every call so tests can override the env var.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-startup-flag"


# Module-level alias so tests can patch STARTUP_FLAG_FILE directly.
STARTUP_FLAG_FILE = _get_startup_flag_file()


def _get_mcp_session_state_file() -> Path:
    """Return the MCP HTTP session state file path (kept for compatibility)."""
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-session-id"


def _get_mcp_claude_session_file() -> Path:
    """Return the MCP Claude UUID state file path (kept for on-compact.py compat)."""
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-claude-session-id"


# Module-level aliases for test patching convenience.
MCP_SESSION_STATE_FILE = _get_mcp_session_state_file()
MCP_CLAUDE_SESSION_FILE = _get_mcp_claude_session_file()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_session_id(hook_input: dict) -> str | None:
    """Return the session_id from hook JSON input, or None if absent."""
    return hook_input.get("session_id") or None


def is_dispatcher(hook_input: dict) -> bool:  # noqa: ARG001
    """Return True if the current session is the Lobster dispatcher.

    Simplified detection (issue #1908): reads the startup flag file written by
    the launcher (claude-persistent.sh). Live PID in the flag = dispatcher.
    Flag absent or dead PID = subagent.

    hook_input is accepted for API compatibility but is not used — the startup
    flag is the sole detection signal for SessionStart hooks.
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
    """Write session_id to the hook dispatcher marker file.

    Kept for on-compact.py compatibility. No longer used by is_dispatcher().
    Atomic write via a .tmp rename. Silent on any failure.
    """
    try:
        DISPATCHER_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = DISPATCHER_SESSION_FILE.with_suffix(".tmp")
        tmp_path.write_text(session_id.strip())
        tmp_path.replace(DISPATCHER_SESSION_FILE)  # atomic on Linux
    except Exception:  # noqa: BLE001
        pass


def write_dispatcher_claude_session_id(session_id: str) -> None:
    """Write session_id to the primary MCP Claude UUID state file.

    Kept for on-compact.py compatibility. No longer read by is_dispatcher().
    Atomic write via a .tmp rename.  Silent on any failure.
    """
    try:
        path = _get_mcp_claude_session_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(session_id.strip())
        tmp_path.replace(path)  # atomic on Linux
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internal helpers (kept for on-compact.py compatibility)
# ---------------------------------------------------------------------------


def _read_session_id_from_file(path: Path) -> "str | None | OSError":
    """Return the session ID stored in a plain-text state file."""
    try:
        if not path.exists():
            return None
        value = path.read_text().strip()
        return value or None
    except OSError as exc:
        return exc


def _check_state_file(path: Path, session_id: "str | None") -> "bool | None":
    """Compare session_id against a plain-text state file.

    Kept for on-compact.py and is_dispatcher_session() compatibility.
    """
    result = _read_session_id_from_file(path)
    if isinstance(result, OSError):
        return True  # fail open
    stored = result
    if stored is None:
        return None
    if session_id is None:
        return None
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
