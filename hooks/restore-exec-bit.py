#!/usr/bin/env python3
"""
PostToolUse hook: restore the executable bit after Edit or Write tool calls.

The Edit and Write tools rewrite file content and can strip the execute bit.
This hook fires after those tools complete and restores +x when appropriate.

Heuristics for deciding a file should be executable (any one match is enough):
1. File extension is .sh or .exp
2. File is inside a directory named 'scripts'
3. The file already has +x set (another process may have set it intentionally)
4. The file starts with a shebang line (#!) -- script that lost its +x

Exit codes: 0 always (PostToolUse hooks should not block; we just side-effect).
"""

import json
import os
import stat
import sys
from pathlib import Path


# Extensions that are always executable
EXEC_EXTENSIONS = {".sh", ".exp"}

# Directory names that imply executability
EXEC_DIRS = {"scripts", "bin", "hooks"}


def has_shebang(path: Path) -> bool:
    """Return True if the file starts with a shebang (#!)."""
    try:
        with path.open("rb") as f:
            header = f.read(2)
        return header == b"#!"
    except OSError:
        return False


def is_currently_executable(path: Path) -> bool:
    """Return True if the file currently has any execute bit set."""
    try:
        mode = path.stat().st_mode
        return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    except OSError:
        return False


def should_be_executable(path: Path) -> bool:
    """
    Return True if this file should have the execute bit.

    Order of checks (fast to slow):
    1. Extension match (.sh, .exp) — unconditional
    2. Parent directory name in EXEC_DIRS — unconditional
    3. Already has +x — leave it as-is (another agent/process set it)
    4. Starts with a shebang line — scripts that got their +x stripped
    """
    if path.suffix.lower() in EXEC_EXTENSIONS:
        return True

    if path.parent.name.lower() in EXEC_DIRS:
        return True

    if is_currently_executable(path):
        return True

    if has_shebang(path):
        return True

    return False


def restore_exec_bit(path: Path) -> None:
    """Add +x for owner (and group/other if they can read) to path."""
    try:
        current_mode = path.stat().st_mode
        # Add execute wherever read is set
        new_mode = current_mode
        if current_mode & stat.S_IRUSR:
            new_mode |= stat.S_IXUSR
        if current_mode & stat.S_IRGRP:
            new_mode |= stat.S_IXGRP
        if current_mode & stat.S_IROTH:
            new_mode |= stat.S_IXOTH
        if new_mode != current_mode:
            os.chmod(path, new_mode)
    except OSError:
        pass  # Best-effort; never fail the hook


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name not in ("Edit", "Write"):
        sys.exit(0)

    raw_path = tool_input.get("file_path", "")
    if not raw_path:
        sys.exit(0)

    path = Path(raw_path)
    if not path.exists() or not path.is_file():
        sys.exit(0)

    if should_be_executable(path):
        restore_exec_bit(path)

    sys.exit(0)


if __name__ == "__main__":
    main()
