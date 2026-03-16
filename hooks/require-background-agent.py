#!/usr/bin/env python3
"""PreToolUse hook: warns when the Agent tool is called without run_in_background: true.

The Lobster dispatcher runs in an infinite message-processing loop. A foreground
Agent call (run_in_background omitted or false) blocks the dispatcher for the
full duration of the agent — potentially minutes — while incoming messages queue
up unprocessed.

Exit codes:
  0 — tool is not Agent, or Agent is called with run_in_background: true (OK)
  1 — soft warning: Agent called without run_in_background: true

Exit 1 (not 2) is intentional. Claude sees the warning and can reconsider, but
is not hard-blocked. There are legitimate cases where a foreground Agent is
genuinely needed (e.g., the result is required synchronously to decide the next
step). Hard-blocking those would be counterproductive.
"""
import json
import sys

data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool != "Agent":
    sys.exit(0)

if inp.get("run_in_background") is True:
    sys.exit(0)

print(
    "Warning: Agent tool called without run_in_background: true. "
    "This blocks message processing for the duration of the agent. "
    "Pass run_in_background: true unless you genuinely need the result "
    "synchronously to decide your next step.",
)
sys.exit(1)
