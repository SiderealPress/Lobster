#!/usr/bin/env python3
"""
Context-compaction hook for Lobster.

Fires on SessionStart with a 'compact' event. Injects a system message into
the Lobster inbox so that the next call to wait_for_messages() surfaces a
reminder to re-read CLAUDE.md and re-orient from handoff/memory context.

The script is idempotent: if a compact-reminder message already exists in the
inbox it skips writing a duplicate.

Dev mode: if LOBSTER_DEBUG=true (or set in config.env), also sends a Telegram
message directly to the owner's chat ID so the developer is immediately notified
that a compaction occurred.
"""

import json
import os
import time
import urllib.request
from pathlib import Path


INBOX_DIR = Path(os.path.expanduser("~/messages/inbox"))
CONFIG_ENV = Path(os.path.expanduser("~/lobster-config/config.env"))

REMINDER_TEXT = (
    "Your context was just compacted. STOP. Before processing any messages:\n\n"
    "1. Re-read ~/lobster/CLAUDE.md \u2014 your instructions\n"
    "2. Read ~/lobster-workspace/memory/canonical/handoff.md \u2014 what was happening\n\n"
    "Do not skip either step."
)

DEV_TELEGRAM_MESSAGE = "\u26a0\ufe0f [DEV] Context compacted. Re-orienting from CLAUDE.md + handoff."


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


def _is_debug_mode(config: dict) -> bool:
    """Return True if LOBSTER_DEBUG is 'true' in the environment or config.env."""
    env_val = os.environ.get("LOBSTER_DEBUG", "").lower()
    if env_val == "true":
        return True
    config_val = config.get("LOBSTER_DEBUG", "").lower()
    return config_val == "true"


def _send_telegram_dev_notify(bot_token: str, chat_id: str) -> None:
    """
    Send DEV_TELEGRAM_MESSAGE to chat_id via the Telegram Bot API.
    Silent on any failure — must never crash the hook.
    """
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": DEV_TELEGRAM_MESSAGE}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:  # noqa: BLE001
        pass


def maybe_send_dev_telegram_notify() -> None:
    """
    If LOBSTER_DEBUG is true and credentials are available, send a Telegram
    notification to the owner that a context compaction occurred.
    """
    config = _parse_config_env()

    if not _is_debug_mode(config):
        return

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_users = config.get("TELEGRAM_ALLOWED_USERS", "").strip()

    if not bot_token or not allowed_users:
        return

    # Take the first user ID from a comma- or space-separated list.
    first_chat_id = allowed_users.replace(",", " ").split()[0]

    _send_telegram_dev_notify(bot_token, first_chat_id)


def main() -> None:
    if already_pending():
        return
    write_reminder()
    maybe_send_dev_telegram_notify()


if __name__ == "__main__":
    main()
