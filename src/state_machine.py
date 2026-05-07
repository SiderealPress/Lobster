"""
dispatcher state machine — 5-state liveness file writer.

Prototype for issue #1918: replaces multi-file heartbeat approach with a
single dispatcher-state.json that encodes the dispatcher's semantic state.
This lets the health check apply per-state timeouts instead of one blunt
threshold for all situations.

States
------
STARTING    - SessionStart hook fired, before first wait_for_messages call
WAITING     - Dispatcher blocking in wait_for_messages (idle)
PROCESSING  - Dispatcher claimed a message via mark_processing (may be in long inference)
WINDING_DOWN - Context warning received, session ending gracefully
DEAD        - SessionStop fired (tombstone, triggers immediate restart)

File
----
~/lobster-workspace/data/dispatcher-state.json
{
    "state": "WAITING",
    "pid": 12345,
    "session_id": "...",
    "updated_at": "2026-05-02T12:58:00Z",
    "since": "2026-05-02T12:57:55Z"   # when this state was entered
}

Written atomically (write .tmp, os.replace) — safe to read concurrently.
Silent on all errors — must never interrupt a hook or dispatcher operation.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Allow override via env var for tests.
_WORKSPACE_DIR = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
STATE_FILE = Path(
    os.environ.get(
        "LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE",
        _WORKSPACE_DIR / "data" / "dispatcher-state.json",
    )
)

# Valid state names (for documentation; not enforced at write time).
STARTING = "STARTING"
WAITING = "WAITING"
PROCESSING = "PROCESSING"
WINDING_DOWN = "WINDING_DOWN"
DEAD = "DEAD"


def write_state(
    state: str,
    pid: int | None = None,
    session_id: str = "",
) -> None:
    """Write the dispatcher state file atomically.

    Args:
        state: One of STARTING, WAITING, PROCESSING, WINDING_DOWN, DEAD.
        pid: Dispatcher PID. Defaults to os.getpid().
        session_id: Claude session ID string (optional, for debugging).

    The `since` field records when the current state was *entered* and is
    preserved across updates unless the state value changes.  This lets the
    health check compute how long the dispatcher has been in a given state
    without confusion from frequent `updated_at` refreshes.

    Silent on all exceptions — must never interrupt hooks or dispatcher code.
    """
    try:
        now_iso = datetime.now(timezone.utc).isoformat()

        # Preserve `since` if the state has not changed since the last write.
        since_iso = now_iso
        try:
            existing = json.loads(STATE_FILE.read_text())
            if existing.get("state") == state and existing.get("since"):
                since_iso = existing["since"]
        except Exception:
            pass  # File absent or unreadable — use now_iso

        data = {
            "state": state,
            "pid": pid if pid is not None else os.getpid(),
            "session_id": session_id or "",
            "updated_at": now_iso,
            "since": since_iso,
        }
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(str(tmp), str(STATE_FILE))
    except Exception:
        pass  # Never interrupt callers


def read_state() -> dict | None:
    """Read the current dispatcher state file.

    Returns the parsed dict, or None if the file is missing or unreadable.
    """
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <STATE>")
        sys.exit(1)
    write_state(sys.argv[1])
    s = read_state()
    print(json.dumps(s, indent=2))
