#!/usr/bin/env python3
"""
Gmail Watch Renewal — Lobster scheduled task.

Calls gmail.users.watch to renew the Pub/Sub push subscription for
the configured Gmail account. Gmail watch subscriptions expire
after 7 days; this job runs every 6 days to keep the subscription live.

The watch registers a Pub/Sub push notification from Gmail to:
  projects/myownlobster/topics/gmail-investor-notifications

Which triggers the Vercel endpoint:
  https://awp-two.vercel.app/api/webhooks/gmail?token=<GMAIL_PUBSUB_TOKEN>

GCP setup required before this job is useful:
  1. Topic must exist: projects/myownlobster/topics/gmail-investor-notifications
  2. Push subscription must exist pointing to the Vercel webhook URL
  3. Gmail service account must have roles/pubsub.publisher on the topic

See docs/email-processing-architecture.md for full context.

Exit codes:
  0 — watch renewed successfully
  1 — fatal error
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

LOBSTER_SRC = Path.home() / "lobster" / "src"
if str(LOBSTER_SRC) not in sys.path:
    sys.path.insert(0, str(LOBSTER_SRC))

from integrations.gmail.token_store import get_valid_token  # noqa: E402

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
PUBSUB_TOPIC = "projects/myownlobster/topics/gmail-investor-notifications"
LABEL_IDS = ["INBOX"]
# Read from environment — never hardcode in source. Validated at runtime in main().
LOBSTER_USER_ID: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID") or os.environ.get("ADMIN_CHAT_ID") or ""
CONFIG_PATH = Path.home() / "lobster-config" / "config.env"
STATE_PATH = Path.home() / "lobster-workspace" / "data" / "gmail-watch-state.json"
MCP_URL = "http://localhost:9100"
HTTP_TIMEOUT = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _write_task_output(output: str, status: str = "success") -> None:
    try:
        requests.post(f"{MCP_URL}/task-output",
                      json={"job_name": "gmail-watch-renewal", "output": output, "status": status},
                      timeout=10)
    except Exception:
        pass


def save_watch_state(expiration_ms: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "expiration_ms": expiration_ms,
        "expiration_iso": datetime.fromtimestamp(expiration_ms / 1000, tz=timezone.utc).isoformat(),
        "renewed_at": datetime.now(tz=timezone.utc).isoformat(),
        "topic": PUBSUB_TOPIC,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2))


def call_gmail_watch(access_token: str) -> dict:
    url = f"{GMAIL_API_BASE}/users/me/watch"
    payload = {"topicName": PUBSUB_TOPIC, "labelIds": LABEL_IDS, "labelFilterBehavior": "INCLUDE"}
    resp = requests.post(
        url, json=payload,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"gmail.users.watch failed: HTTP {resp.status_code} — {resp.text[:400]}")
    return resp.json()


def main() -> None:
    if not LOBSTER_USER_ID:
        msg = "LOBSTER_ADMIN_CHAT_ID env var is not set (set in ~/lobster-config/config.env)"
        log.error(msg)
        _write_task_output(msg, "failed")
        sys.exit(1)
    try:
        token = get_valid_token(LOBSTER_USER_ID)
        if not token or not token.access_token:
            raise RuntimeError("get_valid_token returned empty token")
        access_token = token.access_token
    except Exception as exc:
        msg = f"Gmail token refresh failed: {exc}"
        log.error(msg)
        _write_task_output(msg, "failed")
        sys.exit(1)

    try:
        result = call_gmail_watch(access_token)
    except Exception as exc:
        msg = f"gmail.users.watch call failed: {exc}"
        log.error(msg)
        _write_task_output(msg, "failed")
        sys.exit(1)

    expiration_ms = int(result.get("expiration", 0))
    history_id = result.get("historyId", "unknown")

    if expiration_ms:
        expiration_dt = datetime.fromtimestamp(expiration_ms / 1000, tz=timezone.utc)
        save_watch_state(expiration_ms)
        msg = (f"gmail.users.watch renewed. historyId={history_id} "
               f"expires={expiration_dt.isoformat()} topic={PUBSUB_TOPIC}")
        log.info(msg)
        _write_task_output(msg, "success")
    else:
        msg = f"gmail.users.watch succeeded but no expiration in response: {result}"
        log.warning(msg)
        _write_task_output(msg, "success")

    sys.exit(0)


if __name__ == "__main__":
    main()
