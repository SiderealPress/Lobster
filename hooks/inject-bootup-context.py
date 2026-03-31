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
