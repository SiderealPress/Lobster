#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent tool calls where the prompt doesn't contain a task_id.

The task_id must appear in the prompt so the subagent can pass it to write_result.
This enables reliable DB matching in the SubagentStop hook, which extracts task_id
from write_result args in the transcript.

This is the spawn-side mirror of require-register-agent-task-id.py: that hook
blocks register_agent calls without task_id; this hook blocks Agent spawns where
the prompt doesn't carry task_id into the subagent's context at all.
"""
import json, sys

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

if "task_id is:" not in prompt:
    print(
        'BLOCKED: Agent spawned without task_id in prompt. '
        'Include "Your task_id is: <slug>" so the subagent can pass it to write_result. '
        'This enables reliable DB matching in the SubagentStop hook.',
        file=sys.stderr,
    )
    sys.exit(2)
