#!/usr/bin/env python3
"""
SessionStart hook: inject dispatcher or subagent bootup files.

Fires on every SessionStart event. Reads the appropriate system bootup file
and user-specific bootup files, printing their contents to stdout.

Claude Code SessionStart hooks inject stdout as a system message at the start
of the session, making this content available before the first turn.

File injection order:
1. sys.dispatcher.bootup.md OR sys.subagent.bootup.md (based on role)
2. ~/lobster-user-config/agents/user.base.bootup.md (if exists)
3. ~/lobster-user-config/agents/user.dispatcher.bootup.md (dispatcher only, if exists)
   OR ~/lobster-user-config/agents/user.subagent.bootup.md (subagent only, if exists)

Dispatcher detection (simplified, issue #1908):
The launcher (claude-persistent.sh) writes the subshell PID to
~/lobster-workspace/data/dispatcher-startup-flag immediately before exec-ing
claude. This hook reads that flag:
  - Flag present AND PID alive (kill -0) → dispatcher session. Delete the flag.
  - Flag absent OR PID dead → subagent session.

This eliminates the chicken-and-egg problem of UUID-based detection: the flag
is written *before* CC starts, not after session_start() is called. Stale
flags (dead PID) are safe because the check is purely process-existence-based.
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

# Startup flag written by claude-persistent.sh before exec-ing claude.
# Contains the launcher subshell PID. Deleted after the dispatcher is detected.
STARTUP_FLAG_FILE = _LOBSTER_WORKSPACE / "data" / "dispatcher-startup-flag"


def _is_startup_flag_dispatcher() -> bool:
    """Return True if a live startup flag marks this as the dispatcher session.

    The launcher writes its PID to STARTUP_FLAG_FILE before exec-ing claude.
    If the file exists and the PID is still alive (kill -0), this is the
    dispatcher. Stale flags (dead PID) are treated as absent — safe fallback.

    Returns False on any error (OSError, ValueError, etc.) — conservative default.
    """
    try:
        if not STARTUP_FLAG_FILE.exists():
            return False
        raw = STARTUP_FLAG_FILE.read_text().strip()
        if not raw:
            return False
        pid = int(raw)
        # os.kill(pid, 0) checks process existence without sending a signal.
        # Raises ProcessLookupError if the PID doesn't exist.
        # Raises PermissionError if the PID exists but we can't signal it —
        # that means the process IS alive (just belongs to another user), treat alive.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # PID is dead — stale flag.
            return False
        except PermissionError:
            # PID exists, we just can't signal it — alive.
            pass
        return True
    except (OSError, ValueError):
        return False


def _consume_startup_flag() -> None:
    """Delete the startup flag file after the dispatcher is detected.

    Silent on any error — must never crash the hook.
    """
    try:
        STARTUP_FLAG_FILE.unlink(missing_ok=True)
    except OSError:
        pass


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
    # Read hook input from stdin (provides session_id for logging).
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    session_id = hook_input.get("session_id", "unknown")

    # Simplified dispatcher detection (issue #1908):
    # Check the launcher-written startup flag file. Live PID = dispatcher.
    is_dispatcher = _is_startup_flag_dispatcher()

    if is_dispatcher:
        # Consume the flag so subsequent sessions (subagents) do not see it.
        _consume_startup_flag()
        print(
            f"[{HOOK_NAME}] startup-flag detected live PID — injecting dispatcher bootup",
            file=sys.stderr,
        )

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
