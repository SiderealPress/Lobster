#!/usr/bin/env python3
"""
Context-compaction hook for Lobster.

Fires on SessionStart with a 'compact' event. Injects a system message into
the Lobster inbox so that the next call to wait_for_messages() surfaces a
reminder to re-read CLAUDE.md and re-orient from handoff/memory context.

The script is idempotent: if a compact-reminder message already exists in the
inbox it skips writing a duplicate.

Notification: always sends a Telegram message directly to the owner's chat ID
so the user is immediately notified that a compaction occurred.  The health
check suppresses its own alerts during the compaction window so exactly one
notification reaches the user per compaction event.

State: always writes compacted_at to lobster-state.json so that the health
check can suppress stale-inbox false-positives during the compaction pause.

Dispatcher-only: exits immediately for subagent sessions (detected via
session_role.is_dispatcher()). Subagent compactions must not write compact-
reminders or the sentinel — those signals are only meaningful to the dispatcher.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher


INBOX_DIR = Path(os.path.expanduser("~/messages/inbox"))
CONFIG_ENV = Path(os.path.expanduser("~/lobster-config/config.env"))
STATE_FILE = Path(
    os.environ.get(
        "LOBSTER_STATE_FILE_OVERRIDE",
        os.path.expanduser("~/messages/config/lobster-state.json"),
    )
)

REMINDER_TEXT = (
    "COMPACT REMINDER \u2014 RE-ORIENT NOW\n\n"
    "You are Lobster, the always-on dispatcher. Your role has not changed.\n\n"
    "Identity check:\n"
    "- You run in an infinite main loop: wait_for_messages() \u2192 process each message \u2192 repeat\n"
    "- You NEVER exit. You NEVER stop calling wait_for_messages.\n"
    "- You are a stateless dispatcher. Anything >7 seconds goes to a background subagent.\n\n"
    "Read these files now to restore full context:\n"
    "1. ~/lobster-workspace/.claude/sys.dispatcher.bootup.md\n"
    "  \u2190 dispatcher instructions, main loop, 7-second rule\n"
    "2. ~/lobster-user-config/memory/canonical/handoff.md\n"
    "  \u2190 active projects, key people, priorities\n\n"
    "After reading: resume your main loop by calling wait_for_messages()."
)

SENTINEL_FILE = Path(os.path.expanduser("~/messages/config/compact-pending"))

COMPACTION_TELEGRAM_MESSAGE = "\u267b\ufe0f Context compacted. Re-orienting..."


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

    # Use ts_ms=0 so the filename ("0_compact.json") sorts lexicographically
    # before any real user-message filename (which starts with the current epoch
    # in milliseconds, e.g. "1741234567890_...").  This guarantees the
    # compact-reminder is the first message the dispatcher sees after
    # compaction, regardless of how many user messages were queued beforehand.
    ts_ms = 0
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


def write_sentinel() -> None:
    """
    Write the compact-pending sentinel file.

    The post-compact-gate.py PreToolUse hook checks for this file and blocks
    all tool calls until wait_for_messages() is called. This forces the
    dispatcher back into its main loop before doing anything else.

    Silent on any failure — must never crash the hook.
    """
    try:
        SENTINEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL_FILE.touch()
    except Exception:  # noqa: BLE001
        pass


def write_compacted_at() -> None:
    """
    Record the current UTC timestamp as compacted_at in lobster-state.json.

    Preserves the existing 'mode' field (and any other fields) so that the
    health check can still read lifecycle state correctly. Only adds or
    overwrites the compacted_at field.

    Silent on any failure — must never crash the hook.
    """
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state: dict = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        state["compacted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp_path = STATE_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2) + "\n")
        tmp_path.replace(STATE_FILE)  # atomic on Linux (same filesystem)
    except Exception:  # noqa: BLE001
        pass


def _parse_config_env() -> dict:
    """Parse key=value pairs from config.env, ignoring comments and blank lines."""
    config = {}
    if not CONFIG_ENV.exists():
        return config
    try:
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            # Strip optional surrounding quotes from the value.
            value = value.strip().strip('"').strip("'")
            config[key.strip()] = value
    except OSError:
        pass
    return config


def _send_telegram_notify(bot_token: str, chat_id: str, text: str) -> None:
    """
    Send text to chat_id via the Telegram Bot API.
    Logs to stderr on failure so the cause is visible in Claude hook output.
    Never raises — must not crash the hook.
    """
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            if status != 200:
                body = resp.read(500).decode("utf-8", errors="replace")
                print(
                    f"[on-compact] Telegram notify returned HTTP {status}: {body}",
                    file=sys.stderr,
                )
    except urllib.request.HTTPError as e:  # noqa: BLE001
        try:
            body = e.read(500).decode("utf-8", errors="replace")
        except Exception:
            body = "(could not read body)"
        print(
            f"[on-compact] Telegram notify HTTP error {e.code}: {body}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[on-compact] Telegram notify failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def send_compaction_notify() -> None:
    """
    Send a Telegram notification to the owner that a context compaction occurred.

    Always fires when credentials are available — not gated on LOBSTER_DEBUG.
    This is the single canonical notification for a compaction event; the
    health-check suppresses its own alerts during the compaction window so
    exactly one notification reaches the user per compaction.
    """
    config = _parse_config_env()

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_users = config.get("TELEGRAM_ALLOWED_USERS", "").strip()

    if not bot_token or not allowed_users:
        return

    # Take the first user ID from a comma- or space-separated list.
    first_chat_id = allowed_users.replace(",", " ").split()[0]

    _send_telegram_notify(bot_token, first_chat_id, COMPACTION_TELEGRAM_MESSAGE)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}

    # Always record compaction timestamp — runs for both dispatcher and subagent
    # compactions.  The health check reads this to suppress false-positive
    # "stale inbox" restarts during any compaction pause window.
    write_compacted_at()

    # Always send the Telegram notification for any compaction (dispatcher or
    # subagent).  This must fire even for the dispatcher compact, where
    # is_dispatcher() would return False because context compaction assigns a
    # new session_id that no longer matches the stored dispatcher-session-id
    # marker file.  Subagent compactions are rare and the notification is still
    # informational.
    #
    # The health-check suppresses its own Telegram alerts during the compaction
    # window (COMPACTION_SUPPRESS_SECONDS), so exactly one notification reaches
    # the user per compaction event.
    send_compaction_notify()

    # Guard the inbox reminder and sentinel writes to the dispatcher only.
    # Subagent compactions must not inject compact-reminders into the shared
    # inbox or write the compact-pending sentinel, because those signals are
    # only meaningful to the dispatcher.
    if not is_dispatcher(data):
        sys.exit(0)

    if already_pending():
        # Sentinel still needs refreshing even if the inbox reminder is a dupe
        # (double compaction without intervening wait_for_messages). Touch resets
        # the TTL clock so the gate keeps blocking correctly.
        write_sentinel()
        return
    write_sentinel()
    try:
        write_reminder()
    except Exception:  # noqa: BLE001
        pass  # Reminder failure is non-fatal — sentinel is the critical artifact


if __name__ == "__main__":
    main()
