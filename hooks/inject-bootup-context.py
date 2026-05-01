#!/usr/bin/env python3
"""
SessionStart / compact hook: inject dispatcher or subagent bootup files.

Fires on every SessionStart event (and on compact sessions via the "compact"
matcher entry in settings.json). Reads the appropriate system bootup file
and user-specific bootup files, printing their contents to stdout.

Claude Code SessionStart hooks inject stdout as a system message at the start
of the session, making this content available before the first turn.

File injection order:
1. sys.dispatcher.bootup.md OR sys.subagent.bootup.md (based on role)
2. ~/lobster-user-config/agents/user.base.bootup.md (if exists)
3. ~/lobster-user-config/agents/user.dispatcher.bootup.md (dispatcher only, if exists)
   OR ~/lobster-user-config/agents/user.subagent.bootup.md (subagent only, if exists)

Hook ordering note:
This hook calls session_role.write_dispatcher_session_id() when it detects a
dispatcher session, making it self-sufficient regardless of whether
write-dispatcher-session-id.py ran first. The write is idempotent: if
write-dispatcher-session-id.py already ran (position 0 in settings.json),
the file already has the correct ID and this is a no-op; if this hook runs
first for any reason, the ID is written here and downstream hooks benefit.

Post-compaction sentinel fallback (Option 3, issue #1375):
After context compaction, CC assigns a NEW session_id to the post-compact
session.  on-compact.py writes the new UUID to both state files (Option 1),
but as defense-in-depth this hook also checks for the compact-pending sentinel
file.  If the sentinel exists AND LOBSTER_MAIN_SESSION=1, the session is
treated as a post-compact dispatcher session regardless of the ID-match result.
This bypasses the chicken-and-egg timing problem entirely for the post-compact
case and ensures dispatcher bootup is always injected when it should be.

Fresh-start fallback (issue #1868):
On a genuine fresh restart (new process, MCP server restarted), the primary
state file (dispatcher-claude-session-id) is cleared by the MCP server on
startup and is absent until the dispatcher calls session_start().
write-dispatcher-session-id.py may also skip updating the tertiary marker file
if the previous session's JSONL has a recent mtime, leaving a stale UUID.
Both state files then fail to identify the new dispatcher, causing is_dispatcher()
to return False.  The sentinel fallback doesn't help (no compaction occurred).
Fix: if the primary file is absent AND LOBSTER_MAIN_SESSION=1, treat the session
as the dispatcher.  This is safe because subagents are only spawned after the
dispatcher calls session_start() (which writes the primary file), and compaction
events write the primary file proactively via on-compact.py.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from the hooks directory (session_role).
sys.path.insert(0, str(Path(__file__).parent))

import session_role  # noqa: E402 — path insert must precede this

CLAUDE_DIR = Path(os.path.expanduser("~/lobster/.claude"))
USER_CONFIG_DIR = Path(os.path.expanduser("~/lobster-user-config/agents"))

DISPATCHER_BOOTUP = CLAUDE_DIR / "sys.dispatcher.bootup.md"
SUBAGENT_BOOTUP = CLAUDE_DIR / "sys.subagent.bootup.md"

USER_BASE_BOOTUP = USER_CONFIG_DIR / "user.base.bootup.md"
USER_DISPATCHER_BOOTUP = USER_CONFIG_DIR / "user.dispatcher.bootup.md"
USER_SUBAGENT_BOOTUP = USER_CONFIG_DIR / "user.subagent.bootup.md"

HOOK_NAME = "inject-bootup-context"

# Append-only log of context injections — one line per hook run.
# Populated at import time so tests can override by setting mod.CONTEXT_INJECTION_LOG.
_LOBSTER_WORKSPACE = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
)
CONTEXT_INJECTION_LOG = _LOBSTER_WORKSPACE / "logs" / "context-injection.log"

# Compact-pending sentinel written by on-compact.py for dispatcher compactions only.
# Used as the Option 3 fallback: sentinel present + LOBSTER_MAIN_SESSION=1 → dispatcher.
COMPACT_PENDING_SENTINEL = Path(os.path.expanduser("~/messages/config/compact-pending"))


def _is_post_compact_dispatcher() -> bool:
    """Return True if this looks like a post-compaction dispatcher session.

    This is the Option 3 sentinel-based fallback for issue #1375.  It
    bypasses the session-ID matching checks entirely for the post-compact case.

    Conditions (both required):
    - LOBSTER_MAIN_SESSION=1 is set in the environment (marks sessions started
      by claude-persistent.sh as the dispatcher or its subagents).
    - The compact-pending sentinel file exists.  on-compact.py writes this
      file only for dispatcher compactions, so its presence is a reliable
      dispatcher-scoped signal.

    Why this is safe for subagents:
    Subagent sessions that compact (rare) also have LOBSTER_MAIN_SESSION=1
    and the sentinel will be present from the dispatcher's last compaction.
    However, the sentinel is removed when the dispatcher calls
    wait_for_messages() via post-compact-gate.py, so by the time a subagent
    is spawned after a compaction the sentinel is normally gone.  In the
    narrow window where a subagent starts while the sentinel is still present,
    injecting dispatcher bootup is low-cost: the subagent will receive extra
    context that does not conflict with its own bootup.  This is the same
    acceptable trade-off documented in _is_dispatcher_compact().
    """
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        return False
    return COMPACT_PENDING_SENTINEL.exists()


def _is_fresh_start_dispatcher() -> bool:
    """Return True if this looks like a fresh-restart dispatcher session (issue #1868).

    On a genuine fresh restart, the MCP server clears the primary state file
    (dispatcher-claude-session-id) on startup.  That file is absent until the
    dispatcher calls session_start().  write-dispatcher-session-id.py may also
    skip updating the tertiary marker file if the previous session's JSONL has
    a recent mtime, leaving a stale UUID in the tertiary file.

    Consequence: is_dispatcher() finds no matching state file and returns False.
    The compact-pending sentinel fallback (_is_post_compact_dispatcher) is also
    inactive because no compaction occurred.  inject-bootup-context.py then
    falls back to injecting subagent bootup — wrong for the dispatcher.

    Fix: absent primary file + LOBSTER_MAIN_SESSION=1 → treat as dispatcher.

    Why this is safe:
    - Subagents are spawned only after the dispatcher calls session_start(),
      which writes the primary file.  By the time any subagent SessionStart
      fires, the primary file is present.
    - Compaction events: on-compact.py proactively writes the primary file
      with the new UUID before the post-compact SessionStart fires.  The
      primary file is therefore present for compaction events, not absent.

    Returns False on any OSError (cannot stat the primary file) — safe default.
    """
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        return False
    try:
        return not session_role._get_mcp_claude_session_file().exists()
    except OSError:
        return False


def _read_file_safe(path: Path, label: str) -> str | None:
    """Return file contents or None on any error or empty file, logging to stderr."""
    if not path.exists():
        print(
            f"[{HOOK_NAME}] WARNING: {path} not found; skipping {label} injection.",
            file=sys.stderr,
        )
        return None
    try:
        content = path.read_text()
        return content if content.strip() else None
    except OSError as exc:
        print(
            f"[{HOOK_NAME}] WARNING: could not read {path}: {exc}",
            file=sys.stderr,
        )
        return None


def _inject_if_exists(path: Path, label: str) -> bool:
    """Read and print file contents if the file exists and is non-empty.

    Returns True if the file was successfully injected, False otherwise.
    Silent skip when the file is absent.
    """
    if not path.exists():
        return False
    try:
        content = path.read_text()
        if content.strip():
            print(content)
            return True
        return False
    except OSError as exc:
        print(
            f"[{HOOK_NAME}] WARNING: could not read {path} ({label}): {exc}",
            file=sys.stderr,
        )
        return False


def _append_injection_log(
    session_id: str,
    role: str,
    injected_files: list[str],
) -> None:
    """Append one line to the context injection log.

    Format:
      <ISO UTC timestamp> | session=<id> | role=<role> | injected=[file1, file2, ...]

    Creates the log file and any missing parent directories if needed.
    Errors are swallowed — logging must never break the hook.
    """
    try:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        files_repr = "[" + ", ".join(injected_files) + "]"
        line = f"{timestamp} | session={session_id} | role={role} | injected={files_repr}\n"
        log_path = CONTEXT_INJECTION_LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        pass  # logging must not break injection


def main() -> None:
    # Read hook input from stdin to detect dispatcher vs subagent role.
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    session_id = hook_input.get("session_id", "unknown")

    is_dispatcher = session_role.is_dispatcher(hook_input)

    # Option 3 fallback (issue #1375): if the compact-pending sentinel exists
    # and LOBSTER_MAIN_SESSION=1, treat this as a post-compact dispatcher session
    # regardless of what is_dispatcher() returned.  This covers the case where
    # Option 1 (writing the new UUID in on-compact.py) hasn't propagated yet or
    # fails silently, and provides defense-in-depth for the post-compact window.
    if not is_dispatcher and _is_post_compact_dispatcher():
        print(
            f"[{HOOK_NAME}] sentinel fallback: compact-pending exists + "
            "LOBSTER_MAIN_SESSION=1; treating as post-compact dispatcher",
            file=sys.stderr,
        )
        is_dispatcher = True

    # Fresh-start fallback (issue #1868): if the primary state file is absent
    # and LOBSTER_MAIN_SESSION=1, the MCP server has started but session_start()
    # has not yet been called — this is the pre-session_start window that only
    # exists during a genuine fresh restart.  Subagents cannot trigger this
    # because they start only after the dispatcher has called session_start()
    # (which writes the primary file).  Compaction events are also safe: on-compact.py
    # writes the primary file proactively, so it is present for post-compact starts.
    if not is_dispatcher and _is_fresh_start_dispatcher():
        print(
            f"[{HOOK_NAME}] fresh-start fallback: primary state file absent + "
            "LOBSTER_MAIN_SESSION=1; treating as fresh-restart dispatcher",
            file=sys.stderr,
        )
        is_dispatcher = True

    # If this is the dispatcher session, write the session ID to the marker file.
    # This makes the hook self-sufficient regardless of whether
    # write-dispatcher-session-id.py ran first. The write is idempotent.
    if is_dispatcher:
        sid = session_role.get_session_id(hook_input)
        if sid:
            session_role.write_dispatcher_session_id(sid)

    role = "dispatcher" if is_dispatcher else "subagent"
    injected: list[str] = []

    # 1. Inject system bootup file based on role.
    if is_dispatcher:
        content = _read_file_safe(DISPATCHER_BOOTUP, "sys.dispatcher.bootup.md")
        system_file = DISPATCHER_BOOTUP
    else:
        content = _read_file_safe(SUBAGENT_BOOTUP, "sys.subagent.bootup.md")
        system_file = SUBAGENT_BOOTUP

    if content is None:
        _append_injection_log(session_id, role, injected)
        sys.exit(0)

    print(content)
    injected.append(system_file.name)

    # 2. Inject user base bootup (both roles).
    if _inject_if_exists(USER_BASE_BOOTUP, "user.base.bootup.md"):
        injected.append(USER_BASE_BOOTUP.name)

    # 3. Inject role-specific user bootup.
    if is_dispatcher:
        if _inject_if_exists(USER_DISPATCHER_BOOTUP, "user.dispatcher.bootup.md"):
            injected.append(USER_DISPATCHER_BOOTUP.name)
    else:
        if _inject_if_exists(USER_SUBAGENT_BOOTUP, "user.subagent.bootup.md"):
            injected.append(USER_SUBAGENT_BOOTUP.name)

    _append_injection_log(session_id, role, injected)
    sys.exit(0)


if __name__ == "__main__":
    main()
