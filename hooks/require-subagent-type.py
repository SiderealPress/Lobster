#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent calls with no subagent_type.
Encourages explicit agent routing via lobster-generalist or a named agent.
"""
import json, sys

data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool == "Agent" and not inp.get("subagent_type"):
    print("BLOCKED: Agent called without subagent_type. Use subagent_type='lobster-generalist' for general background tasks, or a named agent type.", file=sys.stderr)
    sys.exit(1)
