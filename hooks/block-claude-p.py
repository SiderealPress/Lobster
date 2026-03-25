#!/usr/bin/env python3
"""
block-claude-p.py — PreToolUse hook that blocks subagents from writing or
executing `claude -p` / `claude --print` invocations.

Applies to: Bash, Write, Edit tool calls.

`claude -p` should never appear in:
  - Bash commands run by agents
  - Files written/edited by agents (shell scripts, Python scripts, task files)

The only legitimate caller of `claude -p` is the Lobster infrastructure itself
(run-job.sh, claude-persistent.sh). Those are committed files, not generated
by subagents at runtime.

Override: not available — this is a hard block. If you need to discuss `claude -p`
in a comment or string literal, prefix the flag with a zero-width space or use
the long form description in prose only.
"""
import json
import re
import sys

BLOCK_MESSAGE = (
    "BLOCKED: `claude -p` / `claude --print` must not be used in agent-generated "
    "code or commands. Spawning Claude subprocesses from within Lobster creates "
    "runaway process chains and MCP connection instability. "
    "Use a simple Python/bash script instead (httpx, requests, subprocess). "
    "If you need LLM reasoning, use a Lobster subagent via the Agent tool."
)

# Patterns that indicate a live invocation (not just a string in prose)
CLAUDE_P_PATTERN = re.compile(
    r'claude\s+((-p\b|--print\b))',
)


def check_bash(command: str) -> bool:
    """Return True if the command contains a claude -p invocation."""
    return bool(CLAUDE_P_PATTERN.search(command))


def check_file_content(content: str) -> bool:
    """Return True if the file content contains a claude -p invocation."""
    for line in content.splitlines():
        stripped = line.strip()
        # Skip pure comments and prose
        if stripped.startswith("#") and "claude -p" not in stripped[:20]:
            continue
        if CLAUDE_P_PATTERN.search(line):
            return True
    return False


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    blocked = False

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if check_bash(command):
            blocked = True

    elif tool_name in ("Write", "Edit", "NotebookEdit"):
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        if content and check_file_content(content):
            blocked = True

    if blocked:
        print(BLOCK_MESSAGE, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
