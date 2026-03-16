#!/usr/bin/env python3
"""PreToolUse hook: warns when tools that should run in background subagents are
called inline by the dispatcher.

The Lobster dispatcher runs in an infinite message-processing loop. Certain tools
should never be called inline — they block message processing, sometimes for many
seconds or minutes. This hook catches the most common cases and steers Claude
toward delegating to a background subagent instead.

Guarded tools and why:

  WebFetch / WebSearch — network I/O that can take 5–30+ seconds. The dispatcher
    has no reason to fetch web content directly; if web information is needed it
    should always go to a background subagent.

Exit codes:
  0 — tool is not guarded, or usage is acceptable
  1 — soft warning: Claude sees it and can reconsider, but is not hard-blocked

Exit 1 (not 2) is intentional throughout. There may be edge cases where inline
usage is genuinely warranted; hard-blocking would be counterproductive.
"""
import json
import sys

# Tools that should always go to a background subagent, never called inline.
# Map tool name → reason shown in warning message.
_GUARDED_TOOLS: dict[str, str] = {
    "WebFetch": (
        "WebFetch makes a network request that can take 5–30+ seconds, blocking "
        "the dispatcher's message-processing loop for the full duration. "
        "Delegate web fetching to a background subagent instead."
    ),
    "WebSearch": (
        "WebSearch makes a network request that can take several seconds, blocking "
        "the dispatcher's message-processing loop for the full duration. "
        "Delegate web searches to a background subagent instead."
    ),
}

data = json.load(sys.stdin)
tool = data.get("tool_name", "")

reason = _GUARDED_TOOLS.get(tool)
if reason is None:
    sys.exit(0)

print(
    f"Warning: {tool} called inline by the dispatcher. {reason}",
)
sys.exit(1)
