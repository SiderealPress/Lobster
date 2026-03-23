#!/usr/bin/env python3
"""
Bot-talk mirroring module.

Mirrors Lobster's inbound and outbound messages to the shared bot-talk channel
so Albert's Lobster can observe what Sahar's Lobster is doing.

Architecture
------------
Every call to `mirror_outbound` or `mirror_inbound` spawns a short-lived daemon
thread that fires once and exits.  The calling path (handle_send_reply,
handle_check_inbox) is never blocked — if the mirror fails, the message is still
delivered normally.

Resilience chain
----------------
1. POST http://46.224.41.108:4242/message  (3-second timeout, 2 retries)
2. SSH fallback: append a log line to /home/shared/bot-talk/log.txt on sharedLobster
3. Local log: ~/lobster-workspace/logs/bot-talk-mirror.log

Anti-duplication
----------------
The bot-talk poller reads only messages with sender="AlbertLobster".
This module writes sender="SaharLobster", so there is no echo loop.

Filtering
---------
Only real user messages and outbound replies reach bot-talk.
Internal system subtypes (self_check, subagent_*, compact_*, scheduler_*) are
excluded so Albert's Lobster isn't spammed with Lobster-internal chatter.
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TALK_HTTP_URL = "http://46.224.41.108:4242/message"
BOT_TALK_SSH_HOST = "sharedLobster"
BOT_TALK_SSH_LOG = "/home/shared/bot-talk/log.txt"
BOT_TALK_HTTP_TIMEOUT = 3.0   # seconds
BOT_TALK_HTTP_RETRIES = 2
BOT_TALK_SENDER = "SaharLobster"
BOT_TALK_TIER = "TIER-BOT"

_WORKSPACE = Path.home() / "lobster-workspace"
_LOCAL_LOG = _WORKSPACE / "logs" / "bot-talk-mirror.log"

# Subtypes that should NOT be mirrored — Lobster-internal system messages
_EXCLUDED_SUBTYPES = frozenset({
    "self_check",
    "compact-reminder",
    "compact_catchup",
    "subagent_notification",
    "subagent_observation",
    "subagent_recovered",
    "scheduler_tick",
})

# Message types that carry real user content worth mirroring
_MIRROR_INBOUND_TYPES = frozenset({
    "text",
    "voice",
    "photo",
    "document",
})


# ---------------------------------------------------------------------------
# Core mirror function (pure: no I/O side effects, takes a pre-built payload)
# ---------------------------------------------------------------------------

def _build_http_payload(content: str, genre: str) -> dict:
    """Build the POST body for the bot-talk HTTP server.

    Returns a plain dict; no I/O performed.
    """
    return {
        "sender": BOT_TALK_SENDER,
        "tier": BOT_TALK_TIER,
        "genre": genre,
        "content": content,
    }


def _build_ssh_log_line(content: str, genre: str) -> str:
    """Build the log line for the SSH fallback.

    Returns a plain string; no I/O performed.
    """
    ts = datetime.now(timezone.utc).isoformat()
    short = content[:200].replace("\n", " ")
    return f"[{ts}] [{BOT_TALK_SENDER}] [{BOT_TALK_TIER}] [{genre}] {short}"


def _try_http(payload: dict) -> bool:
    """Attempt to POST payload to the bot-talk HTTP server.

    Returns True on success, False on any failure.
    Pure in the sense that it has no state — each call is independent.
    """
    for attempt in range(BOT_TALK_HTTP_RETRIES + 1):
        try:
            with httpx.Client(timeout=BOT_TALK_HTTP_TIMEOUT) as client:
                resp = client.post(BOT_TALK_HTTP_URL, json=payload)
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
             f"echo {json.dumps(log_line)} >> {BOT_TALK_SSH_LOG}"],
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
            "sender": BOT_TALK_SENDER,
            "genre": genre,
            "content": content[:500],
            "mirror_failed_reason": reason,
        }
        with _LOCAL_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # if even local logging fails, stay silent


def _do_mirror(content: str, genre: str) -> None:
    """Execute the mirror chain: HTTP → SSH → local log.

    Designed to run in a daemon thread. Never raises.
    """
    payload = _build_http_payload(content, genre)
    if _try_http(payload):
        log.debug(f"bot-talk mirror: HTTP ok ({genre})")
        return

    log_line = _build_ssh_log_line(content, genre)
    if _try_ssh(log_line):
        log.debug(f"bot-talk mirror: SSH fallback ok ({genre})")
        return

    _write_local_log(content, genre, "http_and_ssh_both_failed")
    log.debug(f"bot-talk mirror: both HTTP and SSH failed, wrote local log ({genre})")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def mirror_outbound(text: str, source: str, chat_id: str | int) -> None:
    """Mirror an outbound send_reply to bot-talk.

    Fire-and-forget: spawns a daemon thread and returns immediately.
    Safe to call from any async or sync context.

    Args:
        text:    The reply text that was sent.
        source:  Channel source (telegram, slack, etc.)
        chat_id: Destination chat ID.
    """
    content = f"[OUTBOUND → {source.upper()} chat={chat_id}] {text}"
    _spawn_mirror(content, genre="status-update")


def mirror_inbound(msg: dict) -> None:
    """Mirror a real inbound user message to bot-talk.

    Filters out system/internal message types — only real user messages
    (text, voice, photo, document) are mirrored.

    Fire-and-forget: spawns a daemon thread and returns immediately.

    Args:
        msg: The raw message dict from the inbox JSON file.
    """
    msg_type = msg.get("type", "text")
    subtype = msg.get("subtype", "")

    # Skip internal / system messages
    if subtype in _EXCLUDED_SUBTYPES:
        return
    if msg_type not in _MIRROR_INBOUND_TYPES:
        return

    source = msg.get("source", "unknown").upper()
    user = msg.get("user_name") or msg.get("username") or "unknown"
    text = msg.get("text", "(no text)")

    if msg_type == "voice":
        content = f"[INBOUND from {source}] {user}: (voice message)"
    elif msg_type == "photo":
        content = f"[INBOUND from {source}] {user}: (photo message)"
    elif msg_type == "document":
        fname = msg.get("file_name", "file")
        content = f"[INBOUND from {source}] {user}: (document: {fname})"
    else:
        content = f"[INBOUND from {source}] {user}: {text}"

    _spawn_mirror(content, genre="status-update")


def _spawn_mirror(content: str, genre: str) -> None:
    """Spawn a daemon thread to run _do_mirror.

    Using daemon=True means the thread won't prevent process exit.
    """
    t = threading.Thread(target=_do_mirror, args=(content, genre), daemon=True)
    t.start()
