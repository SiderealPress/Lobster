#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent calls with no subagent_type, and blocks use of
the generic 'general-purpose' agent type which has no Lobster context.
Encourages use of lobster-generalist or a named agent type instead.
"""
import json, sys

data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool != "Agent":
    sys.exit(0)

subagent_type = inp.get("subagent_type")

if not subagent_type:
    print(
        "BLOCKED: Agent called without subagent_type. "
        "Use subagent_type='lobster-generalist' for general background tasks, "
        "or a named agent type (functional-engineer, lobster-ops, brain-dumps, etc.).",
        file=sys.stderr,
    )
    sys.exit(2)

if subagent_type == "general-purpose":
    print(
        "BLOCKED: subagent_type='general-purpose' is not used in Lobster. "
        "Use subagent_type='lobster-generalist' for open-ended background tasks instead. "
        "For specialised work, use: functional-engineer, lobster-ops, or brain-dumps.",
        file=sys.stderr,
    )
    sys.exit(2)
