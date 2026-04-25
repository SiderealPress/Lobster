#!/usr/bin/env python3
"""
Gmail Poller — Lobster scheduled task.

Polls the Gmail History API for new inbox messages and injects them
into the Lobster inbox as JSON files.

Exit codes:
  0 — success (including 0 new messages)
  1 — fatal error (token failure, unrecoverable API error)

State is persisted at ~/lobster-workspace/data/gmail-poll-state.json.
Runs every 10 seconds via systemd timer (lobster scheduled job: gmail-poll).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Bootstrap: make lobster src importable regardless of cwd
# ---------------------------------------------------------------------------

LOBSTER_SRC = Path.home() / "lobster" / "src"
if str(LOBSTER_SRC) not in sys.path:
    sys.path.insert(0, str(LOBSTER_SRC))

from integrations.gmail.token_store import get_valid_token  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
HTTP_TIMEOUT = 15  # seconds

STATE_PATH = Path.home() / "lobster-workspace" / "data" / "gmail-poll-state.json"
INBOX_DIR = Path.home() / "messages" / "inbox"

# Labels that indicate promotional / social / automated mail — skip these.
SKIP_LABEL_IDS = frozenset({
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
})

# User ID for the Gmail token store (Telegram chat_id = user identifier).
GMAIL_USER_ID = "ADMIN_CHAT_ID_REDACTED"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [gmail-poll] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_sender(from_header: str) -> tuple[Optional[str], str]:
    """Extract (display_name, email) from a From header value.

    Pure function — no I/O.

    Examples:
        "Jane Doe <jane@example.com>" -> ("Jane Doe", "jane@example.com")
        "jane@example.com"            -> (None, "jane@example.com")
    """
    match = re.match(r'^(.+?)\s*<(.+?)>\s*$', from_header)
    if match:
        name = match.group(1).strip().strip('"\'')
        return (name or None, match.group(2).strip())
    return (None, from_header.strip())


def get_header(headers: list[dict], name: str) -> Optional[str]:
    """Return the value of the first header matching name (case-insensitive).

    Pure function — no I/O.
    """
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value")
    return None


def decode_base64url(data: str) -> str:
    """Decode a base64url-encoded string to UTF-8 text.

    Pure function — no I/O.
    """
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def extract_body(payload: dict) -> str:
    """Recursively extract plain text body from a Gmail message payload.

    Prefers text/plain; falls back through multipart parts; returns empty
    string if no usable text is found.

    Pure function — no I/O.
    """
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return decode_base64url(data) if data else ""

    if mime.startswith("text/") and not payload.get("parts"):
        data = payload.get("body", {}).get("data", "")
        return decode_base64url(data) if data else ""

    parts = payload.get("parts", [])
    # Prefer text/plain among immediate children
    for part in parts:
        if part.get("mimeType") == "text/plain":
            return extract_body(part)

    # Recurse through all parts
    for part in parts:
        text = extract_body(part)
        if text:
            return text

    return ""


def should_skip_message(labels: list[str], sender_email: str, account_email: str) -> bool:
    """Return True if this message should be skipped (not injected).

    Skips:
    - Messages sent by the authenticated account (self-sent).
    - Promotional / social / update category messages.

    Pure function — no I/O.
    """
    if sender_email.lower() == account_email.lower():
        return True
    return bool(set(labels) & SKIP_LABEL_IDS)


def build_inbox_message(
    gmail_message_id: str,
    thread_id: str,
    subject: Optional[str],
    from_name: Optional[str],
    from_email: str,
    to_header: Optional[str],
    body_text: str,
    received_at: str,
    account_email: str,
) -> dict:
    """Compose the Lobster inbox JSON payload for a single email.

    Pure function — no I/O.
    """
    display_name = from_name or from_email
    subject_display = subject or "(no subject)"
    text = (
        f"\U0001f4e7 Email from {display_name} <{from_email}>\n"
        f"Subject: {subject_display}\n\n"
        f"{body_text}"
    )
    return {
        "id": f"gmail-{gmail_message_id}",
        "source": "gmail",
        "chat_id": account_email,
        "sender_name": display_name,
        "text": text,
        "timestamp": received_at,
        "metadata": {
            "gmail_message_id": gmail_message_id,
            "gmail_thread_id": thread_id,
            "subject": subject_display,
            "from": f"{display_name} <{from_email}>",
            "to": to_header or "",
        },
    }


# ---------------------------------------------------------------------------
# State I/O (side-effecting)
# ---------------------------------------------------------------------------


def load_state() -> Optional[dict]:
    """Load poll state from disk. Returns None if file does not exist."""
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read state file: %s", exc)
        return None


def save_state(state: dict) -> None:
    """Atomically write poll state to disk."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.rename(STATE_PATH)


# ---------------------------------------------------------------------------
# Gmail HTTP helpers (side-effecting)
# ---------------------------------------------------------------------------


def gmail_get(path: str, access_token: str, params: Optional[dict] = None) -> dict:
    """Make an authenticated GET to the Gmail API.

    Raises:
        requests.HTTPError: On non-2xx responses.
        requests.RequestException: On network errors.
    """
    resp = requests.get(
        f"{GMAIL_API_BASE}{path}",
        params=params or {},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_user_profile(access_token: str) -> dict:
    """Fetch the authenticated user's Gmail profile (for bootstrapping)."""
    return gmail_get("/users/me/profile", access_token)


def get_history(access_token: str, start_history_id: str) -> dict:
    """Fetch history since the given historyId, filtered to INBOX messageAdded."""
    return gmail_get(
        "/users/me/history",
        access_token,
        params={
            "startHistoryId": start_history_id,
            "labelId": "INBOX",
            "historyTypes": "messageAdded",
        },
    )


def get_message(access_token: str, message_id: str) -> dict:
    """Fetch a full Gmail message by ID."""
    return gmail_get(
        f"/users/me/messages/{message_id}",
        access_token,
        params={"format": "full"},
    )


# ---------------------------------------------------------------------------
# Inbox injection (side-effecting)
# ---------------------------------------------------------------------------


def write_inbox_message(message: dict) -> bool:
    """Write a message JSON file to the Lobster inbox.

    Returns True if written, False if the file already exists (idempotency).
    """
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    msg_id = message["metadata"]["gmail_message_id"]
    dest = INBOX_DIR / f"gmail-{msg_id}.json"
    if dest.exists():
        log.info("Message %s already in inbox — skipping.", msg_id)
        return False
    dest.write_text(json.dumps(message, ensure_ascii=False), encoding="utf-8")
    log.info("Injected message %s into inbox.", msg_id)
    return True


# ---------------------------------------------------------------------------
# Token acquisition (side-effecting)
# ---------------------------------------------------------------------------


def acquire_access_token() -> str:
    """Return a valid Gmail access token, refreshing via the token store if needed.

    Raises:
        SystemExit(1): If no valid token is available.
    """
    token = get_valid_token(GMAIL_USER_ID)
    if token is None or not token.access_token:
        log.error(
            "No valid Gmail token available for user_id=%r. "
            "User must re-authenticate via the consent link.",
            GMAIL_USER_ID,
        )
        sys.exit(1)
    return token.access_token


# ---------------------------------------------------------------------------
# Core logic (orchestration)
# ---------------------------------------------------------------------------


def bootstrap_state(access_token: str) -> dict:
    """First-run: fetch the current historyId and save state.

    Does NOT inject any existing messages — sets the baseline for future polls.
    """
    log.info("No state file found — bootstrapping with current historyId.")
    profile = get_user_profile(access_token)
    history_id = str(profile.get("historyId", ""))
    email = profile.get("emailAddress", "")
    if not history_id:
        log.error("User profile returned no historyId: %s", profile)
        sys.exit(1)
    state = {"last_history_id": history_id, "email": email}
    save_state(state)
    log.info("Bootstrapped state: email=%s historyId=%s", email, history_id)
    return state


def process_history_item(
    history_item: dict,
    access_token: str,
    account_email: str,
) -> list[str]:
    """Process one history record and inject any new messages into the inbox.

    Returns a list of injected Gmail message IDs.
    """
    injected: list[str] = []
    for added in history_item.get("messagesAdded", []):
        msg_stub = added.get("message", {})
        msg_id = msg_stub.get("id")
        if not msg_id:
            continue

        # Idempotency check before fetching the full message
        dest = INBOX_DIR / f"gmail-{msg_id}.json"
        if dest.exists():
            log.info("Message %s already in inbox — skipping fetch.", msg_id)
            continue

        try:
            raw = get_message(access_token, msg_id)
        except requests.RequestException as exc:
            log.warning("Failed to fetch message %s: %s", msg_id, exc)
            continue

        payload = raw.get("payload", {})
        headers = payload.get("headers", [])
        labels = raw.get("labelIds", [])

        from_header = get_header(headers, "From") or ""
        from_name, from_email = parse_sender(from_header)
        to_header = get_header(headers, "To")
        subject = get_header(headers, "Subject")

        if should_skip_message(labels, from_email, account_email):
            log.info("Skipping message %s (labels=%s, from=%s)", msg_id, labels, from_email)
            continue

        body = extract_body(payload) or raw.get("snippet", "")

        internal_date = raw.get("internalDate")
        if internal_date:
            received_at = datetime.fromtimestamp(
                int(internal_date) / 1000, tz=timezone.utc
            ).isoformat()
        else:
            received_at = datetime.now(tz=timezone.utc).isoformat()

        message = build_inbox_message(
            gmail_message_id=msg_id,
            thread_id=raw.get("threadId", ""),
            subject=subject,
            from_name=from_name,
            from_email=from_email,
            to_header=to_header,
            body_text=body,
            received_at=received_at,
            account_email=account_email,
        )

        if write_inbox_message(message):
            injected.append(msg_id)

    return injected


def poll(access_token: str, state: dict) -> dict:
    """Run one poll cycle: fetch history, inject new messages, return updated state.

    Returns the updated state dict (caller saves after success).
    """
    last_history_id = state["last_history_id"]
    account_email = state.get("email", "")

    try:
        history_response = get_history(access_token, last_history_id)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            # historyId is too old — re-bootstrap from current position
            log.warning("historyId %s is too old (404) — re-bootstrapping.", last_history_id)
            return bootstrap_state(access_token)
        if exc.response is not None and exc.response.status_code == 429:
            log.warning("Gmail API rate-limited (429) — backing off 30s.")
            time.sleep(30)
            sys.exit(0)
        raise

    history_items = history_response.get("history", [])
    new_history_id = history_response.get("historyId", last_history_id)

    total_injected: list[str] = []
    for item in history_items:
        injected = process_history_item(item, access_token, account_email)
        total_injected.extend(injected)

    if total_injected:
        log.info("Injected %d new message(s): %s", len(total_injected), total_injected)
    else:
        log.debug("No new messages this cycle.")

    return {**state, "last_history_id": str(new_history_id)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    access_token = acquire_access_token()

    state = load_state()
    if state is None:
        bootstrap_state(access_token)
        # First run: baseline set, no messages to process.
        return

    try:
        updated_state = poll(access_token, state)
    except requests.HTTPError as exc:
        log.error("Gmail API HTTP error: %s", exc)
        sys.exit(1)
    except requests.RequestException as exc:
        log.error("Gmail API network error: %s", exc)
        sys.exit(1)

    save_state(updated_state)


if __name__ == "__main__":
    main()
