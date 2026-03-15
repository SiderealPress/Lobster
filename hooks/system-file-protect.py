#!/usr/bin/env python3
"""
Block Edit/Write/NotebookEdit/Bash tool calls on Lobster system files.

System files are anything under ~/lobster/ (the installed repo):
  hooks/, src/, scripts/, install.sh, service files, etc.

NOT blocked: ~/lobster-workspace/, ~/lobster-user-config/, ~/messages/

Override: set LOBSTER_DEBUG=true to allow edits during development.
"""

import json
import os
import re
import sys
from pathlib import Path

DENY_REASON = (
    "Blocked: {path!r} is a Lobster system file. "
    "Editing system files during normal operation is not allowed. "
    "To make intentional changes, set LOBSTER_DEBUG=true and re-run."
)

DENY_REASON_BASH = (
    "Blocked: Bash command appears to write to a Lobster system file under ~/lobster/. "
    "Editing system files during normal operation is not allowed. "
    "To make intentional changes, set LOBSTER_DEBUG=true and re-run."
)

# Resolved once at module load
_HOME = str(Path.home())
_LOBSTER_DIR = os.path.join(_HOME, "lobster")

# Patterns that indicate a write/create operation in a Bash command.
# We match the operator or command name; path checking is done separately.
# Redirect operators: > and >> (but not just < for reads)
# Write commands: tee, sed -i, install, rsync, patch, touch, chmod, chown, mkdir, rm, ln
# cp and mv are handled separately (destination-aware) to avoid false positives
#   when ~/lobster/ is the source and a non-system path is the destination.
# We deliberately exclude: cat (reading), ls, grep, find, echo without redirect
_BASH_WRITE_OPS = re.compile(
    r"""
    (?:
        >>?                         # redirect: > or >>
        | \btee\b                   # tee writes to a file
        | \bsed\s+[^|;]*?-i        # sed -i (in-place edit)
        | \binstall\b               # install command
        | \brsync\b                 # rsync (conservative: flag all)
        | \bpatch\b                 # patch applies writes
        | \btouch\b                 # touch creates files
        | \bchmod\b                 # chmod modifies file metadata
        | \bchown\b                 # chown modifies file metadata
        | \bmkdir\b                 # mkdir creates directories
        | \brm\b                    # rm deletes files
        | \bln\b                    # ln creates links
    )
    """,
    re.VERBOSE,

)



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


def _lobster_path_variants() -> list[str]:
    """Return the path strings we look for in a Bash command."""
    return [
        _LOBSTER_DIR + "/",   # /home/lobster/lobster/
        _LOBSTER_DIR + os.sep,
        "~/lobster/",          # tilde form
        "$HOME/lobster/",      # $HOME form
        "${HOME}/lobster/",
    ]


def _cp_mv_writes_to_lobster(command: str) -> bool:
    """
    Return True only if a cp or mv command has ~/lobster/ (or the absolute
    equivalent) as its destination — i.e., as the last whitespace-delimited
    token that looks like a path.

    We tokenize each sub-command (split on |, ;, &&, ||, newline) and check
    if the final non-flag argument starts with a lobster path prefix.
    """
    lobster_prefixes = tuple(_lobster_path_variants())
    # Split on common shell statement separators to handle chained commands
    sub_commands = re.split(r'[|;&\n]|\band\b|\bor\b', command)
    for sub in sub_commands:
        tokens = sub.split()
        if not tokens:
            continue
        cmd0 = tokens[0].lstrip()
        if cmd0 not in ("cp", "mv"):
            continue
        # Find the last token that looks like a path (non-flag argument)
        path_args = [t for t in tokens[1:] if not t.startswith("-")]
        if not path_args:
            continue
        dest = path_args[-1]
        if dest.startswith(lobster_prefixes):
            return True
    return False


def bash_writes_system_file(command: str) -> bool:
    """
    Return True if the Bash command contains both:
      1. A write/modify operation targeting ~/lobster/, AND
      2. A clear reference to a path inside ~/lobster/

    We are deliberately conservative: only flag commands that contain an
    obvious write operator/command AND a clear reference to ~/lobster/.
    False negatives (missed blocks) are preferred over false positives.

    cp/mv are treated specially: we only block when ~/lobster/ is the
    destination (last argument), not when it is the source.
    """
    # Fast path: if no lobster path reference at all, skip.
    lobster_variants = _lobster_path_variants()
    if not any(v in command for v in lobster_variants):
        return False

    # For cp/mv, only block when the lobster path is the destination (last arg).
    # We split on shell statement separators and check each sub-command.
    if re.search(r'\b(cp|mv)\b', command):
        if _cp_mv_writes_to_lobster(command):
            return True
        # If this command contains only cp/mv (no other write ops), allow it.
        if not _BASH_WRITE_OPS.search(command):
            return False
        # Fall through to check other write ops below.

    # Check for other write-like operations in the command.
    return bool(_BASH_WRITE_OPS.search(command))


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if is_debug_mode():
        sys.exit(0)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not bash_writes_system_file(command):
            sys.exit(0)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": DENY_REASON_BASH,
            }
        }))
        sys.exit(0)

    if tool_name not in ("Edit", "Write", "NotebookEdit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")

    if not is_system_file(file_path):
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
