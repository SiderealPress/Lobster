#!/usr/bin/env python3
"""
Context-compaction hook for Lobster.

Fires on SessionStart with a 'compact' event. Injects a system message into
the Lobster inbox so that the next call to wait_for_messages() surfaces a
reminder to re-read CLAUDE.md and re-orient from handoff/memory context.

The script is idempotent: if a compact-reminder message already exists in the
inbox it skips writing a duplicate.
"""

import json
import os
import time
from pathlib import Path


INBOX_DIR = Path(os.path.expanduser("~/messages/inbox"))

REMINDER_TEXT = (
    "Your context was just compacted. Re-read CLAUDE.md \u2014 it will guide you "
    "to the handoff, memory, and all other bootup context you need to re-orient."
)


def already_pending() -> bool:
    """Return True if a compact-reminder message is already sitting in the inbox."""
    if not INBOX_DIR.exists():
        return False
    for path in INBOX_DIR.iterdir():
        if not path.suffix == ".json":
            continue
        try:
            data = json.loads(path.read_text())
            if data.get("subtype") == "compact-reminder":
                return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def write_reminder() -> None:
    """Write a compact-reminder system message to the inbox."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)

    # Timestamp in milliseconds, matching the pattern used by other inbox files.
    ts_ms = int(time.time() * 1000)
    message_id = f"{ts_ms}_compact"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000"

    message = {
        "id": message_id,
        "source": "system",
        "chat_id": 0,
        "user_id": 0,
        "username": "lobster-system",
        "user_name": "System",
        "type": "text",
        "subtype": "compact-reminder",
        "text": REMINDER_TEXT,
        "timestamp": timestamp,
    }

    dest = INBOX_DIR / f"{message_id}.json"
    dest.write_text(json.dumps(message, indent=2) + "\n")


def main() -> None:
    if already_pending():
        return
    write_reminder()


if __name__ == "__main__":
    main()
