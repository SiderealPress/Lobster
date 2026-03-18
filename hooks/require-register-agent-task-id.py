#!/usr/bin/env python3
"""PreToolUse hook: blocks register_agent calls missing a task_id parameter.

The task_id is required so the SubagentStop hook can reliably match the DB row
by primary key when closing ghost agent entries.
"""
import json, sys

data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool != "mcp__lobster-inbox__register_agent":
    sys.exit(0)

task_id = inp.get("task_id")

if not task_id:
    print(
        "BLOCKED: register_agent called without task_id. "
        "Set task_id to the same value the subagent will use in write_result. "
        "This enables reliable DB matching in the SubagentStop hook.",
        file=sys.stderr,
    )
    sys.exit(2)
