#!/usr/bin/env python3
"""
Stop hook: ensure subagents call write_result before exiting.
Injects a reminder if write_result was not called during the session.
"""
import json
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # If we can't read transcript, don't block

    # Check if this is a subagent session (not the dispatcher)
    # Dispatchers call wait_for_messages; subagents don't
    transcript = data.get("transcript", [])

    tool_calls = []
    for msg in transcript:
        if isinstance(msg, dict):
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tool_calls.append(item.get("name", ""))

    # If this session called wait_for_messages, it's the dispatcher — skip
    if "mcp__lobster-inbox__wait_for_messages" in tool_calls:
        sys.exit(0)

    # If this session called write_result, we're good
    if "mcp__lobster-inbox__write_result" in tool_calls:
        sys.exit(0)

    # Subagent finished without calling write_result — inject reminder
    print(
        "STOP: You must call mcp__lobster-inbox__write_result before finishing. "
        "The dispatcher is waiting for your result. "
        "If the task failed, report the failure — but you must call write_result. "
        "Call it now with your findings, then you may exit."
    )
    sys.exit(0)  # Exit 0 so the message is injected (not a hard block)


if __name__ == "__main__":
    main()
