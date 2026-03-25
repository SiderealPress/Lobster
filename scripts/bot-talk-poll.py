#!/usr/bin/env python3
"""
Bot-talk poller — no LLM, no claude -p.

Polls the bot-talk HTTP API for new messages (both senders) and forwards them
to Telegram. Runs via cron every 2 minutes. State is persisted in
~/lobster-workspace/data/bot-talk-state.json.

Usage:
    uv run ~/lobster/scripts/bot-talk-poll.py
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "httpx", "-q"], check=True)
    import httpx

# ── Config ──────────────────────────────────────────────────────────────────
HOME = Path.home()
WORKSPACE = HOME / "lobster-workspace"
CONFIG_ENV = HOME / "lobster-config" / "config.env"

TOKEN_FILE = WORKSPACE / "data" / "bot-talk-token.txt"
STATE_FILE = WORKSPACE / "data" / "bot-talk-state.json"
LOG_FILE = WORKSPACE / "logs" / "bot-talk-poll.log"

SAHAR_CHAT_ID = "8305714125"
REQUEST_TIMEOUT = 10  # seconds
DEFAULT_BOT_TALK_API = "http://bot-talk-api:4242"  # override via BOT_TALK_API_URL in config.env


def load_config() -> dict:
    """Load key=value pairs from config.env."""
    cfg = {}
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_message_ts": None}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log(f"Telegram error: {e}")
        return False


def format_message(msg: dict) -> str:
    sender = msg.get("sender", "unknown")
    text = msg.get("message", "")
    ts = msg.get("timestamp", "")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_label = dt.strftime("%H:%M UTC")
        except Exception:
            ts_label = ts[:16]
    else:
        ts_label = ""

    if sender == "SaharLobster":
        prefix = "📤 *SaharLobster → Albert*"
    elif sender == "AlbertLobster":
        prefix = "📥 *AlbertLobster → Sahar*"
    else:
        prefix = f"💬 *{sender}*"

    ts_part = f" _{ts_label}_" if ts_label else ""
    return f"{prefix}{ts_part}:\n{text}"


def main() -> None:
    cfg = load_config()
    bot_token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    bot_talk_api = os.environ.get("BOT_TALK_API_URL") or cfg.get("BOT_TALK_API_URL") or DEFAULT_BOT_TALK_API
    if not bot_token:
        log("ERROR: TELEGRAM_BOT_TOKEN not found in config.env")
        sys.exit(1)

    if not TOKEN_FILE.exists():
        log("ERROR: bot-talk token file not found — skipping poll")
        sys.exit(0)

    api_token = TOKEN_FILE.read_text().strip()
    if not api_token:
        log("ERROR: bot-talk token file is empty")
        sys.exit(0)

    state = load_state()
    last_ts = state.get("last_message_ts")

    headers = {"X-Bot-Token": api_token}
    params = {}
    if last_ts:
        params["since"] = last_ts

    try:
        r = httpx.get(f"{bot_talk_api}/messages", headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        log(f"API error: {e}")
        sys.exit(0)

    if r.status_code == 401:
        log("ERROR: bot-talk API returned 401 — token invalid or expired")
        sys.exit(0)

    if r.status_code != 200:
        log(f"API returned {r.status_code}")
        sys.exit(0)

    try:
        data = r.json()
    except Exception as e:
        log(f"JSON parse error: {e}")
        sys.exit(0)

    messages = data if isinstance(data, list) else data.get("messages", [])

    # Filter to only new messages
    if last_ts:
        messages = [m for m in messages if m.get("timestamp", "") > last_ts]

    if not messages:
        log("No new messages")
        sys.exit(0)

    # Sort chronologically
    messages.sort(key=lambda m: m.get("timestamp", ""))

    log(f"{len(messages)} new message(s)")

    # Format and send
    blocks = [format_message(m) for m in messages]
    text = "\n\n".join(blocks)

    # Telegram has 4096 char limit — split if needed
    if len(text) > 4000:
        chunks = []
        current = []
        current_len = 0
        for block in blocks:
            if current_len + len(block) > 3800 and current:
                chunks.append("\n\n".join(current))
                current = [block]
                current_len = len(block)
            else:
                current.append(block)
                current_len += len(block)
        if current:
            chunks.append("\n\n".join(current))
    else:
        chunks = [text]

    for chunk in chunks:
        if not send_telegram(bot_token, SAHAR_CHAT_ID, chunk):
            log("Failed to send Telegram message")
            sys.exit(1)

    # Update state — track newest timestamp
    newest_ts = max(m.get("timestamp", "") for m in messages)
    state["last_message_ts"] = newest_ts
    save_state(state)
    log(f"Delivered {len(messages)} message(s), last_ts={newest_ts}")


if __name__ == "__main__":
    main()
