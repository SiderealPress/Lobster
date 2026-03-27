#!/usr/bin/env python3
"""
Bot-talk mirroring module.

Mirrors Lobster's inbound and outbound messages to the shared bot-talk channel
so Albert's Lobster can observe what the owner's Lobster is doing.

Architecture
------------
Every call to `mirror_outbound` or `mirror_inbound` spawns a short-lived daemon
thread that fires once and exits.  The calling path (handle_send_reply,
handle_check_inbox) is never blocked — if the mirror fails, the message is still
delivered normally.

Resilience chain
----------------
1. HTTP POST to BOT_TALK_HTTP_URL (3-second timeout, 2 retries)
2. SSH fallback: append a log line to the remote log file via BOT_TALK_SSH_HOST
3. Local log: ~/lobster-workspace/logs/bot-talk-mirror.log

Configuration (all overridable via environment variables):
  BOT_TALK_HTTP_URL      - Full URL to the bot-talk /message endpoint
  BOT_TALK_SSH_HOST      - SSH host alias for the fallback log write
  BOT_TALK_SSH_LOG_PATH  - Remote log file path on the SSH host

Anti-duplication
----------------
The bot-talk poller reads only messages with sender="AlbertLobster".
This module writes sender="SaharLobster", so there is no echo loop.

Filtering
---------
Only real user messages and outbound replies reach bot-talk.
Internal system subtypes (self_check, subagent_*, compact-reminder, compact_catchup, scheduler_*) are
excluded so Albert's Lobster isn't spammed with Lobster-internal chatter.
"""

import json
import logging
import os
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all values overridable via environment variables.
# Falls back to reading config.env (same pattern as inbox_server.py / OPENAI_API_KEY).
# If BOT_TALK_HTTP_URL is empty after all lookups, HTTP mirroring is silently
# disabled (SSH fallback still applies if sharedLobster is reachable).
#
# IMPORTANT: The bot-talk service runs plain HTTP on port 4242 — there is no
# TLS on this endpoint (TLS was intentionally removed).  Always use http://
# (never https://) when constructing or configuring BOT_TALK_HTTP_URL.
# Using https:// causes an SSL handshake failure on every request.
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


def _build_auth_headers() -> dict:
    """Build HTTP headers including X-Bot-Token if configured.

    Returns a plain dict; no I/O performed.
    """
    headers: dict = {}
    if BOT_TALK_TOKEN:
        headers["X-Bot-Token"] = BOT_TALK_TOKEN
    return headers


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
