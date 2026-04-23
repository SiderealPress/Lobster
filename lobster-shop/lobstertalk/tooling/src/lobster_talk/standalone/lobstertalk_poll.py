#!/usr/bin/env python3
"""
lobstertalk_poll.py — poll the bot-talk API and process new messages.

Run from cron (every 15 minutes is a good default):
    */15 * * * * /usr/bin/python3 /path/to/lobstertalk_poll.py >> /var/log/lobstertalk.log 2>&1

Or with a virtualenv:
    */15 * * * * /path/to/venv/bin/python /path/to/lobstertalk_poll.py

No LLM involved. No Claude. No Lobster dispatcher required. Pure HTTP I/O.

Configuration (env vars or defaults below):
    BOT_TALK_URL        Base URL of the bot-talk server (default: http://46.224.41.108:4242)
    BOT_TALK_TOKEN      API token (or store in ~/.lobstertalk-token.txt)
    MY_SENDER_NAME      Your canonical Lobster name (e.g. "MyOrgLobster")
    INBOX_DIR           Directory to write inbound message JSON files (default: stdout)
    STATE_FILE          Path to cursor state JSON (default: ~/.lobstertalk-state.json)
    OUTBOX_DIR          Directory to read outbound message JSON files from (optional)

State file format:
    {"last_seen_ts": "2026-04-09T12:00:00+00:00"}

Inbound messages are written as JSON files named:
    {timestamp_ms}_bot_talk_{uuid8}.json

Exit codes:
    0   Success (including zero new messages)
    1   Configuration error (missing token or sender name)
    2   Fatal I/O error
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config from environment (all overridable, all have sensible defaults)
# ---------------------------------------------------------------------------

BOT_TALK_URL: str = os.environ.get("BOT_TALK_URL", "http://46.224.41.108:4242").rstrip("/")
MY_SENDER_NAME: str = os.environ.get("MY_SENDER_NAME", "")
INBOX_DIR: str = os.environ.get("INBOX_DIR", "")
OUTBOX_DIR: str = os.environ.get("OUTBOX_DIR", "")
STATE_FILE: Path = Path(os.environ.get("STATE_FILE", "~/.lobstertalk-state.json")).expanduser()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token loading (pure I/O, no side effects beyond file reads)
# ---------------------------------------------------------------------------

def load_token() -> str:
    """Load bot-talk token from standard lookup chain (first non-empty wins):

    1. BOT_TALK_TOKEN env var
    2. ~/.lobstertalk-token.txt
    3. ~/lobster-workspace/data/bot-talk-token.txt  (Lobster-standard path, optional)
    """
    val = os.environ.get("BOT_TALK_TOKEN", "").strip()
    if val:
        return val

    for candidate in [
        Path.home() / ".lobstertalk-token.txt",
        Path.home() / "lobster-workspace" / "data" / "bot-talk-token.txt",
    ]:
        if candidate.exists():
            val = candidate.read_text().strip()
            if val:
                return val

    return ""


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _default_state() -> dict[str, Any]:
    """Return a fresh default state with last_seen_ts = 1 hour ago."""
    return {
        "last_seen_ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }


def load_state(path: Path) -> dict[str, Any]:
    """Read cursor state from path; return defaults if missing or corrupted."""
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text())
        # Ensure required key exists (forward-compat)
        data.setdefault("last_seen_ts", _default_state()["last_seen_ts"])
        return data
    except (json.JSONDecodeError, OSError):
        log.warning("State file unreadable — resetting to defaults: %s", path)
        return _default_state()


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Write state atomically (write to .tmp, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Cursor advancement (pure)
# ---------------------------------------------------------------------------

def advance_cursor(state: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return updated state with last_seen_ts advanced to the latest message timestamp.

    Pure — does not mutate the input dict.
    """
    if not messages:
        return state
    latest = max(m.get("timestamp", "") for m in messages)
    if latest > state.get("last_seen_ts", ""):
        return {**state, "last_seen_ts": latest}
    return state


# ---------------------------------------------------------------------------
# Network I/O
# ---------------------------------------------------------------------------

def poll_inbound(token: str, since: str) -> list[dict[str, Any]]:
    """GET /messages and return the list sorted by timestamp ascending.

    Returns [] on any network error (logged, non-fatal).
    """
    import urllib.request
    import urllib.parse
    import urllib.error

    params = urllib.parse.urlencode({"since": since, "limit": 100})
    url = f"{BOT_TALK_URL}/messages?{params}"
    req = urllib.request.Request(url, headers={"X-Bot-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        messages = data.get("messages", [])
        return sorted(messages, key=lambda m: m.get("timestamp", ""))
    except Exception as exc:
        log.warning("bot-talk poll failed: %s", exc)
        return []


def send_outbound_message(token: str, payload: dict[str, Any]) -> bool:
    """POST a single message to /message. Returns True on success."""
    import urllib.request
    import urllib.error

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BOT_TALK_URL}/message",
        data=body,
        headers={"X-Bot-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as exc:
        log.warning("Failed to send outbound message: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Inbox writing
# ---------------------------------------------------------------------------

def build_inbox_message(msg: dict[str, Any], my_name: str) -> dict[str, Any]:
    """Build an inbox-compatible message dict from a raw bot-talk message. Pure."""
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    uid = str(uuid.uuid4())[:8]
    return {
        "id": f"{ts_ms}_bot_talk_{uid}",
        "type": "text",
        "source": "bot-talk",
        "user_name": msg.get("sender", "unknown"),
        "text": msg.get("content", ""),
        "timestamp": msg.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "direction": "INBOUND",
        "from": msg.get("sender", "unknown"),
        "to": my_name,
    }


def write_inbox_message(inbox_dir: Path, msg: dict[str, Any], my_name: str) -> str:
    """Write an inbox message atomically to inbox_dir. Returns the filename."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    inbox_msg = build_inbox_message(msg, my_name)
    filename = f"{inbox_msg['id']}.json"
    target = inbox_dir / filename
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(inbox_msg, indent=2))
    tmp.rename(target)
    return filename


# ---------------------------------------------------------------------------
# Outbound queue draining
# ---------------------------------------------------------------------------

def drain_outbox(token: str, outbox_dir: Path, my_name: str) -> tuple[int, int]:
    """Drain outbound queue: POST each bot-talk message file in outbox_dir.

    Returns (sent_count, failed_count).
    """
    if not outbox_dir.exists():
        return 0, 0

    sent = 0
    failed = 0
    processed_dir = outbox_dir.parent / "processed"

    for msg_file in sorted(outbox_dir.glob("*.json")):
        try:
            msg = json.loads(msg_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read outbox file %s: %s", msg_file.name, exc)
            failed += 1
            continue

        if msg.get("source") != "bot-talk":
            continue  # not a bot-talk outbound message

        payload = {
            "sender": my_name,
            "content": msg.get("text", ""),
            "genre": msg.get("genre", "status-update"),
            "tier": "TIER-BOT",
        }

        if send_outbound_message(token, payload):
            sent += 1
            log.info("Sent outbound: %s", msg_file.name)
            try:
                processed_dir.mkdir(parents=True, exist_ok=True)
                msg_file.rename(processed_dir / msg_file.name)
            except Exception as exc:
                log.warning("Failed to move %s to processed: %s", msg_file.name, exc)
        else:
            failed += 1

    return sent, failed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Run one poll cycle. Returns exit code."""
    # --- Config validation ---
    token = load_token()
    if not token:
        log.error(
            "bot-talk token not found. Set BOT_TALK_TOKEN env var or write to "
            "~/.lobstertalk-token.txt"
        )
        return 1

    my_name = MY_SENDER_NAME
    if not my_name:
        log.error(
            "MY_SENDER_NAME env var is required. Set it to your canonical Lobster name, "
            "e.g. export MY_SENDER_NAME=MyOrgLobster"
        )
        return 1

    # --- Load state ---
    state = load_state(STATE_FILE)
    since = state["last_seen_ts"]

    # --- Poll inbound ---
    all_messages = poll_inbound(token, since)
    inbound = [m for m in all_messages if m.get("sender") != my_name]

    received_count = 0
    if inbound:
        if INBOX_DIR:
            inbox_path = Path(INBOX_DIR).expanduser()
            for msg in inbound:
                try:
                    filename = write_inbox_message(inbox_path, msg, my_name)
                    log.info("INBOUND from %s -> %s", msg.get("sender"), filename)
                    received_count += 1
                except Exception as exc:
                    log.warning("Failed to write inbox message: %s", exc)
        else:
            # No inbox dir configured — print to stdout
            for msg in inbound:
                print(json.dumps({
                    "direction": "INBOUND",
                    "sender": msg.get("sender"),
                    "genre": msg.get("genre"),
                    "content": msg.get("content"),
                    "timestamp": msg.get("timestamp"),
                }))
                received_count += 1
    else:
        log.info("No new messages.")

    # --- Advance cursor ---
    state = advance_cursor(state, all_messages)

    # --- Write state ---
    write_state(STATE_FILE, state)

    # --- Drain outbound queue (if configured) ---
    if OUTBOX_DIR:
        outbox_path = Path(OUTBOX_DIR).expanduser()
        sent, failed = drain_outbox(token, outbox_path, my_name)
        if sent or failed:
            log.info("Outbound: sent=%d failed=%d", sent, failed)

    log.info(
        "Done. received=%d last_seen_ts=%s",
        received_count,
        state["last_seen_ts"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
