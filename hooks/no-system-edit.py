#!/usr/bin/env python3
"""Block edits to system files (CLAUDE.md, system agents, hooks, services)."""
import json, sys

PROTECTED_PATTERNS = [
    "/lobster/CLAUDE.md",
    "/lobster/.claude/agents/",
    "/lobster/hooks/",
    "/lobster/.githooks/",
    "/lobster/services/",
    "/lobster/src/",
]


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    # Check Write/Edit targets
    if file_path:
        for pattern in PROTECTED_PATTERNS:
            if pattern in file_path:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Cannot edit system file: {file_path}. "
                            "System files are read-only and git-managed. "
                            "Use user/agents/*.agent.md for customization."
                        ),
                    }
                }))
                sys.exit(0)

    # Check Bash commands targeting system files
    command = tool_input.get("command", "")
    if command:
        # Only block write operations, not reads
        write_ops = ["rm ", "mv ", "cp ", "> ", ">> ", "tee ", "sed -i", "chmod ", "chown "]
        if any(op in command for op in write_ops):
            for pattern in PROTECTED_PATTERNS:
                if pattern in command:
                    print(json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"Cannot modify system files via bash. "
                                "System files are read-only and git-managed."
                            ),
                        }
                    }))
                    sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
