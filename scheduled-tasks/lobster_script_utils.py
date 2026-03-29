#!/usr/bin/env python3
"""
lobster_script_utils — shared utilities for code-first Lobster poller scripts.

Code-first scripts (Kind 1) import this module to:
  - write_to_inbox(text, chat_id, job_name)  — drop an actionable message into ~/messages/inbox/
  - log_result(job_name, status, text)        — write a run record to scheduled-jobs/logs/
  - get_config(key)                           — read a value from config.env or the environment

Usage example (in a poller script):
    from lobster_script_utils import write_to_inbox, log_result, get_config

    chat_id = get_config("TELEGRAM_ALLOWED_USERS")
    if new_items:
        write_to_inbox(f"Found {len(new_items)} new items: ...", chat_id, "my-poller")
        log_result("my-poller", "success", f"{len(new_items)} items")
    else:
        log_result("my-poller", "success", "no new items")
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Path resolution — mirrors inbox_server.py conventions
# ---------------------------------------------------------------------------

_HOME = Path.home()
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))
_REPO_DIR = Path(os.environ.get("LOBSTER_INSTALL_DIR", _HOME / "lobster"))
_CONFIG_DIR = Path(os.environ.get("LOBSTER_CONFIG_DIR", _HOME / "lobster-config"))

INBOX_DIR = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages")) / "inbox"
LOGS_DIR = _WORKSPACE / "scheduled-jobs" / "logs"


def _ensure_dirs() -> None:
    """Create required directories if they do not exist."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------

def get_config(key: str, default: str = "") -> str:
    """Return a configuration value by key.

    Resolution order:
    1. Environment variable ``key``
    2. ``~/lobster/config/config.env``  (KEY=VALUE lines)
    3. ``default`` (empty string unless caller provides one)
    """
    # 1. Environment variable
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val

    # 2. config.env file
    config_file = _REPO_DIR / "config" / "config.env"
    if config_file.exists():
        for line in config_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()

    return default


# ---------------------------------------------------------------------------
# write_to_inbox
# ---------------------------------------------------------------------------

def write_to_inbox(text: str, chat_id: str | int, job_name: str) -> Path:
    """Write an actionable message to ~/messages/inbox/ for the dispatcher to deliver.

    The message is written atomically as a JSON file.  The dispatcher's
    wait_for_messages loop will pick it up and relay it to the user.

    Args:
        text:     Human-readable message body.
        chat_id:  Telegram/Slack chat ID.  Must be a non-empty, non-zero value.
        job_name: Slug identifying the calling script (used in the message subject).

    Returns:
        Path to the written inbox file.

    Raises:
        ValueError: if chat_id is empty, zero, or falsy.
    """
    # Guard: refuse to write if chat_id is not a real value.
    if not chat_id or chat_id == 0 or str(chat_id).strip() in ("", "0"):
        raise ValueError(
            f"write_to_inbox: chat_id must be a real, non-zero value (got {chat_id!r}). "
            "Set TELEGRAM_ALLOWED_USERS in config.env or pass chat_id explicitly."
        )

    _ensure_dirs()

    now = datetime.now(timezone.utc)
    msg_id = str(uuid.uuid4())

    message = {
        "id": msg_id,
        "source": "scheduled_job",
        "job_name": job_name,
        "chat_id": chat_id,
        "text": text,
        "timestamp": now.isoformat(),
        "type": "scheduled_job_output",
    }

    ts_str = now.strftime("%Y%m%d-%H%M%S")
    filename = f"{ts_str}-{job_name}-{msg_id[:8]}.json"
    dest = INBOX_DIR / filename

    # Atomic write: write to a temp file then rename
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(message, indent=2))
    tmp.rename(dest)

    return dest


# ---------------------------------------------------------------------------
# log_result
# ---------------------------------------------------------------------------

def log_result(job_name: str, status: str, text: str) -> Path:
    """Append a run record to ~/lobster-workspace/scheduled-jobs/logs/<job_name>.jsonl.

    Args:
        job_name: Slug identifying the script.
        status:   "success" or "failed".
        text:     Short summary of what happened.

    Returns:
        Path to the log file.
    """
    _ensure_dirs()

    if status not in ("success", "failed"):
        status = "success"

    now = datetime.now(timezone.utc)
    record = {
        "timestamp": now.isoformat(),
        "job_name": job_name,
        "status": status,
        "text": text,
    }

    log_file = LOGS_DIR / f"{job_name}.jsonl"
    with open(log_file, "a") as fh:
        fh.write(json.dumps(record) + "\n")

    return log_file
