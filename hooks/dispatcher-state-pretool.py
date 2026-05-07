#!/usr/bin/env python3
"""
PreToolUse hook: write dispatcher state transitions.

Prototype for issue #1918 (5-state liveness machine).

Transitions:
  wait_for_messages  → WAITING   (dispatcher is idle, blocking for messages)
  mark_processing    → PROCESSING (dispatcher claimed a message, may run long inference)

Dispatcher-only guard: uses is_dispatcher_session() from session_role.py —
same pattern as pre-tool-heartbeat.py and thinking-heartbeat.py.

Silent on all errors — must never block tool execution.
"""

import json
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(_HOOKS_DIR))

import session_role  # noqa: E402

# Import state machine (same directory as this hook).
_LOBSTER_DIR = _HOOKS_DIR.parent
sys.path.insert(0, str(_LOBSTER_DIR / "src"))
import state_machine  # noqa: E402


# Tool names that trigger state transitions.
_WFM_TOOL = "mcp__lobster-inbox__wait_for_messages"
_MARK_PROCESSING_TOOL = "mcp__lobster-inbox__mark_processing"


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, EOFError):
        sys.exit(0)

    # Dispatcher-only guard: subagents have agent_id in hook payload.
    if not session_role.is_dispatcher_session(hook_input):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    session_id = hook_input.get("session_id", "")

    try:
        if tool_name == _WFM_TOOL:
            state_machine.write_state(state_machine.WAITING, session_id=session_id)
        elif tool_name == _MARK_PROCESSING_TOOL:
            state_machine.write_state(state_machine.PROCESSING, session_id=session_id)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
