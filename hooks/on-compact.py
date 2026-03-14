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

State: always writes compacted_at to lobster-state.json so that the health
check can suppress stale-inbox false-positives during the compaction pause.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path


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
    "1. ~/lobster-workspace/.claude/dispatcher.bootup.md\n"
    "  \u2190 dispatcher instructions, main loop, 7-second rule\n"
    "2. ~/lobster-user-config/memory/canonical/handoff.md\n"
    "  \u2190 active projects, key people, priorities\n\n"
    "After reading: resume your main loop by calling wait_for_messages()."
)

SENTINEL_FILE = Path(os.path.expanduser("~/messages/config/compact-pending"))

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
    Logs to stderr on failure so the cause is visible in Claude hook output.
    Never raises — must not crash the hook.
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
    # Always record compaction timestamp first — before any early returns.
    # This ensures the health check can suppress false-positive restarts even
    # if a compact-reminder is already pending.
    write_compacted_at()

    if already_pending():
        # Sentinel still needs refreshing even if the inbox reminder is a dupe
        # (double compaction without intervening wait_for_messages). Touch resets
        # the TTL clock so the gate keeps blocking correctly.
        write_sentinel()
        maybe_send_dev_telegram_notify()
        return
    write_sentinel()
    try:
        write_reminder()
    except Exception:  # noqa: BLE001
        pass  # Reminder failure is non-fatal — sentinel is the critical artifact
    maybe_send_dev_telegram_notify()


if __name__ == "__main__":
    main()
