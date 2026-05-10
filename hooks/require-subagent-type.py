#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent calls with no subagent_type, and blocks use of
the generic 'general-purpose' agent type which has no Lobster context.
Encourages use of lobster-generalist or a named agent type instead.
"""
import json, sys

# All agent types defined in .claude/agents/. Keep in sync with that directory.
KNOWN_AGENT_TYPES = (
    "lobster-generalist",
    "functional-engineer",
    "review",
    "lobster-ops",
    "lobster-auditor",
    "brain-dumps",
    "compact-catchup",
    "nightly-consolidation",
    "session-note-appender",
    "session-note-polish",
)

data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool != "Agent":
    sys.exit(0)

subagent_type = inp.get("subagent_type")

if not subagent_type:
    types_list = ", ".join(KNOWN_AGENT_TYPES)
    print(
        "BLOCKED: Agent called without subagent_type. "
        f"Known types: {types_list}. "
        "Use subagent_type='lobster-generalist' for general background tasks.",
        file=sys.stderr,
    )
    sys.exit(2)

if subagent_type == "general-purpose":
    types_list = ", ".join(KNOWN_AGENT_TYPES)
    print(
        "BLOCKED: subagent_type='general-purpose' is not used in Lobster. "
        "Use subagent_type='lobster-generalist' for open-ended background tasks instead. "
        f"For specialised work, choose from: {types_list}.",
        file=sys.stderr,
    )
    sys.exit(2)
