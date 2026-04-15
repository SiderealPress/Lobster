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
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_bot_talk_url = os.environ.get("BOT_TALK_URL")
if not _bot_talk_url:
    raise RuntimeError("BOT_TALK_URL environment variable is not set — cannot start lobstertalk")
BOT_TALK_BASE_URL = _bot_talk_url

MY_LOBSTER_NAME: str = (
    os.environ.get("BOT_TALK_SENDER")
    or os.environ.get("LOBSTER_NAME")
    or os.environ.get("BOT_TALK_SENDER_NAME")
    or ""
)
if not MY_LOBSTER_NAME:
    raise RuntimeError("BOT_TALK_SENDER env var required (set in ~/messages/config/config.env)")

ADMIN_CHAT_ID_STR: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID") or os.environ.get("ADMIN_CHAT_ID") or ""
if not ADMIN_CHAT_ID_STR:
    raise RuntimeError("LOBSTER_ADMIN_CHAT_ID env var required (or ADMIN_CHAT_ID as fallback)")
ADMIN_CHAT_ID: int = int(ADMIN_CHAT_ID_STR)
STATE_FILE = Path.home() / "lobster-workspace" / "data" / "lobstertalk-unified-state.json"
INBOX_DIR = Path.home() / "messages" / "inbox"
OUTBOX_DIR = Path.home() / "messages" / "outbox"
PROCESSED_DIR = Path.home() / "messages" / "processed"
LOG_FILE = Path.home() / "lobster-workspace" / "logs" / "lobstertalk.jsonl"
LOG_ROTATE_BYTES = 50 * 1024 * 1024  # 50 MB

# Exit hot mode after this many seconds of no new messages (20 minutes)
HOT_MODE_TIMEOUT_SECS = 20 * 60

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
        "last_activity_ts": None,
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

def _update_state_after_messages(
    state: dict[str, Any], message_count: int, now: datetime | None = None
) -> dict[str, Any]:
    """Return updated state dict after a poll that returned `message_count` messages.

    Hot-mode entry: any messages received → hot_mode=True, update last_activity_ts.
    Hot-mode exit: time-based — if now - last_activity_ts >= HOT_MODE_TIMEOUT_SECS,
    exit hot mode regardless of poll count. This prevents the dispatcher from
    being stuck in hot mode forever if the conversation goes quiet.

    Pure: does not mutate the input dict.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    state = dict(state)
    if message_count > 0:
        state["hot_mode"] = True
        state["last_activity_ts"] = now.isoformat()
        if not state.get("hot_mode_activated_at"):
            state["hot_mode_activated_at"] = now.isoformat()
    else:
        # Check time-based timeout
        last_activity = state.get("last_activity_ts")
        if last_activity:
            try:
                last_dt = datetime.fromisoformat(last_activity)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                idle_secs = (now - last_dt).total_seconds()
                if idle_secs >= HOT_MODE_TIMEOUT_SECS:
                    state["hot_mode"] = False
                    state["hot_mode_activated_at"] = None
            except (ValueError, TypeError):
                # Malformed timestamp — exit hot mode to be safe
                state["hot_mode"] = False
                state["hot_mode_activated_at"] = None
        else:
            # No activity recorded yet — exit hot mode
            state["hot_mode"] = False
            state["hot_mode_activated_at"] = None
    return state


def _advance_cursor(state: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return updated state with last_seen_ts advanced to the latest message timestamp.

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

    Uses sudo systemd-run (system scope) instead of --user because this script
    runs inside a systemd system service unit where no D-Bus user session is
    available (--user would fail with "Failed to connect to bus: No medium found").

    Failure is non-fatal: the hourly baseline will catch any missed activity.
    """
    uv_bin = Path.home() / ".local" / "bin" / "uv"
    script = Path.home() / "lobster" / "scheduled-tasks" / "lobstertalk_unified.py"
    cmd = [
        "sudo", "systemd-run",
        "--on-active=5min",
        "--unit=lobster-lobstertalk-unified-hot",
        str(uv_bin), "run", str(script),
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
    """Write task output to ~/messages/task-outputs/ as a JSON file."""
    task_outputs_dir = Path.home() / "messages" / "task-outputs"
    task_outputs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    filename = f"{timestamp.strftime('%Y%m%d-%H%M%S')}-lobstertalk-unified.json"
    record = {
        "job_name": "lobstertalk-unified",
        "timestamp": timestamp.isoformat(),
        "status": status,
        "output": output,
    }
    (task_outputs_dir / filename).write_text(json.dumps(record, indent=2))


def _call_write_result(task_id: str, chat_id: int, has_inbound: bool) -> None:
    """Write a subagent_result notification to ~/messages/inbox/ for the dispatcher."""
    inbox_dir = Path.home() / "messages" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc)
    filename = f"{int(ts.timestamp() * 1000)}_{task_id}.json"
    record = {
        "id": f"{int(ts.timestamp() * 1000)}_{task_id}",
        "type": "subagent_result",
        "task_id": task_id,
        "chat_id": chat_id,
        "source": "system",
        "timestamp": ts.isoformat(),
        "status": "success",
        "sent_reply_to_user": has_inbound,
        "text": f"lobstertalk-unified complete. inbound={has_inbound}",
    }
    (inbox_dir / filename).write_text(json.dumps(record, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(task_id: str = "lobstertalk-unified") -> None:
    """Execute one full lobstertalk-unified cycle."""
    if os.environ.get("LOBSTER_ENABLE_BOTTALK", "").lower() not in ("1", "true", "yes"):
        _call_write_task_output("LOBSTER_ENABLE_BOTTALK not set — skipping.", "success")
        return

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
    state = _advance_cursor(state, all_messages)

    # Step 4: Hot-mode management
    # Use total traffic (all_messages) not just filtered inbound count so that
    # our own outbound echoes from the server keep hot mode alive — the
    # conversation is active even if all polled messages were our own.
    traffic_count = len(all_messages)
    state = _update_state_after_messages(state, traffic_count, now=datetime.now(timezone.utc))
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
