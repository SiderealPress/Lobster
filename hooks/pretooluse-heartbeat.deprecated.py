#!/usr/bin/env python3
# DEPRECATED: This hook was deregistered by migration 90 (PR #1988).
# If this fires, it means settings.json was corrupted or a migration re-registered it.

"""
DEPRECATED — do not register or use this hook.

This file is retained for audit purposes only. It should never be registered
in settings.json. If this hook fires, it means a settings.json entry for
pretooluse-heartbeat was re-added incorrectly.

Superseded by: hooks/pre-tool-heartbeat.py (issue #1786, PR #1817)
  - pre-tool-heartbeat.py adds a dispatcher-only guard so subagent tool calls
    cannot falsely update the heartbeat timestamp.
  - This file wrote last_pretooluse_at unconditionally (no dispatcher guard).

Deregistered by: migration 90 (upgrade.sh), which removes this hook from
  settings.json on any install that still has it registered.

If this file fires: log the event and exit 0 so tool execution is not blocked.
Report the incident — it means settings.json was manually edited or a migration
re-registered this hook after migration 90 removed it.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
LOG_FILE = MESSAGES_DIR / "logs" / "deprecated-hook-fired.jsonl"


def log_unexpected_firing() -> None:
    """Write a warning to the log file so this unexpected firing is observable."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "deprecated_hook_fired",
        "hook": "pretooluse-heartbeat.deprecated.py",
        "message": (
            "DEPRECATED hook fired — pretooluse-heartbeat.deprecated.py should not "
            "be registered in settings.json. Check that migration 90 has run and "
            "that no settings.json entry points to pretooluse-heartbeat."
        ),
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # Secondary /tmp marker for quick observability.  Wrapped in its own
    # try/except so a read-only or missing /tmp cannot propagate an exception
    # out of this function — tool execution must never be blocked.
    try:
        Path("/tmp/pretooluse-heartbeat-deprecated-fired.log").write_text(
            f"FIRED at {datetime.now(timezone.utc).isoformat()}Z\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def main() -> None:
    try:
        log_unexpected_firing()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
