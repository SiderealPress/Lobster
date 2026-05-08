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
    try:
        if not STARTUP_FLAG_FILE.exists():
            return False
        raw = STARTUP_FLAG_FILE.read_text().strip()
        if not raw:
            return False
        pid = int(raw)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # PID exists, can't signal — treat as alive
        return True
    except (OSError, ValueError):
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


def _read_dispatcher_session_id() -> "str | None":
    """Return the stored dispatcher session ID from the hook marker file, or None."""
    result = _read_session_id_from_file(DISPATCHER_SESSION_FILE)
    if isinstance(result, OSError):
        return None
    return result


# ---------------------------------------------------------------------------
# Hook-process-context API — for PreToolUse hooks (issue #1113)
# MUST NOT CHANGE: PreToolUse hooks depend on is_dispatcher_session()
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
    """
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        return False

    tmux_pids = _get_tmux_pane_pids()
    if not tmux_pids:
        return True

    claude_ancestor_count = 0
    pid = os.getpid()
    for _ in range(15):  # Safety limit
        ppid = _get_ppid(pid)
        if ppid is None:
            break
        if str(ppid) in tmux_pids:
            return claude_ancestor_count <= 1
        parent_name = _get_proc_name(ppid)
        if _is_claude_process(parent_name):
            claude_ancestor_count += 1
        pid = ppid

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
    which checks the startup flag file.

    NOTE: Intentionally unchanged by issue #1908 — PreToolUse hooks depend on this
    function's process-tree + state-file logic during active processing (after the
    startup flag has been consumed by inject-bootup-context.py).
    """
    # Fast path: agent_id is present only in subagent PreToolUse payloads.
    # The dispatcher never has agent_id.  Exit immediately without any file I/O.
    if hook_input.get("agent_id"):
        return False

    # State-file check: covers MCP Claude UUID file + hook marker file.
    #
    # Only short-circuit on True (confirmed dispatcher match).  A False result
    # means "this session ID does not match the stored dispatcher ID", which is
    # ambiguous on restart: the stored ID may be stale (previous session) rather
    # than a live dispatcher entry.  Falling through to the process-tree check
    # resolves the ambiguity.  Only a confirmed match (True) is authoritative
    # enough to skip the remaining checks.
    session_id = get_session_id(hook_input)
    primary_result = _check_state_file(_get_mcp_claude_session_file(), session_id)
    if primary_result is True:
        return True

    # Tertiary: hook marker file.
    tertiary_result = _check_state_file(DISPATCHER_SESSION_FILE, session_id)
    if tertiary_result is True:
        return True

    # No state file confirmed dispatcher — fall back to process-tree.
    # This handles both the "stale state file after restart" case and the
    # "no state file yet" (early boot) case.
    return _is_dispatcher_by_process_tree()
