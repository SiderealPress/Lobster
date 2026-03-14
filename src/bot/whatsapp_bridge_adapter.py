#!/usr/bin/env python3
"""
Lobster WhatsApp Bridge Adapter - BIS-47 / BIS-48

Bridges the whatsapp-web.js bridge output into Lobster's file-based inbox/outbox system.

Architecture:
    wa-events/  <-- JSON files written by Node.js bridge (one file per message event)
        |
        v
    normalize()  <-- convert to Lobster's standard inbox schema
        |
        v
    ~/messages/inbox/   <-- Lobster reads these via check_inbox()
        |
    Lobster processes, calls send_reply()
        |
        v
    ~/messages/outbox/  <-- reply JSON files (source="whatsapp")
        |
        v
    wa-commands/  <-- JSON command files written here for the Node bridge to pick up
        |
    Node bridge sends reply via WhatsApp

Replaces: whatsapp_router.py (Twilio-based, kept for backward compatibility)

BIS-48 additions:
  - is_routable() enforces @mention gate: group messages only route when
    mentions_lobster is True; DMs always route.
  - normalize_event() passes through bridge's authoritative mentions_lobster
    field and resolves group_name from both 'chatName' (connectors/whatsapp)
    and 'group_name' (whatsapp-bridge) field names.

Environment variables:
    LOBSTER_MESSAGES      - Base messages directory (default: ~/messages)
    LOBSTER_WORKSPACE     - Workspace directory (default: ~/lobster-workspace)
    WA_EVENTS_DIR         - Incoming event files from bridge (default: ~/messages/wa-events)
    WA_COMMANDS_DIR       - Outgoing command files for bridge (default: ~/messages/wa-commands)
    WHATSAPP_LOBSTER_JID  - Lobster's own WhatsApp JID (e.g. 15551234567@c.us)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages")))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", str(_HOME / "lobster-workspace")))

INBOX_DIR = _MESSAGES / "inbox"
OUTBOX_DIR = _MESSAGES / "outbox"
EVENTS_DIR = Path(os.environ.get("WA_EVENTS_DIR", str(_MESSAGES / "wa-events")))
COMMANDS_DIR = Path(os.environ.get("WA_COMMANDS_DIR", str(_MESSAGES / "wa-commands")))
# Alias used by tests and external code
WA_COMMANDS_DIR = COMMANDS_DIR

# Lobster's own WhatsApp JID for mention detection
LOBSTER_JID = os.environ.get("WHATSAPP_LOBSTER_JID", "")

for _d in (INBOX_DIR, OUTBOX_DIR, EVENTS_DIR, COMMANDS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = _WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("lobster-whatsapp-adapter")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_DIR / "whatsapp-adapter.log")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
log.addHandler(logging.StreamHandler())

# Group name cache
_group_name_cache: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Pure functions (exported / testable)
# ---------------------------------------------------------------------------


def is_system_event(event: dict) -> bool:
    """Return True if this is a system/control event from the bridge."""
    return event.get("type") == "system"


def is_routable(event: dict, lobster_jid: str = "") -> bool:
    """
    Determine whether this bridge event should be written to Lobster's inbox.

    Rules:
    - fromMe messages are never routed
    - System events are always routed (bridge notifications to Drew)
    - DMs (non-group) are always routed
    - Group messages are routed only if mentions_lobster is True
    """
    if event.get("fromMe"):
        return False
    if is_system_event(event):
        return True
    if not event.get("isGroup"):
        return True
    return bool(event.get("mentions_lobster"))


def normalize_event(event: dict) -> dict | None:
    """
    Convert a whatsapp-web.js bridge event to Lobster's standard inbox schema.

    Returns None if the event should be dropped (fromMe=True or missing id).

    Bridge fields: id, body, from, fromMe, isGroup, author, timestamp,
                   mentionedIds, mentions_lobster, chatName
    Inbox fields:  id, source, chat_id, user_id, user_name, text, is_group,
                   group_name, mentions_lobster, timestamp
    """
    # Drop outgoing messages and events with no bridge id
    if event.get("fromMe"):
        return None
    if not event.get("id"):
        return None

    msg_id = f"{int(time.time() * 1000)}_wa_{event['id']}"
    chat_id = event.get("from", "")
    is_group = bool(event.get("isGroup"))

    # In groups, 'author' is the sender's JID; 'from' is the group JID
    user_id = event.get("author") or event.get("from") or ""
    # Derive readable name from JID (real names require extra API call)
    user_name = user_id.split("@")[0] if "@" in user_id else user_id

    # Cache/retrieve group names.
    # Accept both field names: 'chatName' (connectors/whatsapp/index.js) and
    # 'group_name' (whatsapp-bridge/index.js - BIS-48) for cross-compatibility.
    # Fall back to the group JID (from field) when no name is available.
    chat_name = event.get("chatName") or event.get("group_name") or ""
    if is_group and chat_name:
        _group_name_cache[chat_id] = chat_name
    elif is_group and chat_id in _group_name_cache:
        chat_name = _group_name_cache[chat_id]
    elif is_group and not chat_name:
        # Use the group JID as a fallback name when nothing else is available
        chat_name = chat_id

    # Convert Unix timestamp to ISO 8601
    raw_ts = event.get("timestamp")
    if raw_ts:
        try:
            ts = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            ts = datetime.now(tz=timezone.utc).isoformat()
    else:
        ts = datetime.now(tz=timezone.utc).isoformat()

    # mentions_lobster: use explicit bridge field if present; otherwise fall back
    # to non-empty mentionedIds (any @mention in the message counts as a mention)
    mentioned_ids = event.get("mentionedIds") or []
    mentions_lobster = bool(event.get("mentions_lobster")) or bool(mentioned_ids)

    return {
        "id": msg_id,
        "source": "whatsapp",
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "text": event.get("body") or "",
        "is_group": is_group,
        "group_name": chat_name,
        "mentions_lobster": mentions_lobster,
        "timestamp": ts,
    }


def normalize_system_event(event: dict) -> dict:
    """Normalize a bridge system event (e.g. session_expired) into an inbox message."""
    return {
        "id": f"{int(time.time() * 1000)}_wa_sys",
        "source": "whatsapp",
        "type": "system",
        "subtype": event.get("subtype", ""),
        "chat_id": "system",
        "user_id": "system",
        "user_name": "WhatsApp Bridge",
        "text": event.get("body") or "[WhatsApp system event]",
        "is_group": False,
        "group_name": "",
        "mentions_lobster": False,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


def build_wa_command(to: str, text: str) -> dict:
    """Build a bridge send-command payload."""
    return {"action": "send", "to": to, "text": text}


def outbox_reply_to_command(reply: dict) -> dict | None:
    """
    Convert a Lobster outbox reply with source='whatsapp' into a bridge command dict.

    Returns None if the reply is not a WhatsApp reply or has missing fields.
    """
    if reply.get("source", "").lower() != "whatsapp":
        return None
    chat_id = reply.get("chat_id", "")
    text = reply.get("text", "")
    if not chat_id or not text:
        return None
    if chat_id in ("system", ""):
        return None
    return build_wa_command(chat_id, text)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via a temp file rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(path)


def process_event_file(file_path: Path) -> None:
    """Read one bridge event file, normalize it, write to inbox, then delete."""
    try:
        raw = file_path.read_text(encoding="utf-8")
        event = json.loads(raw)
    except Exception as exc:
        log.error("Failed to read event file %s: %s", file_path, exc)
        return

    try:
        if not is_routable(event, LOBSTER_JID):
            log.debug("Skipping non-routable event from %s", event.get("from"))
        else:
            inbox_msg = normalize_system_event(event) if is_system_event(event) else normalize_event(event)
            if inbox_msg is None:
                log.debug("normalize_event returned None for event from %s", event.get("from"))
            else:
                inbox_path = INBOX_DIR / f"{inbox_msg['id']}.json"
                _atomic_write(inbox_path, inbox_msg)
                log.info(
                    "Inbox: %s | from=%s group=%s mentions=%s",
                    inbox_msg["id"],
                    inbox_msg.get("user_id", "?"),
                    inbox_msg.get("is_group"),
                    inbox_msg.get("mentions_lobster"),
                )
    except Exception as exc:
        log.error("Failed to process event %s: %s", file_path, exc)

    try:
        file_path.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("Could not delete event file %s: %s", file_path, exc)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class EventsDirHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix == ".json":
            time.sleep(0.05)
            Thread(target=process_event_file, args=(path,), daemon=True).start()


class WhatsAppOutboxHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(".json"):
            Thread(target=self._process, args=(event.src_path,), daemon=True).start()

    def _process(self, filepath: str) -> None:
        try:
            time.sleep(0.1)
            data = json.loads(Path(filepath).read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not read outbox file %s: %s", filepath, exc)
            return

        cmd = outbox_reply_to_command(data)
        if cmd is None:
            return

        ts_ms = int(time.time() * 1000)
        cmd_path = WA_COMMANDS_DIR / f"{ts_ms}_wa_cmd.json"
        try:
            _atomic_write(cmd_path, cmd)
            log.info("Command written: send to %s - %s", cmd["to"], cmd["text"][:50])
        except Exception as exc:
            log.error("Failed to write command file: %s", exc)

        try:
            Path(filepath).unlink(missing_ok=True)
        except Exception as exc:
            log.warning("Could not remove outbox file %s: %s", filepath, exc)


# Backward-compatible alias
OutboxHandler = WhatsAppOutboxHandler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Starting Lobster WhatsApp Bridge Adapter")
    log.info("Events dir:   %s", EVENTS_DIR)
    log.info("Commands dir: %s", COMMANDS_DIR)
    log.info("Lobster JID:  %s", LOBSTER_JID or "(not set)")

    # Process files left over from previous session
    for f in sorted(EVENTS_DIR.glob("*.json")):
        process_event_file(f)
    for f in sorted(OUTBOX_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("source", "").lower() == "whatsapp":
                WhatsAppOutboxHandler()._process(str(f))
        except Exception as exc:
            log.warning("Error in startup outbox sweep: %s", exc)

    observer = Observer()
    observer.schedule(EventsDirHandler(), str(EVENTS_DIR), recursive=False)
    observer.schedule(WhatsAppOutboxHandler(), str(OUTBOX_DIR), recursive=False)
    observer.start()
    log.info("Adapter running - watching %s and %s", EVENTS_DIR, OUTBOX_DIR)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down adapter...")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
