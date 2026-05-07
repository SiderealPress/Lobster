#!/usr/bin/env python3
"""
SessionStop hook: write DEAD state for dispatcher.

Prototype for issue #1918 (5-state liveness machine).

Writes DEAD state when the dispatcher session ends so the health check can
immediately restart (rather than waiting for the heartbeat to go stale).

Dispatcher detection: uses is_dispatcher() from session_role.py (not
is_dispatcher_session()) — correct for SessionStop hooks per the existing
convention (see thinking-heartbeat.py docstring for the is_dispatcher vs
is_dispatcher_session distinction).

Silent on all errors.
"""

import json
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

    # For SessionStop, use is_dispatcher() which checks the startup flag file.
    # Note: by SessionStop, the flag may already have been consumed by SessionStart.
    # Fall back to is_dispatcher_session() as a secondary check.
    is_disp = session_role.is_dispatcher(hook_input) or session_role.is_dispatcher_session(hook_input)
    if not is_disp:
        sys.exit(0)

    try:
        session_id = hook_input.get("session_id", "")
        state_machine.write_state(state_machine.DEAD, session_id=session_id)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
