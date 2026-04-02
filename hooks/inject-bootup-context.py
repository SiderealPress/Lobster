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
"""

import json
import os
import sys
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


def _inject_if_exists(path: Path, label: str) -> None:
    """Read and print file contents if the file exists and is non-empty. Silent skip otherwise."""
    if not path.exists():
        return
    try:
        content = path.read_text()
        if content.strip():
            print(content)
    except OSError as exc:
        print(
            f"[{HOOK_NAME}] WARNING: could not read {path} ({label}): {exc}",
            file=sys.stderr,
        )


def main() -> None:
    # Read hook input from stdin to detect dispatcher vs subagent role.
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

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

    # If this is the dispatcher session, write the session ID to the marker file.
    # This makes the hook self-sufficient regardless of whether
    # write-dispatcher-session-id.py ran first. The write is idempotent.
    if is_dispatcher:
        session_id = session_role.get_session_id(hook_input)
        if session_id:
            session_role.write_dispatcher_session_id(session_id)

    # 1. Inject system bootup file based on role.
    if is_dispatcher:
        content = _read_file_safe(DISPATCHER_BOOTUP, "sys.dispatcher.bootup.md")
    else:
        content = _read_file_safe(SUBAGENT_BOOTUP, "sys.subagent.bootup.md")

    if content is None:
        sys.exit(0)

    print(content)

    # 2. Inject user base bootup (both roles).
    _inject_if_exists(USER_BASE_BOOTUP, "user.base.bootup.md")

    # 3. Inject role-specific user bootup.
    if is_dispatcher:
        _inject_if_exists(USER_DISPATCHER_BOOTUP, "user.dispatcher.bootup.md")
    else:
        _inject_if_exists(USER_SUBAGENT_BOOTUP, "user.subagent.bootup.md")

    sys.exit(0)


if __name__ == "__main__":
    main()
