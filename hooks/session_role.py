#!/usr/bin/env python3
"""
Shared utility: dispatcher vs subagent session discrimination.

Provides a single `is_dispatcher(hook_input)` predicate that all hooks can
import to determine whether the current Claude Code session is the Lobster
dispatcher or a background subagent.

## Detection strategy (layered)

1. **Claude UUID state file (primary)**: When the dispatcher calls
   `session_start(agent_type='dispatcher', claude_session_id=<uuid>)`, the MCP
   server writes the Claude session UUID to
   `$LOBSTER_WORKSPACE/data/dispatcher-claude-session-id`.  Claude Code
   SessionStart hooks receive this same UUID in `hook_input["session_id"]`, so
   a direct string comparison works correctly.  The file is cleared when the MCP
   server restarts.
   Match → dispatcher.  Mismatch → subagent.  File absent → try next fallback.

2. **MCP HTTP session state file (secondary — currently skipped)**: The MCP
   server writes the HTTP transport session ID (32-char hex) to
   `$LOBSTER_WORKSPACE/data/dispatcher-session-id` whenever
   `_tag_dispatcher_session()` is called.  This ID is a DIFFERENT format from
   the Claude UUID in `hook_input["session_id"]` — they never match.  When the
   secondary file is present, `_check_state_file()` always returns False
   (mismatch) for both dispatcher and subagents, making it useless as a
   discriminator.  Worse, the False short-circuits execution before the
   tertiary check, which uses the correct UUID format.  The secondary check is
   intentionally skipped; the file is left on disk for diagnostic purposes only.

3. **Hook marker file (tertiary)**: At dispatcher startup the SessionStart hook
   (`write-dispatcher-session-id.py`) writes the Claude session ID to
   `~/messages/config/dispatcher-session-id`.  Used as fallback when the MCP
   state files are absent (e.g. before the first `session_start` call after a
   server restart, or if LOBSTER_WORKSPACE points to a non-standard location).
   Match → dispatcher.  Mismatch → subagent.  File absent → default.

4. **Default**: If no state file is readable or present, return False
   (treat as subagent = safe/conservative).

The transcript-scanning fallback that existed in previous versions has been
removed.  It was fragile (CC JSONL format changes, same-week compaction bug
tracked in PR #1076) and is now superseded by the MCP state file, which is
always authoritative when the MCP server is running.

## Writing the marker file

Call `write_dispatcher_session_id(session_id)` at dispatcher startup.
Typically invoked from the `write-dispatcher-session-id.py` SessionStart hook.
The MCP server also calls `_write_dispatcher_claude_session_file()` internally
when session_start(agent_type='dispatcher', claude_session_id=...) is called.
"""

import os
from pathlib import Path

# Tertiary: hook marker file (written by write-dispatcher-session-id.py SessionStart hook)
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
    """Return the MCP HTTP session state file path, resolved at call time.

    Contains the HTTP transport session ID (32-char hex), NOT the Claude UUID.
    Reads LOBSTER_WORKSPACE on every call so tests can override the env var
    without having to patch a module-level constant.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-session-id"


def _get_mcp_claude_session_file() -> Path:
    """Return the MCP Claude UUID state file path, resolved at call time.

    Contains the Claude session UUID (36-char UUID4) written by the MCP server
    when session_start(agent_type='dispatcher', claude_session_id=...) is called.
    This is the same UUID that SessionStart hooks receive in hook_input["session_id"].
    Reads LOBSTER_WORKSPACE on every call so tests can override the env var.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "dispatcher-claude-session-id"


# Module-level aliases for test patching convenience — tests that set LOBSTER_WORKSPACE
# can also patch these directly.  Updated lazily if needed.
MCP_SESSION_STATE_FILE = _get_mcp_session_state_file()
MCP_CLAUDE_SESSION_FILE = _get_mcp_claude_session_file()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_session_id(hook_input: dict) -> str | None:
    """Return the session_id from hook JSON input, or None if absent."""
    return hook_input.get("session_id") or None


def is_dispatcher(hook_input: dict) -> bool:
    """Return True if the current session is the Lobster dispatcher.

    Checks the Claude UUID state file first (written when the dispatcher calls
    session_start with agent_type='dispatcher' and claude_session_id=<uuid>),
    then falls back to the MCP HTTP session state file, then the hook marker file.
    Returns False when no state file is present or readable.

    Fail-open behavior: if a file exists but cannot be read due to an OS
    error, returns True (same conservative fail-open as before) so the
    dispatcher is never incorrectly blocked by a transient I/O error.

    ## Why two active checks (primary + tertiary)?

    The Claude UUID (hook_input["session_id"]) and the MCP HTTP session ID are
    different formats — they never match directly.  The primary check uses the
    Claude UUID file (dispatcher-claude-session-id), which IS the correct
    comparison for SessionStart hooks.  The secondary check (dispatcher-session-id)
    stores the HTTP transport session ID and is intentionally skipped because it
    always mismatches both dispatcher and subagents, causing false-negative
    short-circuits before the tertiary check.  The tertiary check (hook marker
    file ~/messages/config/dispatcher-session-id) stores the Claude UUID and
    serves as the fallback for the race window before session_start is called.
    """
    session_id = get_session_id(hook_input)

    # --- Primary: Claude UUID state file ---
    # Written by MCP server when session_start(agent_type='dispatcher',
    # claude_session_id=<uuid>) is called.  Same UUID format as hook_input.
    primary_result = _check_state_file(_get_mcp_claude_session_file(), session_id)
    if primary_result is not None:
        return primary_result

    # --- Secondary: MCP HTTP session state file (SKIPPED intentionally) ---
    # _get_mcp_session_state_file() stores the MCP HTTP transport session ID
    # (32-char hex, e.g. '43e178fa975741eb9f6c1cb9f328d52b'), but
    # hook_input["session_id"] is a Claude UUID (36-char UUID4, e.g.
    # '756633a5-4802-4327-ab98-684243d5fc2a').  These formats never match, so
    # _check_state_file() always returns False when the secondary file is
    # present — for BOTH the dispatcher and subagents.  Treating that False as
    # a conclusive "subagent" result blocks the tertiary check, which uses
    # ~/messages/config/dispatcher-session-id (a file that DOES store the
    # correct Claude UUID and WOULD return True for the dispatcher).
    # The secondary check is not a reliable discriminator; skip it entirely and
    # fall through to the tertiary check.

    # --- Tertiary: hook marker file ---
    tertiary_result = _check_state_file(DISPATCHER_SESSION_FILE, session_id)
    if tertiary_result is not None:
        return tertiary_result

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


def write_dispatcher_claude_session_id(session_id: str) -> None:
    """Write session_id to the primary MCP Claude UUID state file.

    This is the primary dispatcher detection file read by is_dispatcher().
    It stores the Claude session UUID (36-char UUID4) that matches
    hook_input["session_id"] in SessionStart hooks.

    Called by on-compact.py when a dispatcher compaction is confirmed, so that
    inject-bootup-context.py can detect the new post-compact session as the
    dispatcher before session_start() has been called (which would normally
    write this file via the MCP server).

    Atomic write via a .tmp rename.  Silent on any failure — must never crash
    the caller.
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
