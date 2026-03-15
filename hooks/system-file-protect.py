#!/usr/bin/env python3
"""
Block Edit/Write/NotebookEdit tool calls on Lobster system files.

System files are anything under ~/lobster/ (the installed repo):
  hooks/, src/, scripts/, install.sh, service files, etc.

NOT blocked: ~/lobster-workspace/, ~/lobster-user-config/, ~/messages/

Override: set LOBSTER_DEBUG=true to allow edits during development.
"""

import json
import os
import sys
from pathlib import Path

DENY_REASON = (
    "Blocked: {path!r} is a Lobster system file. "
    "Editing system files during normal operation is not allowed. "
    "To make intentional changes, set LOBSTER_DEBUG=true and re-run."
)

# Resolved once at module load
_HOME = str(Path.home())
_LOBSTER_DIR = os.path.join(_HOME, "lobster")


def is_debug_mode() -> bool:
    return os.environ.get("LOBSTER_DEBUG", "").lower() == "true"


def is_system_file(file_path: str) -> bool:
    """Return True if file_path is inside the Lobster system directory."""
    if not file_path:
        return False
    # Normalise: expand ~ and resolve symlinks-free absolute path
    expanded = os.path.expanduser(file_path)
    # Use os.path.abspath so we don't need the file to exist yet
    abs_path = os.path.abspath(expanded)
    # Must be under ~/lobster/ (the repo), not ~/lobster-workspace/ etc.
    # Ensure we match the directory itself and not a prefix collision
    # (e.g. ~/lobster-workspace should NOT match ~/lobster/).
    return abs_path == _LOBSTER_DIR or abs_path.startswith(_LOBSTER_DIR + os.sep)


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name not in ("Edit", "Write", "NotebookEdit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")

    if not is_system_file(file_path):
        sys.exit(0)

    if is_debug_mode():
        sys.exit(0)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON.format(path=file_path),
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
