#!/usr/bin/env python3
"""
Shared utility: dispatcher vs subagent session discrimination.

Provides a single `is_dispatcher(hook_input)` predicate that all hooks can
import to determine whether the current Claude Code session is the Lobster
dispatcher or a background subagent.

## Detection strategy (layered)

1. **Claude UUID state file (primary)**: When session_start(agent_type='dispatcher')
   is called, the MCP server writes the Claude session UUID (the agent_id field,
   which is the same UUID that the SessionStart hook receives) to
   `$LOBSTER_WORKSPACE/data/dispatcher-claude-session-id`.  The hook compares the
   `session_id` from hook_input against this file — apples to apples.
   Match → dispatcher.  Mismatch → subagent.  File absent → try next.

2. **HTTP session ID state file**: The MCP server also writes the HTTP session ID
   (32-char hex) to `$LOBSTER_WORKSPACE/data/dispatcher-session-id` via
   `_tag_dispatcher_session()`.  This file uses a different ID space than the
   Claude UUID, so a match here is not expected in normal operation.  It is
   kept as a belt-and-suspenders check for stdio transport mode where HTTP
   session IDs may not be in play.
   Match → dispatcher.  Mismatch → try next.  File absent → try next.

3. **Hook marker file**: At dispatcher startup the SessionStart hook
   (`write-dispatcher-session-id.py`) writes the session ID to
   `~/messages/config/dispatcher-session-id`.  Used as fallback when neither
   MCP state file is available.
   Match → dispatcher.  Mismatch → subagent.  File absent → try env var.

4. **LOBSTER_MAIN_SESSION env var (defense-in-depth)**: If the env var
   LOBSTER_MAIN_SESSION=1 is set and all state files are absent or unreadable,
   assume dispatcher.  This catches the narrow race window between MCP restart
   and the first session_start(agent_type='dispatcher') call, where no state
   file exists yet.  Only fires when no conflicting file evidence exists.

5. **Default**: If none of the above resolve, return False (conservative/subagent).

The transcript-scanning fallback that existed in previous versions has been
removed.  It was fragile (CC JSONL format changes, same-week compaction bug
tracked in PR #1076) and is now superseded by the MCP state files.

## Writing the marker file

Call `write_dispatcher_session_id(session_id)` at dispatcher startup.
Typically invoked from the `write-dispatcher-session-id.py` SessionStart hook.
The MCP server also calls `_write_dispatcher_state_file()` and
`_write_dispatcher_claude_session_file()` internally; hooks do not need to
call those paths directly.
"""

import os
from pathlib import Path

# Secondary: hook marker file (written by write-dispatcher-session-id.py SessionStart hook)
# Resolved at import time — stable across calls.
DISPATCHER_SESSION_FILE = Path(
    os.path.expanduser("~/messages/config/dispatcher-session-id")
)

# Tools that only the dispatcher calls — kept for reference / external callers.
# No longer used internally for dispatcher detection (transcript scan removed).
DISPATCHER_ONLY_TOOLS = {
    "mcp__lobster-inbox__wait_for_messages",
    "mcp__lobster-inbox__check_inbox",
}


def _get_mcp_session_state_file() -> Path:
    """Return the MCP HTTP-session state file path, resolved at call time.

    Reads LOBSTER_WORKSPACE on every call so tests can override the env var
    without having to patch a module-level constant.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-session-id"


def _get_mcp_claude_session_state_file() -> Path:
    """Return the MCP Claude-UUID state file path, resolved at call time.

    This file stores the Claude session UUID (written when
    session_start(agent_type='dispatcher') is called).  Unlike the HTTP-session
    state file, the ID stored here matches the session_id field in hook_input.

    Reads LOBSTER_WORKSPACE on every call so tests can override the env var
    without having to patch a module-level constant.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-claude-session-id"


# Module-level alias for test patching convenience — tests that set LOBSTER_WORKSPACE
# can also patch this directly.  Updated lazily if needed.
MCP_SESSION_STATE_FILE = _get_mcp_session_state_file()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_session_id(hook_input: dict) -> str | None:
    """Return the session_id from hook JSON input, or None if absent."""
    return hook_input.get("session_id") or None


def is_dispatcher(hook_input: dict) -> bool:
    """Return True if the current session is the Lobster dispatcher.

    Checks state files in priority order, then falls back to the LOBSTER_MAIN_SESSION
    env var.  Returns False when no evidence is found (conservative default).

    Fail-open behavior: if a file exists but cannot be read due to an OS
    error, returns True (same conservative fail-open as before) so the
    dispatcher is never incorrectly blocked by a transient I/O error.
    """
    session_id = get_session_id(hook_input)

    # --- Primary: Claude UUID state file (correct ID space — apples to apples) ---
    claude_result = _check_state_file(_get_mcp_claude_session_state_file(), session_id)
    if claude_result is not None:
        return claude_result

    # --- Secondary: HTTP session ID state file (different ID space; kept for belt-and-suspenders) ---
    http_result = _check_state_file(_get_mcp_session_state_file(), session_id)
    if http_result is not None:
        return http_result

    # --- Tertiary: hook marker file ---
    hook_result = _check_state_file(DISPATCHER_SESSION_FILE, session_id)
    if hook_result is not None:
        return hook_result

    # --- Fix 3 (defense-in-depth): LOBSTER_MAIN_SESSION env var ---
    # Only fires when all state files are absent (no conflicting evidence).
    # This covers the race window between MCP restart and the first
    # session_start(agent_type='dispatcher') call.
    if os.environ.get("LOBSTER_MAIN_SESSION") == "1":
        return True

    # --- Default: no state file present → treat as subagent (conservative) ---
    return False


def write_dispatcher_session_id(session_id: str) -> None:
    """Write session_id to the hook dispatcher marker file.

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


def _read_session_id_from_file(path: Path) -> "str | None | OSError":
    """Return the session ID stored in a plain-text state file.

    Returns:
        str       — the session ID (non-empty string).
        None      — file absent or empty (no stored ID).
        OSError   — an I/O error occurred reading the file.
    """
    try:
        if not path.exists():
            return None
        value = path.read_text().strip()
        return value or None
    except OSError as exc:
        return exc


def _check_state_file(path: Path, session_id: "str | None") -> "bool | None":
    """Compare session_id against a plain-text state file.

    Returns:
        True   — session_id matches the stored dispatcher ID.
        False  — file exists and session_id does NOT match (→ subagent).
        None   — file absent, empty, or session_id unavailable; caller should
                 try next fallback.

    Fail-open: if the file exists but reading it raises an OSError (e.g.
    permissions, concurrent deletion), returns True so the dispatcher is never
    incorrectly blocked by a transient I/O error.
    """
    result = _read_session_id_from_file(path)
    if isinstance(result, OSError):
        return True  # fail open — can't read the file, assume dispatcher
    stored = result
    if stored is None:
        return None  # file absent or empty — can't decide; try next fallback
    if session_id is None:
        return None  # session ID not in hook input — can't decide; try next fallback
    return session_id == stored


# Keep for backwards-compat: callers that imported _read_dispatcher_session_id directly.
def _read_dispatcher_session_id() -> "str | None":
    """Return the stored dispatcher session ID from the hook marker file, or None."""
    result = _read_session_id_from_file(DISPATCHER_SESSION_FILE)
    if isinstance(result, OSError):
        return None
    return result
