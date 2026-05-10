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
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from the hooks directory (session_role).
sys.path.insert(0, str(Path(__file__).parent))

import session_role  # noqa: E402 — path insert must precede this

CLAUDE_DIR = Path(os.path.expanduser("~/lobster/.claude"))
USER_CONFIG_DIR = Path(os.path.expanduser("~/lobster-user-config/agents"))

DISPATCHER_BOOTUP = CLAUDE_DIR / "sys.dispatcher.bootup.md"
SUBAGENT_BOOTUP = CLAUDE_DIR / "sys.subagent.bootup.md"

# Minimal bootup stub injected on compaction starts (issue #1954).
# Saves ~25-35k tokens by skipping the full dispatcher bootup when the
# compact-catchup subagent is about to restore context anyway.
# Falls back to DISPATCHER_BOOTUP if this file is absent.
COMPACT_DISPATCHER_BOOTUP = CLAUDE_DIR / "sys.compact-dispatcher.bootup.md"

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

# Single source of truth for startup cause classification (issue #1972).
# on-compact.py writes {"cause": "compaction", "ts": "<iso_utc>"} before exiting.
# This hook reads + resets it on every startup. Override via env var for tests.
STARTUP_CAUSE_FILE = Path(
    os.environ.get(
        "LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE",
        str(_LOBSTER_WORKSPACE / "data" / "last-startup-cause.json"),
    )
)

# Maximum age in seconds for a "compaction" cause entry to be trusted.
# Beyond this window we fall back to "restart" to avoid misclassifying a
# compaction that was followed by an unrelated external restart.
COMPACTION_CAUSE_WINDOW_SECONDS = 300  # 5 minutes


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


def read_and_reset_startup_cause() -> str:
    """Read last-startup-cause.json and return the startup cause as a string.

    Returns "compaction" if:
      - The file exists AND cause == "compaction" AND ts is within the last
        COMPACTION_CAUSE_WINDOW_SECONDS seconds.

    Returns "restart" for all other cases:
      - File absent
      - File corrupt / unparseable JSON
      - cause == "restart"
      - cause == "compaction" but ts is stale (>= COMPACTION_CAUSE_WINDOW_SECONDS)

    After reading, always overwrites the file with {"cause": "restart", "ts": "<now>"}
    so the next startup defaults to "restart" unless on-compact.py fires first.
    The overwrite is silent on failure — the return value is still correct.

    This function is the ONLY place the dispatcher reads startup cause.  Do not
    read last-startup-cause.json anywhere else.
    """
    cause: str = "restart"

    try:
        if STARTUP_CAUSE_FILE.exists():
            raw = STARTUP_CAUSE_FILE.read_text()
            data = json.loads(raw)
            file_cause = data.get("cause", "restart")
            if file_cause == "compaction":
                ts_str = data.get("ts", "")
                try:
                    ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age_seconds = (datetime.now(timezone.utc) - ts_dt).total_seconds()
                    if age_seconds < COMPACTION_CAUSE_WINDOW_SECONDS:
                        cause = "compaction"
                except (ValueError, AttributeError):
                    pass  # unparseable ts → keep cause = "restart"
    except Exception:  # noqa: BLE001
        pass  # any read failure → keep cause = "restart"

    # Always reset to "restart" so subsequent startups default correctly.
    _reset_startup_cause_to_restart()

    return cause


def _reset_startup_cause_to_restart() -> None:
    """Overwrite last-startup-cause.json with cause=restart and the current timestamp.

    Called unconditionally after read_and_reset_startup_cause() reads the file,
    ensuring that the next startup defaults to "restart" unless on-compact.py
    fires first and writes cause=compaction.

    Silent on any failure — must never crash the hook.
    """
    try:
        STARTUP_CAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cause": "restart",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp_path = STARTUP_CAUSE_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        tmp_path.replace(STARTUP_CAUSE_FILE)  # atomic on Linux (same filesystem)
    except Exception:  # noqa: BLE001
        pass


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

    # Read and reset last-startup-cause.json (issue #1972).
    # Always runs (both dispatcher and subagent) so the file is reset on every
    # startup and never accumulates a stale "compaction" entry.
    # Only the dispatcher acts on the cause — subagents ignore it.
    startup_cause = read_and_reset_startup_cause()
    if is_dispatcher:
        print(
            f"[{HOOK_NAME}] startup cause: {startup_cause}",
            file=sys.stderr,
        )

    role = "dispatcher" if is_dispatcher else "subagent"
    injected: list[str] = []

    # 1. Inject system bootup file based on role.
    #
    # For dispatcher + compaction start (issue #1954): use the compact stub instead
    # of the full bootup to save ~25-35k tokens.  compact-catchup will restore full
    # situational awareness, so the full bootup is redundant here.  Fall back to the
    # full bootup if the compact stub file is absent (graceful degradation).
    if is_dispatcher:
        is_compact_start = startup_cause == "compaction"
        if is_compact_start and COMPACT_DISPATCHER_BOOTUP.exists():
            content = _read_file_safe(
                COMPACT_DISPATCHER_BOOTUP, "sys.compact-dispatcher.bootup.md"
            )
            system_file = COMPACT_DISPATCHER_BOOTUP
            print(
                f"[{HOOK_NAME}] compact start detected — injecting compact stub"
                f" ({COMPACT_DISPATCHER_BOOTUP.name})",
                file=sys.stderr,
            )
        else:
            if is_compact_start:
                # Compact stub missing — log and fall back to full bootup.
                print(
                    f"[{HOOK_NAME}] compact start but stub absent"
                    f" ({COMPACT_DISPATCHER_BOOTUP}) — falling back to full bootup",
                    file=sys.stderr,
                )
            content = _read_file_safe(DISPATCHER_BOOTUP, "sys.dispatcher.bootup.md")
            system_file = DISPATCHER_BOOTUP
            is_compact_start = False  # treat as non-compact for user config injection
    else:
        content = _read_file_safe(SUBAGENT_BOOTUP, "sys.subagent.bootup.md")
        system_file = SUBAGENT_BOOTUP
        is_compact_start = False

    if content is None:
        _append_injection_log(session_id, role, injected)
        sys.exit(0)

    # For the dispatcher: prepend a single line announcing the startup cause
    # so step 2d of the startup sequence can use it without reading any files.
    # This is the ONLY place startup_cause is surfaced to the model context.
    if is_dispatcher:
        now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cause_banner = (
            f"<!-- startup-cause: {startup_cause} | ts: {now_utc} -->\n"
            f"**Startup cause: {startup_cause}**\n"
            f"(Written by inject-bootup-context.py from last-startup-cause.json)\n\n"
        )
        print(cause_banner, end="")

    print(content)
    injected.append(system_file.name)

    # 2. Inject user base bootup (both roles).
    # Skipped on compact starts — compact-catchup restores context; the full user
    # config would add tokens without meaningful benefit at this point.
    if not is_compact_start:
        if _inject_if_exists(USER_BASE_BOOTUP, "user.base.bootup.md"):
            injected.append(USER_BASE_BOOTUP.name)

    # 3. Inject role-specific user bootup.
    # Also skipped on compact starts for the same reason.
    if is_dispatcher:
        if not is_compact_start:
            if _inject_if_exists(USER_DISPATCHER_BOOTUP, "user.dispatcher.bootup.md"):
                injected.append(USER_DISPATCHER_BOOTUP.name)
    else:
        if _inject_if_exists(USER_SUBAGENT_BOOTUP, "user.subagent.bootup.md"):
            injected.append(USER_SUBAGENT_BOOTUP.name)

    _append_injection_log(session_id, role, injected)
    sys.exit(0)


if __name__ == "__main__":
    main()
