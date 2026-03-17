#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent tool calls where the prompt doesn't contain a task_id.

The task_id must appear in the prompt so the subagent can pass it to write_result.
This enables reliable DB matching in the SubagentStop hook, which extracts task_id
from write_result args in the transcript.

This is the spawn-side mirror of require-register-agent-task-id.py: that hook
blocks register_agent calls without task_id; this hook blocks Agent spawns where
the prompt doesn't carry task_id into the subagent's context at all.

## Accepted formats

YAML frontmatter (preferred):

    ---
    task_id: my-task
    chat_id: 8305714125
    source: telegram
    ---

Legacy text format (backward compat, still accepted):

    Your task_id is: my-task
"""
import json
import re
import sys


def _has_task_id_in_frontmatter(prompt: str) -> bool:
    """Return True if prompt starts with YAML frontmatter containing a task_id key."""
    prompt = prompt.lstrip()
    if not prompt.startswith("---"):
        return False

    rest = prompt[3:]
    end = rest.find("\n---")
    if end == -1:
        return False

    block = rest[:end]
    for line in block.splitlines():
        line = line.strip()
        if re.match(r"^task_id\s*:", line):
            # Check the value is non-empty
            _, _, value = line.partition(":")
            if value.strip():
                return True

    return False


def _has_task_id_in_text(prompt: str) -> bool:
    """Return True if prompt contains the legacy 'task_id is: X' pattern."""
    return bool(re.search(r"task_id\s+is:\s*\S+", prompt, re.IGNORECASE))


try:
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {})
except Exception as e:
    print(f"Warning: require-task-id-in-prompt hook received malformed input: {e}", file=sys.stderr)
    sys.exit(0)

if tool != "Agent":
    sys.exit(0)

prompt = inp.get("prompt", "")

if _has_task_id_in_frontmatter(prompt) or _has_task_id_in_text(prompt):
    sys.exit(0)

print(
    'BLOCKED: Agent spawned without task_id in prompt. '
    'Include a YAML frontmatter block (---\\ntask_id: <slug>\\n---) at the top of the prompt, '
    'or the legacy format "Your task_id is: <slug>". '
    'This enables reliable DB matching in the SubagentStop hook.',
    file=sys.stderr,
)
sys.exit(2)
