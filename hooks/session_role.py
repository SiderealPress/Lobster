#!/usr/bin/env python3
"""
Shared utility: dispatcher vs subagent session discrimination.

Provides two public predicates for hooks to determine whether the current
Claude Code session is the Lobster dispatcher or a background subagent.
The two differ in the context they target and the fallback they apply:

## Session-context API — `is_dispatcher(hook_input)`

For use in **SessionStart / SubagentStop / Stop hooks** where CC provides a
stable `hook_input["session_id"]` (Claude UUID) that matches the value the
MCP server records when `session_start(agent_type='dispatcher')` is called.

Strategy: MCP Claude UUID state file → hook marker file → default False.
No process-tree walk (not needed: the state file is always authoritative
in session-context hooks).

## Hook-process-context API — `is_dispatcher_session(hook_input)`

For use in **PreToolUse hooks** where an `agent_id` field is injected by
CC for subagent sessions (absent for the dispatcher), and where the
process-tree can supplement the state-file check when the system is very
early in boot (before `session_start` has been called).

Strategy: agent_id fast path → MCP state files → process-tree walk →
env-var-only fallback.

Use `is_dispatcher_session` in PreToolUse hooks that guard dispatcher-only
behaviour (e.g. post-compact-gate).  Use `is_dispatcher` for SessionStart /
SubagentStop / Stop hooks.

## Original single-function design

The split was introduced to fix a class of false-positives described in
issue #1151 (MCP HTTP session ID namespace mismatch).  See that issue and
PR #1102 for history.  This module is the canonical location for both APIs
as of the cleanup in issue #1113.

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
import subprocess
from pathlib import Path

# Tmux session name for the process-tree fallback used by is_dispatcher_session().
_LOBSTER_TMUX_SESSION = os.environ.get("LOBSTER_TMUX_SESSION", "lobster")

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


# ---------------------------------------------------------------------------
# Hook-process-context API — for PreToolUse hooks (issue #1113)
# ---------------------------------------------------------------------------

def _get_tmux_pane_pids() -> "set[str]":
    """Return the set of PIDs for all panes in the lobster tmux session."""
    try:
        result = subprocess.run(
            [
                "tmux", "-L", _LOBSTER_TMUX_SESSION,
                "list-panes", "-t", _LOBSTER_TMUX_SESSION,
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


def _get_ppid(pid: int) -> "int | None":
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

    **For use in PreToolUse hooks** (hook-process context).  Adds a process-tree
    walk fallback on top of the state-file checks in `is_dispatcher()`, for the
    early-boot window before `session_start` has been called.

    Detection strategy (in order):
      0. agent_id fast path: CC injects agent_id only into subagent PreToolUse
         payloads.  If present → subagent (return False immediately, no I/O).
         See issue #1152.
      1. MCP state files + hook marker file via `is_dispatcher()`.  Returns a
         definitive answer when either file is present and readable.
      2. Process-tree walk: count consecutive claude ancestors before a tmux pane
         PID.  ≤1 ancestor → dispatcher; ≥2 → subagent.
      3. Env-var-only fallback: LOBSTER_MAIN_SESSION=1 without tmux confirmation.

    For SessionStart / SubagentStop / Stop hooks, use the simpler `is_dispatcher()`
    which omits the process-tree walk.  The process-tree walk is only needed in
    PreToolUse where the state files may not yet have been written.

    Note: This function was formerly private to `post-compact-gate.py`.  It was
    promoted to session_role.py in issue #1113 so other hooks can reuse it.
    """
    # Fast path: agent_id is present only in subagent PreToolUse payloads.
    # The dispatcher never has agent_id.  Exit immediately without any file I/O.
    # NOTE: agent_id is NOT available in SessionStart hooks; this check is only
    # valid in PreToolUse context.  See issue #1152.
    if hook_input.get("agent_id"):
        return False

    # State-file check: covers MCP Claude UUID file + hook marker file.
    # is_dispatcher() returns False when no file matches — that means "no signal",
    # but we need to distinguish "definitely subagent" from "no signal".
    # Probe the primary (Claude UUID) file directly first.
    session_id = get_session_id(hook_input)
    primary_result = _check_state_file(_get_mcp_claude_session_file(), session_id)
    if primary_result is not None:
        return primary_result

    # Tertiary: hook marker file.
    tertiary_result = _check_state_file(DISPATCHER_SESSION_FILE, session_id)
    if tertiary_result is not None:
        return tertiary_result

    # No state file signal available — fall back to process-tree.
    return _is_dispatcher_by_process_tree()
