#!/usr/bin/env python3
"""PreToolUse hook: blocks the dispatcher from calling Agent without run_in_background: true.

The Lobster dispatcher runs in an infinite message-processing loop. A foreground
Agent call (run_in_background omitted or false) blocks the dispatcher for the
full duration of the agent — potentially minutes — while incoming messages queue
up unprocessed and health-check pings go unanswered.

This hook enforces the 7-second rule as a hard constraint for the dispatcher.
Subagents are exempt: they may legitimately spawn nested agents synchronously
when the result is needed to decide the next step.

Note: Claude Code has used both "Agent" and "Task" as the tool name for spawning
subagents across versions. Both are treated identically.

Exit codes:
  0 — tool is not Agent/Task, Agent has run_in_background: true, or session is a subagent
  2 — hard block: dispatcher called Agent/Task without run_in_background: true
"""
import json
import sys
from pathlib import Path

# Import the shared dispatcher/subagent detection utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher

# Tool names used to spawn subagents across CC versions.
AGENT_TOOL_NAMES = {"Agent", "Task"}

data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool not in AGENT_TOOL_NAMES:
    sys.exit(0)

if inp.get("run_in_background") is True:
    sys.exit(0)

# Only enforce for the dispatcher. Subagents may call Agent synchronously.
if not is_dispatcher(data):
    sys.exit(0)

print(
    "BLOCKED: Dispatcher called Agent without run_in_background: true. "
    "This blocks the message-processing loop for the full duration of the "
    "subagent. Always pass run_in_background=True when spawning agents from "
    "the dispatcher. The result will be delivered via write_result.",
    file=sys.stderr,
)
sys.exit(2)
