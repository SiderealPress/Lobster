"""
PID liveness primitive — Phase 1: PID ground truth (issue #2148).

Scoped from shadowlobster#65's "Deepened design brief". This module is the
single shared source of truth for "is this OS process actually alive?",
callable from both Python (session_store.py, agent-monitor.py) and shell
(health-check-v3.sh, via `python3 -m agents.pid_liveness <pid>` or similar)
so the three independent liveness classifiers stop disagreeing.

Two responsibilities, both pure/read-only (no writes, no side effects):

  1. is_pid_alive(pid) — ground-truth liveness check via os.kill(pid, 0).
     This is the PRIMARY signal wherever a real PID was captured. Existing
     output_file-mtime / stop_reason heuristics remain the fallback for rows
     where no real process boundary exists (pre-migration rows, or Agent-tool
     in-process subagents that have no PID of their own — see
     find_dispatcher_ancestor_pid below).

  2. find_dispatcher_ancestor_pid() — walks the real /proc process tree to
     find the nearest ancestor process whose executable is literally named
     "claude" (the dispatcher's own OS process). This is needed because two
     of the three call sites that must capture a "dispatcher PID" do NOT run
     inside the dispatcher's own `claude` process:

       - hooks/auto-register-agent.py is exec'd fresh, per Agent-tool call,
         as a direct child of the `claude` process (one hop up).
       - src/mcp/inbox_server.py's handle_session_start() (dispatcher
         self-registration) runs inside the `lobster-inbox` MCP server child
         process, which Claude Code spawns via stdio per `.mcp.json`
         (`"type": "stdio"`) — itself a direct child of `claude`, but NOT the
         same PID as `claude` itself. A plain os.getpid() there would record
         the MCP server child's PID, not the dispatcher's.

     Verified live (read-only `pstree -p` inspection, no live state touched)
     against the actual running host on 2026-08-03: Agent/Task-tool-spawned
     subagents show up as *threads* of the single dispatcher `claude` OS
     process (pstree's `{claude}(<tid>)` notation), not as separate
     processes — confirming they have no OS PID of their own to capture.
     This is exactly the "in-process sub-conversation" case the issue's
     architectural caveat asked to be verified rather than assumed.

Known limitation — PID reuse race (see docs/engineering-lessons-learned.md,
"PID Reuse Race"): is_pid_alive() can return a false positive if the PID was
reused by an unrelated process after the original one exited. This module
does not attempt to close that race (e.g. via /proc/<pid>/stat start-time
comparison) — it is documented here as a known limitation per the issue's
explicit instruction not to over-engineer a fix for it in this slice.
"""

from __future__ import annotations

import os


def is_pid_alive(pid: int | None) -> bool:
    """Return True if `pid` refers to a live OS process, False otherwise.

    Uses os.kill(pid, 0) — sends no actual signal, just probes existence.
    Treats PermissionError (process exists but we lack privileges to signal
    it — e.g. owned by a different user) as alive, since the process clearly
    exists. Treats any other failure (ProcessLookupError, invalid pid, etc.)
    as not alive.

    Args:
        pid: The OS process ID to check. None, 0, and negative values are
             treated as "no real PID" and return False without raising.
    """
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False

    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists; we just can't signal it.
    except OSError:
        return False
    return True


def _read_ppid(pid: int) -> int | None:
    """Return the parent PID of `pid` by reading /proc/<pid>/stat, or None.

    The comm field may itself contain parentheses/spaces, so we split on the
    last ')' to safely isolate the fields that follow it (state, ppid, ...).
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            content = f.read()
        after_comm = content.rsplit(")", 1)[-1]
        fields = after_comm.split()
        ppid = int(fields[1])  # fields[0] is state, fields[1] is ppid
        return ppid if ppid > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _read_comm(pid: int) -> str:
    """Return the process name (comm) for `pid`, or '' if unreadable."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def find_dispatcher_ancestor_pid(
    start_pid: int | None = None,
    max_depth: int = 20,
) -> int | None:
    """Walk up the process tree from `start_pid` to find the dispatcher's PID.

    Returns the PID of the nearest ancestor process whose kernel-reported
    comm is exactly "claude" (the dispatcher's real OS process — the `claude`
    CLI binary running inside the lobster tmux session), or None if no such
    ancestor is found within `max_depth` hops, or if the process tree cannot
    be read (e.g. running on a non-Linux system without /proc).

    Args:
        start_pid: PID to start walking from. Defaults to os.getpid() (the
                   calling process itself).
        max_depth: Maximum number of parent hops to walk before giving up.
                   0 means "do not walk at all" (returns None immediately).

    This mirrors the process-tree walk already established and validated in
    hooks/session_role.py's _is_dispatcher_by_process_tree() / _get_ppid() /
    _is_claude_process() for is_dispatcher_session() — reused here as the
    shared, DB-facing counterpart so PID capture at session_start() time uses
    the same ground truth the hook-side dispatcher detection already relies
    on. Unlike that helper, this one does not cross-check tmux pane PIDs —
    it is only used to find a PID to record, not to gate dispatcher-only
    tool access, so a plain ancestor walk is sufficient and simpler to test.
    """
    pid = start_pid if start_pid is not None else os.getpid()

    current = pid
    for _ in range(max_depth):
        ppid = _read_ppid(current)
        if ppid is None or ppid <= 1:
            return None
        comm = _read_comm(ppid)
        if comm == "claude":
            return ppid
        current = ppid

    return None
