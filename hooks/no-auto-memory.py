#!/usr/bin/env python3
"""
Block writes to Claude Code's auto-memory directory.

Persistent state belongs in CLAUDE.md or canonical memory files, not .claude/memory/.
Based on sayhar/claude-bicycle hooks/no-auto-memory.py
"""

import json
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    file_path = data.get("tool_input", {}).get("file_path", "")

    if "/.claude/" not in file_path or "/memory/" not in file_path:
        sys.exit(0)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Don't use auto-memory. Instead update CLAUDE.md or "
                "canonical memory files (memory/priorities.md, etc.)."
            ),
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
