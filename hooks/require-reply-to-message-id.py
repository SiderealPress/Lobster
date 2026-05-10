#!/usr/bin/env python3
"""
PreToolUse hook: enforce reply_to_message_id on Telegram send_reply calls.

Without reply_to_message_id, Telegram replies are sent standalone (not threaded
to the original message). This hook blocks send_reply calls that target Telegram
but omit or set reply_to_message_id=0.

Exemptions (always allowed):
  - Non-Telegram sources (slack, sms, whatsapp, bot-talk, system, ...)
  - System/proactive sends where chat_id == 0 (no incoming message context)
  - Calls where reply_to_message_id is already set to a positive integer

Exit codes:
  0 - Allow the call
  2 - Block the call (Claude Code shows stderr message to Claude)

Issue: #1168
"""

import json
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Unparseable input — allow rather than block (fail-open)
        sys.exit(0)

    tool_name = data.get("tool_name", "")

    # Only check send_reply calls
    if tool_name != "mcp__lobster-inbox__send_reply":
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    source = (tool_input.get("source") or "telegram").lower()

    # Only enforce on Telegram (source absent defaults to telegram per API contract)
    if source != "telegram":
        sys.exit(0)

    # Exempt proactive/system sends: chat_id == 0 means no originating user message
    chat_id = tool_input.get("chat_id")
    try:
        if int(chat_id) == 0:
            sys.exit(0)
    except (TypeError, ValueError):
        # chat_id absent or non-numeric — cannot determine, allow
        sys.exit(0)

    # Check reply_to_message_id: must be present and a positive integer
    reply_to = tool_input.get("reply_to_message_id")
    try:
        if reply_to is not None and int(reply_to) > 0:
            sys.exit(0)  # Properly set — allow
    except (TypeError, ValueError):
        pass  # Non-integer value — fall through to block

    # Block: reply_to_message_id is missing or not a positive integer
    print(
        "BLOCKED: Telegram send_reply is missing reply_to_message_id.\n"
        "Every Telegram reply must include reply_to_message_id (the integer\n"
        "Telegram message ID shown in wait_for_messages output as\n"
        "'pass as reply_to_message_id') to thread the reply correctly.\n\n"
        "If this is a proactive/system send with no originating message,\n"
        "pass chat_id=0 to exempt this check.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
