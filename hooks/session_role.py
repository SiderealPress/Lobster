#!/usr/bin/env python3
"""
Shared utility: dispatcher vs subagent session discrimination.

Provides a single `is_dispatcher(hook_input)` predicate that all hooks can
import to determine whether the current Claude Code session is the Lobster
dispatcher or a background subagent.

## Detection strategy (layered)

1. **MCP state file (primary)**: The MCP server writes the current dispatcher
   session ID to `$LOBSTER_WORKSPACE/data/dispatcher-session-id` (defaulting to
   `~/lobster-workspace/data/dispatcher-session-id`) whenever
   `_tag_dispatcher_session()` is called (Options A, B, or C).  The file is
   cleared when the MCP server starts, so a stale ID from a previous run never
   lingers.  This hook reads that file and compares to the `session_id` field in
   the hook JSON input.
   Match → dispatcher.  Mismatch → subagent.  File absent → try fallback.

2. **Hook marker file (secondary)**: At dispatcher startup the SessionStart hook
   (`write-dispatcher-session-id.py`) writes the session ID to
   `~/messages/config/dispatcher-session-id`.  Used as fallback when the MCP
   state file is absent (e.g. before the first `_tag_dispatcher_session` call
   after a server restart, or if LOBSTER_WORKSPACE points to a non-standard
   location).
   Match → dispatcher.  Mismatch → subagent.  File absent → default.

3. **Default**: If neither state file is readable or present, return False
   (treat as subagent = safe/conservative).

The transcript-scanning fallback that existed in previous versions has been
removed.  It was fragile (CC JSONL format changes, same-week compaction bug
tracked in PR #1076) and is now superseded by the MCP state file, which is
always authoritative when the MCP server is running.

## Writing the marker file

Call `write_dispatcher_session_id(session_id)` at dispatcher startup.
Typically invoked from the `write-dispatcher-session-id.py` SessionStart hook.
The MCP server also calls `_write_dispatcher_state_file()` internally; hooks
do not need to call that path directly.
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
    """Return the MCP server state file path, resolved at call time.

    Reads LOBSTER_WORKSPACE on every call so tests can override the env var
    without having to patch a module-level constant.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-session-id"


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

    Checks the MCP state file first (written by the running MCP server and
    cleared on restart), then falls back to the hook marker file.  Returns
    False when no state file is present or readable.

    Fail-open behavior: if a file exists but cannot be read due to an OS
    error, returns True (same conservative fail-open as before) so the
    dispatcher is never incorrectly blocked by a transient I/O error.
    """
    session_id = get_session_id(hook_input)

    # --- Primary: MCP state file (re-resolved each call to respect env overrides) ---
    primary_result = _check_state_file(_get_mcp_session_state_file(), session_id)
    if primary_result is not None:
        return primary_result

    # --- Secondary: hook marker file ---
    secondary_result = _check_state_file(DISPATCHER_SESSION_FILE, session_id)
    if secondary_result is not None:
        return secondary_result

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
