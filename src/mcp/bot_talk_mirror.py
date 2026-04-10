#!/usr/bin/env python3
"""
Bot-talk mirroring module — cross-Lobster channel only.

Bot-talk is strictly inter-Lobster communication: messages exchanged between
Lobster instances (e.g. SaharLobster <-> AlbertLobster). Owner messages sent
from Telegram to their own Lobster are NOT bot-talk and are never logged here.

Public API
----------
mirror_outbound(text, source, chat_id)
    Called when this Lobster sends a reply OUT to the bot-talk channel (to
    another Lobster). Records direction="OUTBOUND" and emits to EventBus.
    Fire-and-forget.

log_inbound_cross_lobster(sender, content)
    Called when a cross-Lobster message arrives FROM another Lobster instance.
    Records direction="INBOUND", emits to EventBus, and routes the message
    to ~/messages/inbox/ with source="bot-talk" so the dispatcher handles it.
    Fire-and-forget.

The old mirror_inbound() function (which incorrectly logged owner Telegram
messages as bot-talk entries) has been removed. Only cross-Lobster messages
should be passed to this module.

Architecture
------------
Every call spawns a short-lived daemon thread that fires once and exits.
The calling path is never blocked — if the mirror fails, the message is still
delivered normally.

Resilience chain
----------------
1. HTTP POST to BOT_TALK_HTTP_URL (3-second timeout, 2 retries)
2. SSH fallback: append a log line to the remote log file via BOT_TALK_SSH_HOST
3. Local log: ~/lobster-workspace/logs/bot-talk-mirror.log

All bot-talk messages (both directions) are also logged to the central
EventBus (logs/events.jsonl) with structured direction/from/to fields.

Configuration (all overridable via environment variables):
  BOT_TALK_HTTP_URL      - Full URL to the bot-talk /message endpoint
  BOT_TALK_SSH_HOST      - SSH host alias for the fallback log write
  BOT_TALK_SSH_LOG_PATH  - Remote log file path on the SSH host
  LOBSTER_NAME           - Identity string for this Lobster instance (required)

IMPORTANT: The bot-talk service runs plain HTTP on port 4242 — there is no
TLS on this endpoint (TLS was intentionally removed).  Always use http://
(never https://) when constructing or configuring BOT_TALK_HTTP_URL.
Using https:// causes an SSL handshake failure on every request.
"""

import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all values overridable via environment variables.
# Falls back to reading config.env (same pattern as inbox_server.py / OPENAI_API_KEY).
# If BOT_TALK_HTTP_URL is empty after all lookups, HTTP mirroring is silently
# disabled (SSH fallback still applies if sharedLobster is reachable).
# ---------------------------------------------------------------------------

def _read_config_env(key: str) -> str:
    """Read a single key from ~/messages/config/config.env or the default config.env.

    Returns the value as a string, or "" if not found.
    """
    _messages = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    search_paths = [
        _messages / "config" / "config.env",
        Path.home() / "messages" / "config" / "config.env",
    ]
    for config_path in search_paths:
        if config_path.exists():
            try:
                for line in config_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith(f"{key}="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return ""


BOT_TALK_HTTP_URL: str = (
    os.environ.get("BOT_TALK_HTTP_URL")
    or _read_config_env("BOT_TALK_HTTP_URL")
    or ""
)
BOT_TALK_SSH_HOST: str = (
    os.environ.get("BOT_TALK_SSH_HOST")
    or _read_config_env("BOT_TALK_SSH_HOST")
    or "sharedLobster"
)
BOT_TALK_SSH_LOG: str = (
    os.environ.get("BOT_TALK_SSH_LOG_PATH")
    or _read_config_env("BOT_TALK_SSH_LOG_PATH")
    or "/home/shared/bot-talk/log.txt"
)
BOT_TALK_TOKEN: str = (
    os.environ.get("BOT_TALK_TOKEN")
    or _read_config_env("BOT_TALK_TOKEN")
    or ""
)
LOBSTER_NAME: str = (
    os.environ.get("LOBSTER_NAME")
    or _read_config_env("LOBSTER_NAME")
    or "MyLobster"
)
BOT_TALK_HTTP_TIMEOUT = 3.0   # seconds
BOT_TALK_HTTP_RETRIES = 2
BOT_TALK_TIER = "TIER-BOT"

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_LOCAL_LOG = _WORKSPACE / "logs" / "bot-talk-mirror.log"
_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_INBOX_DIR = _MESSAGES_DIR / "inbox"


# ---------------------------------------------------------------------------
# Pure builder functions (no I/O side effects)
# ---------------------------------------------------------------------------

def _build_http_payload(content: str, genre: str, direction: str, from_: str, to: str) -> dict:
    """Build the POST body for the bot-talk HTTP server.

    Returns a plain dict; no I/O performed.
    """
    return {
        "sender": LOBSTER_NAME,
        "tier": BOT_TALK_TIER,
        "genre": genre,
        "content": content,
        "direction": direction,
        "from": from_,
        "to": to,
    }


def _build_ssh_log_line(content: str, genre: str, direction: str = "") -> str:
    """Build the log line for the SSH fallback.

    Returns a plain string; no I/O performed.
    """
    ts = datetime.now(timezone.utc).isoformat()
    short = content[:200].replace("\n", " ")
    direction_tag = f" [{direction}]" if direction else ""
    return f"[{ts}] [{LOBSTER_NAME}] [{BOT_TALK_TIER}] [{genre}]{direction_tag} {short}"


def _build_auth_headers() -> dict:
    """Build HTTP headers including X-Bot-Token if configured.

    Returns a plain dict; no I/O performed.
    """
    headers: dict = {}
    if BOT_TALK_TOKEN:
        headers["X-Bot-Token"] = BOT_TALK_TOKEN
    return headers


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _try_http(payload: dict) -> bool:
    """Attempt to POST payload to the bot-talk HTTP server.

    Returns True on success, False on any failure (including empty URL).
    Pure in the sense that it has no state — each call is independent.
    """
    if not BOT_TALK_HTTP_URL:
        return False
    headers = _build_auth_headers()
    for attempt in range(BOT_TALK_HTTP_RETRIES + 1):
        try:
            with httpx.Client(timeout=BOT_TALK_HTTP_TIMEOUT) as client:
                resp = client.post(BOT_TALK_HTTP_URL, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    return True
                log.debug(f"bot-talk HTTP returned {resp.status_code} (attempt {attempt + 1})")
        except Exception as exc:
            log.debug(f"bot-talk HTTP failed (attempt {attempt + 1}): {exc}")
        if attempt < BOT_TALK_HTTP_RETRIES:
            time.sleep(0.5)
    return False


def _try_ssh(log_line: str) -> bool:
    """Attempt to append log_line to the remote log.txt via SSH.

    Returns True on success, False on any failure.
    """
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             BOT_TALK_SSH_HOST,
             f"echo {shlex.quote(log_line)} >> {BOT_TALK_SSH_LOG}"],
            timeout=10,
            capture_output=True,
        )
        return result.returncode == 0
    except Exception as exc:
        log.debug(f"bot-talk SSH fallback failed: {exc}")
        return False


def _write_local_log(content: str, genre: str, reason: str) -> None:
    """Write a local fallback log entry when both HTTP and SSH fail."""
    try:
        _LOCAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp": ts,
            "sender": LOBSTER_NAME,
            "genre": genre,
            "content": content[:500],
            "mirror_failed_reason": reason,
        }
        with _LOCAL_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # if even local logging fails, stay silent


def _emit_event_bus(direction: str, from_: str, to: str, content: str) -> None:
    """Emit a bot-talk event to the central EventBus (logs/events.jsonl).

    Non-blocking: swallows all exceptions so a bus failure never affects
    the calling path. The event carries structured direction/from/to fields
    so log queries need no content-marker heuristics.
    """
    try:
        from event_bus import get_event_bus, LobsterEvent  # type: ignore[import]
        event = LobsterEvent(
            event_type="bot_talk.message",
            severity="debug",
            source="bot_talk_mirror",
            payload={
                "direction": direction,
                "from": from_,
                "to": to,
                "content": content[:500],
            },
        )
        get_event_bus().emit_sync(event)
    except Exception as exc:
        log.debug(f"bot-talk EventBus emit failed: {exc}")


def _route_to_inbox(from_: str, content: str) -> None:
    """Write an inbound cross-Lobster message to ~/messages/inbox/.

    The message is written with source="bot-talk" so the dispatcher picks
    it up and processes it like any other message. Swallows all exceptions
    so inbox routing failures are non-fatal.
    """
    try:
        _INBOX_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
        msg_id = f"{ts_ms}_bot_talk_{uuid.uuid4().hex[:8]}"
        message = {
            "id": msg_id,
            "type": "text",
            "source": "bot-talk",
            "chat_id": from_,
            "user_name": from_,
            "text": content,
            "timestamp": now.isoformat(),
            "direction": "INBOUND",
            "from": from_,
            "to": LOBSTER_NAME,
        }
        inbox_file = _INBOX_DIR / f"{msg_id}.json"
        # Atomic write: write to tmp then rename to prevent partial reads
        tmp_file = inbox_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(message), encoding="utf-8")
        tmp_file.rename(inbox_file)
        log.debug(f"bot-talk: routed inbound message from {from_!r} to inbox ({msg_id})")
    except Exception as exc:
        log.warning(f"bot-talk: failed to route inbound message to inbox: {exc}")


def _do_mirror(content: str, genre: str, direction: str, from_: str, to: str) -> None:
    """Execute the mirror chain: HTTP -> SSH -> local log, then emit to EventBus.

    Designed to run in a daemon thread. Never raises.
    """
    payload = _build_http_payload(content, genre, direction, from_, to)
    if _try_http(payload):
        log.debug(f"bot-talk mirror: HTTP ok ({genre}, {direction})")
    else:
        log_line = _build_ssh_log_line(content, genre, direction)
        if _try_ssh(log_line):
            log.debug(f"bot-talk mirror: SSH fallback ok ({genre}, {direction})")
        else:
            _write_local_log(content, genre, "http_and_ssh_both_failed")
            log.debug(f"bot-talk mirror: both HTTP and SSH failed, wrote local log ({genre}, {direction})")

    # Always emit to EventBus regardless of HTTP/SSH result
    _emit_event_bus(direction, from_, to, content)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def mirror_outbound(text: str, source: str, chat_id: str | int) -> None:
    """Log an outbound cross-Lobster message to bot-talk.

    This should only be called for messages sent TO another Lobster instance
    via the bot-talk channel. Regular Telegram/Slack replies to the owner
    are NOT bot-talk and must not be passed here.

    Records direction="OUTBOUND" with from=this_lobster, to=destination.
    Emits to EventBus. Fire-and-forget (daemon thread).

    Args:
        text:    The message text sent to the remote Lobster.
        source:  Channel source identifier (e.g. "bot-talk").
        chat_id: Destination identifier (remote Lobster name or channel).
    """
    destination = str(chat_id)
    _spawn_mirror(
        content=text,
        genre="status-update",
        direction="OUTBOUND",
        from_=LOBSTER_NAME,
        to=destination,
    )


def log_inbound_cross_lobster(sender: str, content: str) -> None:
    """Log and route an inbound cross-Lobster message.

    Called when THIS Lobster receives a message FROM another Lobster instance.
    Two things happen (both fire-and-forget):
      1. The message is mirrored to the bot-talk HTTP endpoint (with direction,
         from, to fields) and emitted to the central EventBus.
      2. The message is routed to ~/messages/inbox/ with source="bot-talk" so
         the dispatcher picks it up and processes it normally.

    Fire-and-forget: spawns a daemon thread and returns immediately.

    Args:
        sender:  Identity of the remote Lobster (e.g. "AlbertLobster").
        content: The message content received.
    """
    # Mirror + EventBus in background thread
    _spawn_mirror(
        content=content,
        genre="status-update",
        direction="INBOUND",
        from_=sender,
        to=LOBSTER_NAME,
    )
    # Route to inbox — fast file write, done inline before returning
    _route_to_inbox(sender, content)


def _spawn_mirror(content: str, genre: str, direction: str, from_: str, to: str) -> None:
    """Spawn a daemon thread to run _do_mirror.

    Using daemon=True means the thread won't prevent process exit.
    """
    t = threading.Thread(
        target=_do_mirror,
        args=(content, genre, direction, from_, to),
        daemon=True,
    )
    t.start()
