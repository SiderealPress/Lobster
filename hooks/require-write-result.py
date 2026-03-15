#!/usr/bin/env python3
"""
Stop hook: ensure subagents call write_result before exiting.
Injects a reminder if write_result was not called during the session.

Dispatcher sessions (detected via session_role.is_dispatcher()) are exempt —
the dispatcher never calls write_result, so the check only applies to subagents.
"""
import json
import sys
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # If we can't read transcript, don't block

    # Dispatcher sessions are exempt — skip the write_result check.
    # session_role.is_dispatcher() uses the marker file as primary signal and
    # the transcript (which is present in Stop hooks) as fallback.
    if is_dispatcher(data):
        sys.exit(0)

    transcript = data.get("transcript", [])

    tool_calls = []
    for msg in transcript:
        if isinstance(msg, dict):
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tool_calls.append(item.get("name", ""))

    # If this session called write_result, we're good
    if "mcp__lobster-inbox__write_result" in tool_calls:
        sys.exit(0)

    # Subagent finished without calling write_result — block exit
    print(
        "STOP: You must call mcp__lobster-inbox__write_result before finishing. "
        "The dispatcher is waiting for your result. "
        "If the task failed, report the failure — but you must call write_result. "
        "Call it now with your findings, then you may exit."
    )
    sys.exit(2)  # Exit 2 to hard-block the session from terminating


if __name__ == "__main__":
    main()
