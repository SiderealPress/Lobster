#!/usr/bin/env python3
"""
SessionStop hook: write DEAD state for dispatcher.

Part of the 5-state liveness machine (issue #1918).

Writes DEAD state when the dispatcher session ends so the health check can
immediately restart (rather than waiting for the heartbeat to go stale).

Dispatcher detection: uses is_dispatcher_session() from session_role.py.

Why is_dispatcher_session() and NOT is_dispatcher():
- is_dispatcher() reads the startup-flag file written by the launcher before
  exec-ing claude. That flag is DELETED by inject-bootup-context.py at
  SessionStart time. By the time SessionStop fires, the flag is always absent,
  so is_dispatcher() always returns False for Stop hooks.
- is_dispatcher_session() uses agent_id fast path → MCP state files →
  process-tree walk. The dispatcher process is still alive during SessionStop,
  so the process-tree walk is reliable here.

inject-bootup-context.py writes the session ID to the marker file (DISPATCHER_SESSION_FILE)
when it detects the dispatcher via startup flag, giving is_dispatcher_session()
a reliable state file to read without falling back to the process tree.

Silent on all errors.
"""

import json
import os
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(_HOOKS_DIR))

import session_role  # noqa: E402

_LOBSTER_DIR = _HOOKS_DIR.parent
sys.path.insert(0, str(_LOBSTER_DIR / "src"))
import state_machine  # noqa: E402


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, EOFError):
        hook_input = {}

    # Use is_dispatcher_session() — not is_dispatcher().
    # is_dispatcher() checks the startup-flag file which is deleted at SessionStart;
    # it always returns False during SessionStop. is_dispatcher_session() uses
    # state files and process-tree walk which remain valid throughout the session.
    if not session_role.is_dispatcher_session(hook_input):
        sys.exit(0)

    try:
        session_id = hook_input.get("session_id", "")
        state_machine.write_state(state_machine.DEAD, session_id=session_id)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
