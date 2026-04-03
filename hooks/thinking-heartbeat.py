#!/usr/bin/env python3
"""
PostToolUse hook: thinking heartbeat.

Writes last_thinking_at (ISO UTC timestamp) to lobster-state.json on every
PostToolUse event. The health check reads this field and folds it into the
effective freshness signal alongside the WFM heartbeat file and last_processed_at.

Purpose: the dispatcher can spend 10+ minutes in a reasoning phase (thinking,
composing responses, spawning subagents) without touching wait_for_messages or
mark_processed. During this window the health check sees no activity and may
incorrectly conclude the dispatcher is frozen. Any tool call at all means the
dispatcher is alive — this hook captures that signal.

Design:
- Unconditional: fires on every PostToolUse (no tool-name filtering needed)
- Atomic write: write to .tmp, then os.rename() to avoid partial reads
- Merge: read existing JSON, update last_thinking_at, write back — no overwrite
- Silent on failure: health check degrades gracefully when field is absent
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
STATE_FILE = Path(os.environ.get("LOBSTER_STATE_FILE_OVERRIDE", MESSAGES_DIR / "config" / "lobster-state.json"))


def _read_state(path: Path) -> dict:
    """Return existing state dict, or empty dict if file is absent or unparseable."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state_atomic(path: Path, state: dict) -> None:
    """Write state dict atomically: write to .tmp then rename."""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.rename(str(tmp), str(path))


def write_thinking_heartbeat(state_file: Path) -> None:
    """Merge last_thinking_at into state_file, creating it if absent."""
    state = _read_state(state_file)
    state["last_thinking_at"] = datetime.now(timezone.utc).isoformat()
    _write_state_atomic(state_file, state)


def main() -> None:
    try:
        write_thinking_heartbeat(STATE_FILE)
    except Exception:
        # Never block tool execution — health check degrades gracefully if field is absent
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
