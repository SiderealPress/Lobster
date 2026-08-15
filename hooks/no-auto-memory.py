#!/usr/bin/env python3
"""
Block writes to Claude Code's auto-memory directory.

Persistent state belongs in CLAUDE.md or Lobster's MCP memory system,
not Claude Code's .claude/memory/ auto-memory.
Based on sayhar/claude-bicycle hooks/no-auto-memory.py
"""

import json
import re
import sys

DENY_REASON = (
    "Don't use Claude Code auto-memory. "
    "Use Lobster's MCP memory system instead "
    "(memory_store, memory_search, memory_recent)."
)

# Bash commands that write/create files or directories
_BASH_WRITE_PATTERN = re.compile(
    r"\b(mkdir|touch|cp|mv|tee|install|echo\s.*>|printf\s.*>|cat\s.*>|>\.*)\b"
)


def is_memory_file_path(file_path: str) -> bool:
    """Return True if file_path targets Claude Code's auto-memory directory."""
    return "/.claude/" in file_path and "/memory/" in file_path


def is_bash_memory_write(command: str) -> bool:
    """
    Return True if a Bash command would create or write to Claude Code's
    auto-memory directories.

    We look for commands that:
    - Reference both '.claude' and 'memory' in the path, AND
    - Are write/create operations (mkdir, touch, cp, mv, tee, redirects, etc.)

    Read-only commands (ls, cat reading, grep, etc.) are allowed through.
    """
    if ".claude" not in command or "memory" not in command:
        return False

    # Only block if the command looks like a write/create operation
    return bool(_BASH_WRITE_PATTERN.search(command))


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    should_deny = False

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        should_deny = is_bash_memory_write(command)
    else:
        # Write, Edit, and any other file-path based tools
        file_path = tool_input.get("file_path", "")
        should_deny = is_memory_file_path(file_path)

    if not should_deny:
        sys.exit(0)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
