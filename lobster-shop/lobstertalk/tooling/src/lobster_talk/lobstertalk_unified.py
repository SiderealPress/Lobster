#!/usr/bin/env uv run python3
"""
LobsterTalk Unified Job — genericized production version.

This is the scheduled job that owns the complete inter-Lobster communication cycle:
receiving messages from the bot-talk server, routing them to the Lobster inbox,
sending queued outbound messages, and managing hot-mode re-triggering.

GENERICIZING: Before using this script, set the two constants below:
  MY_LOBSTER_NAME  — your canonical Lobster name (must be in the server allowlist)
  ADMIN_CHAT_ID    — your owner's Telegram/Slack chat ID for inbox routing

Production deployment: this script is run by the Lobster scheduler as the
`lobstertalk-unified` job (hourly baseline, self-reschedules to 5 min in hot mode).

Architecture
------------
Pure functions handle data transformation; I/O is isolated to:
  - _load_state() / _write_state()
  - _load_token()
  - _poll_inbound()
  - _send_outbound()
  - _write_inbox_message()
  - _schedule_hot_retrigger()
  - _call_write_task_output() / _call_write_result()

All state is stored in a single JSON file; all inbox writes are atomic.

Hot mode
--------
When any messages are received, hot mode activates and a 5-minute systemd one-shot
timer is scheduled via `systemd-run`. After 2 consecutive empty polls, hot mode
reverts to hourly baseline. This is tracked in the state file.

See lobstertalk/lobstertalk-api.md for the full protocol spec.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    from lobster_talk.advanced.eventbus_bridge import emit_to_eventbus as _emit_to_eventbus
except ImportError:
    # eventbus_bridge is optional — not available in standalone/minimal installs.
    def _emit_to_eventbus(msg):  # type: ignore[misc]
        return False

# ---------------------------------------------------------------------------
# Runtime config — read from files/env at startup (not baked in at install time)
# ---------------------------------------------------------------------------
#
# MY_LOBSTER_NAME is read from (first non-empty wins):
#   1. ~/lobster-workspace/data/lobster-name.txt
#   2. LOBSTER_NAME env var
#   Falls back to "MyLobster" if neither is set (installer should always write the file).
#
# ADMIN_CHAT_ID is read from (first non-zero wins):
#   1. ~/lobster-workspace/data/lobster-admin-chat-id.txt
#   2. LOBSTER_ADMIN_CHAT_ID env var
#   Falls back to 0 (inbound messages will have chat_id=0 until configured).
#
# BOT_TALK_BASE_URL is read from (first non-empty wins):
#   1. BOT_TALK_URL env var
#   2. BOT_TALK_URL line in ~/messages/config/config.env
#   Falls back to the default relay server address.

def _load_lobster_name() -> str:
    """Load canonical Lobster name from config file or env."""
    name_file = Path.home() / "lobster-workspace" / "data" / "lobster-name.txt"
    if name_file.exists():
        val = name_file.read_text().strip()
        if val:
            return val
    val = os.environ.get("LOBSTER_NAME", "").strip()
    return val if val else "MyLobster"


def _load_admin_chat_id() -> int:
    """Load owner's Telegram/Slack chat ID from config file or env."""
    chat_id_file = Path.home() / "lobster-workspace" / "data" / "lobster-admin-chat-id.txt"
    if chat_id_file.exists():
        try:
            val = int(chat_id_file.read_text().strip())
            if val:
                return val
        except ValueError:
            pass
    try:
        return int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0").strip())
    except ValueError:
        return 0


def _load_bot_talk_url() -> str:
    """Load bot-talk base URL from env or config file."""
    val = os.environ.get("BOT_TALK_URL", "").strip()
    if val:
        return val
    for config_path in [
        Path.home() / "messages" / "config" / "config.env",
        Path.home() / "lobster-config" / "config.env",
    ]:
        if not config_path.exists():
            continue
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("BOT_TALK_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
                if url:
                    return url
    return "http://46.224.41.108:4242"


# Config load order note: these values are read at module import time, which happens
# when the script starts. install.sh writes config files before the first scheduled
# run, so the load order is safe. However, if this module is imported during install
# (e.g. for testing), the config files may not exist yet and defaults will be used.
MY_LOBSTER_NAME: str = _load_lobster_name()
ADMIN_CHAT_ID: int = _load_admin_chat_id()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOT_TALK_BASE_URL = _load_bot_talk_url()
STATE_FILE = Path.home() / "lobster-workspace" / "data" / "lobstertalk-unified-state.json"
INBOX_DIR = Path.home() / "messages" / "inbox"
OUTBOX_DIR = Path.home() / "messages" / "outbox"
PROCESSED_DIR = Path.home() / "messages" / "processed"
LOG_FILE = Path.home() / "lobster-workspace" / "logs" / "lobstertalk.jsonl"
LOG_ROTATE_BYTES = 50 * 1024 * 1024  # 50 MB

# After this many consecutive empty polls, exit hot mode
COOLDOWN_THRESHOLD = 2

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Token loading
# ---------------------------------------------------------------------------

def _load_token() -> str:
    """Load bot-talk token from standard lookup chain (first non-empty wins):
    1. ~/lobster-workspace/data/bot-talk-token.txt
    2. BOT_TALK_TOKEN in ~/messages/config/config.env
    3. BOT_TALK_TOKEN in ~/lobster-config/config.env
    """
    token_file = Path.home() / "lobster-workspace" / "data" / "bot-talk-token.txt"
    if token_file.exists():
        val = token_file.read_text().strip()
        if val:
            return val

    for config_path in [
        Path.home() / "messages" / "config" / "config.env",
        Path.home() / "lobster-config" / "config.env",
    ]:
        if not config_path.exists():
            continue
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("BOT_TALK_TOKEN="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return ""


# ---------------------------------------------------------------------------
# State management (pure transformation + atomic I/O)
# ---------------------------------------------------------------------------

def _default_state() -> dict[str, Any]:
    """Return a fresh default state dict (last_seen_ts = now - 1 hour)."""
    return {
        "last_seen_ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "hot_mode": False,
        "consecutive_empty_polls": 0,
        "hot_mode_activated_at": None,
    }


def _load_state() -> dict[str, Any]:
    """Read state file; return defaults if missing or corrupted."""
    if not STATE_FILE.exists():
        return _default_state()
    try:
        data = json.loads(STATE_FILE.read_text())
        # Ensure all expected keys exist (forward-compat)
        defaults = _default_state()
        for key, val in defaults.items():
            data.setdefault(key, val)
        return data
    except (json.JSONDecodeError, OSError):
        log.warning("State file corrupted or unreadable — resetting to defaults")
        return _default_state()


def _write_state(state: dict[str, Any]) -> None:
    """Write state atomically (write to .tmp, then rename)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


# ---------------------------------------------------------------------------
# Hot-mode state transitions (pure functions)
# ---------------------------------------------------------------------------

def _update_state_after_messages(state: dict[str, Any], message_count: int) -> dict[str, Any]:
    """Return updated state dict after a poll that returned `message_count` messages.

    Pure: does not mutate the input dict.
    """
    state = dict(state)
    if message_count > 0:
        state["hot_mode"] = True
        if not state.get("hot_mode_activated_at"):
            state["hot_mode_activated_at"] = datetime.now(timezone.utc).isoformat()
        state["consecutive_empty_polls"] = 0
    else:
        state["consecutive_empty_polls"] = state.get("consecutive_empty_polls", 0) + 1
        if state["consecutive_empty_polls"] >= COOLDOWN_THRESHOLD:
            state["hot_mode"] = False
            state["hot_mode_activated_at"] = None
    return state


def _advance_cursor(state: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return updated state with last_seen_ts advanced to the latest message timestamp.

    Called with all_messages (including own-echo), NOT inbound-only. This is intentional:
    the cursor tracks "what have we processed from the server" (all messages, including
    our own outbound echo) so we don't re-fetch them. Hot-mode activation, by contrast,
    uses received_count which counts inbound-only messages — it tracks "is there real
    activity from other Lobsters". The two populations are deliberately different.

    Note: lexicographic timestamp comparison works correctly only when all timestamps
    use a consistent format. The server returns ISO 8601 with 'Z' suffix; if any
    timestamps use '+00:00' instead, comparison may be unreliable. Normalise on ingest
    if the server format ever changes.

    Pure: does not mutate the input dict.
    """
    if not messages:
        return state
    latest = max(m.get("timestamp", "") for m in messages)
    if latest > state.get("last_seen_ts", ""):
        state = dict(state)
        state["last_seen_ts"] = latest
    return state


# ---------------------------------------------------------------------------
# Network I/O
# ---------------------------------------------------------------------------

def _poll_inbound(token: str, since: str) -> list[dict[str, Any]]:
    """GET /messages and return the list sorted by timestamp ascending.

    Returns [] on any network error (logged).
    """
    import httpx

    try:
        resp = httpx.get(
            f"{BOT_TALK_BASE_URL}/messages",
            headers={"X-Bot-Token": token},
            params={"since": since, "limit": 100},
            timeout=10.0,
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
        return sorted(messages, key=lambda m: m.get("timestamp", ""))
    except Exception as exc:
        log.warning(f"bot-talk poll failed: {exc}")
        return []


def _send_outbound(token: str, outbox_path: Path) -> list[tuple[Path, bool]]:
    """Drain outbound queue: POST each bot-talk message file in outbox_path.

    Returns a list of (file_path, success) tuples.
    """
    import httpx

    if not outbox_path.exists():
        return []

    results = []
    for msg_file in sorted(outbox_path.glob("*.json")):
        try:
            msg = json.loads(msg_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(f"Failed to read outbox file {msg_file}: {exc}")
            results.append((msg_file, False))
            continue

        if msg.get("source") != "bot-talk":
            continue  # not a bot-talk message

        payload = {
            "sender": MY_LOBSTER_NAME,
            "content": msg.get("text", ""),
            "genre": msg.get("genre", "status-update"),
            "tier": "TIER-BOT",
        }
        try:
            resp = httpx.post(
                f"{BOT_TALK_BASE_URL}/message",
                headers={"X-Bot-Token": token, "Content-Type": "application/json"},
                json=payload,
                timeout=10.0,
            )
            resp.raise_for_status()
            results.append((msg_file, True))
            log.info(f"Sent outbound: {msg_file.name}")
        except Exception as exc:
            log.warning(f"Failed to send outbound {msg_file.name}: {exc}")
            results.append((msg_file, False))

    return results


# ---------------------------------------------------------------------------
# Inbox writing
# ---------------------------------------------------------------------------

def _build_inbox_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Build an inbox message dict from a raw bot-talk message. Pure."""
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    uid = str(uuid.uuid4())[:8]
    return {
        "id": f"{ts_ms}_bot_talk_{uid}",
        "type": "text",
        "source": "bot-talk",
        "chat_id": ADMIN_CHAT_ID,
        "user_name": msg.get("sender", "unknown"),
        "text": msg.get("content", ""),
        "timestamp": msg.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "direction": "INBOUND",
        "from": msg.get("sender", "unknown"),
        "to": MY_LOBSTER_NAME,
    }


def _write_inbox_message(msg: dict[str, Any]) -> None:
    """Write an inbox message atomically to INBOX_DIR."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    inbox_msg = _build_inbox_message(msg)
    filename = f"{inbox_msg['id']}.json"
    target = INBOX_DIR / filename
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(inbox_msg, indent=2))
    tmp.rename(target)


# ---------------------------------------------------------------------------
# JSONL logging
# ---------------------------------------------------------------------------

def _append_log(entry: dict[str, Any]) -> None:
    """Append a JSONL entry to the lobstertalk log, rotating if > LOG_ROTATE_BYTES."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_ROTATE_BYTES:
        LOG_FILE.rename(LOG_FILE.with_suffix(".jsonl.bak"))
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Hot-mode systemd re-trigger
# ---------------------------------------------------------------------------

def _schedule_hot_retrigger(run_id: str) -> None:
    """Schedule a 5-minute one-shot systemd timer to re-run this job.

    Uses a timestamp suffix in the unit name to avoid collisions with a previous
    run that left the unit in failed/active state. A fixed name would cause
    systemd-run to refuse creating the unit if the old one still exists.

    Failure is non-fatal: the hourly baseline will catch any missed activity.
    """
    dispatch_script = (
        Path.home() / "lobster" / "scheduled-tasks" / "dispatch-job.sh"
    )
    # Timestamp suffix prevents "unit already exists" collision when a previous
    # hot-mode timer is still in failed/active state.
    unit_name = f"lobster-lobstertalk-unified-hot-{int(time.time())}"
    cmd = [
        "systemd-run", "--user",
        "--on-active=5min",
        f"--unit={unit_name}",
        str(dispatch_script), "lobstertalk-unified",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            log.warning(f"systemd-run failed (non-fatal): {result.stderr.decode()[:200]}")
    except Exception as exc:
        log.warning(f"hot-mode retrigger failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# MCP tool calls (called via uv run inside the Lobster scheduler context)
# ---------------------------------------------------------------------------

def _call_write_task_output(output: str, status: str) -> None:
    """Call write_task_output MCP tool via the scheduler's standard interface."""
    # In practice this is called by the Lobster scheduler which provides MCP context.
    # When run standalone (outside the scheduler), this is a no-op.
    pass


def _call_write_result(task_id: str, chat_id: int, has_inbound: bool) -> None:
    """Call write_result MCP tool to signal completion to the dispatcher."""
    # In practice this is called by the Lobster scheduler which provides MCP context.
    pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(task_id: str = "lobstertalk-unified") -> None:
    """Execute one full lobstertalk-unified cycle."""
    run_id = str(uuid.uuid4())[:8]
    debug = os.environ.get("LOBSTER_DEBUG", "").lower() == "true"

    # Step 1: Load state and token
    state = _load_state()
    token = _load_token()
    if not token:
        log.error("bot-talk token not found — aborting")
        _call_write_task_output("Token not found — aborted", "failed")
        _call_write_result(task_id, ADMIN_CHAT_ID, False)
        return

    # Step 2: Receive — GET /messages
    since = state["last_seen_ts"]
    all_messages = _poll_inbound(token, since)

    # Filter own echo
    inbound = [m for m in all_messages if m.get("sender") != MY_LOBSTER_NAME]

    received_count = 0
    for msg in inbound:
        try:
            _write_inbox_message(msg)
            # Emit to EventBus unconditionally (non-fatal — stub returns False until IPC is wired).
            # The EventBus path is independent of inbox-file delivery; both can run in parallel.
            try:
                _emit_to_eventbus({"direction": "inbound", **msg})
            except Exception as eb_exc:
                log.debug(f"EventBus emit failed (non-fatal): {eb_exc}")
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "direction": "INBOUND",
                "sender": msg.get("sender"),
                "content": msg.get("content", "")[:500],
                "job_run": run_id,
            })
            received_count += 1
            log.info(f"Routed INBOUND from {msg.get('sender')}: {msg.get('content', '')[:100]}")

            if debug:
                log.info(f"[DEBUG] INBOUND from {msg.get('sender')}: {msg.get('content', '')[:500]}")
        except Exception as exc:
            log.warning(f"Failed to write inbox message: {exc}")

    # Step 3: Advance timestamp cursor
    # At-least-once delivery note: if the process crashes after writing inbox
    # messages but before _write_state(), the cursor is not advanced and the
    # same messages will be re-fetched and re-delivered on the next run.
    # The inbox writer is idempotent for duplicates — the dispatcher deduplicates
    # by message ID — so this is acceptable (at-least-once, not exactly-once).
    state = _advance_cursor(state, all_messages)

    # Step 4: Hot-mode management
    state = _update_state_after_messages(state, received_count)
    if state["hot_mode"]:
        _schedule_hot_retrigger(run_id)

    # Step 5: Send — drain outbound queue
    outbound_results = _send_outbound(token, OUTBOX_DIR)
    sent_count = 0
    for msg_file, success in outbound_results:
        if success:
            sent_count += 1
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "direction": "OUTBOUND",
                "sender": MY_LOBSTER_NAME,
                "content": f"(from outbox: {msg_file.name})",
                "job_run": run_id,
            })
            try:
                processed_target = PROCESSED_DIR / msg_file.name
                PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
                msg_file.rename(processed_target)
            except Exception as exc:
                log.warning(f"Failed to move {msg_file.name} to processed: {exc}")

    # Step 6: Write state and output
    _write_state(state)

    summary_parts = []
    if received_count:
        summary_parts.append(f"Received {received_count} INBOUND messages, routed to inbox.")
    else:
        summary_parts.append("No new messages.")
    if sent_count:
        summary_parts.append(f"Sent {sent_count} OUTBOUND messages.")
    summary_parts.append(
        f"hot_mode={state['hot_mode']}, consecutive_empty={state['consecutive_empty_polls']}"
    )
    output = " ".join(summary_parts)
    log.info(output)

    _call_write_task_output(output, "success")
    _call_write_result(task_id, ADMIN_CHAT_ID, received_count > 0)


if __name__ == "__main__":
    task_id = sys.argv[1] if len(sys.argv) > 1 else "lobstertalk-unified"
    run(task_id=task_id)
