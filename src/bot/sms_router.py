#!/usr/bin/env python3
"""
Lobster SMS Router - Twilio SMS webhook to Claude Code bridge

Mirrors the WhatsApp router pattern:
1. Receives incoming SMS messages via Twilio webhook POST /webhook/sms
2. Writes messages to ~/messages/inbox/ in standard Lobster format
3. Watches ~/messages/outbox/ for replies with source="sms"
4. Sends replies back via Twilio SMS API

Environment variables required:
    TWILIO_ACCOUNT_SID    - Twilio account SID
    TWILIO_AUTH_TOKEN     - Twilio auth token (used to validate signatures)
    TWILIO_SMS_NUMBER     - Sending number in E.164 format, e.g. +1XXXXXXXXXX
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in _sys.path:
    _sys.path.insert(0, _SRC_DIR)
from utils.fs import atomic_write_json  # noqa: E402
from threading import Thread

from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
import uvicorn
# ---------------------------------------------------------------------------
# Shared outbox handler (src/channels/outbox.py)
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
from channels.outbox import OutboxFileHandler, OutboxWatcher, drain_outbox  # noqa: E402

# ---------------------------------------------------------------------------
# Canonical atomic filesystem helper (src/utils/fs.py)
# ---------------------------------------------------------------------------
import sys as _sys
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in _sys.path:
    _sys.path.insert(0, _SRC_DIR)
from utils.fs import atomic_write_json  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_SMS_NUMBER = os.environ.get("TWILIO_SMS_NUMBER", "")

WEBHOOK_BASE_URL = os.environ.get("TWILIO_WEBHOOK_BASE_URL", "")
WEBHOOK_PATH = "/webhook/sms"
WEBHOOK_URL = WEBHOOK_BASE_URL.rstrip("/") + WEBHOOK_PATH

# Optional: restrict to specific phone numbers (E.164, e.g. "+12025551234")
ALLOWED_NUMBERS = [
    x.strip()
    for x in os.environ.get("SMS_ALLOWED_NUMBERS", "").split(",")
    if x.strip()
]

# Directories
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))

INBOX_DIR = _MESSAGES / "inbox"
OUTBOX_DIR = _MESSAGES / "outbox"
IMAGES_DIR = _MESSAGES / "images"
FILES_DIR = _MESSAGES / "files"

for _d in [INBOX_DIR, OUTBOX_DIR, IMAGES_DIR, FILES_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# Logging
LOG_DIR = _WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("lobster-sms")
log.setLevel(logging.INFO)
# Import GzipRotatingFileHandler from the local src/mcp/log_utils module.
# We can't use ``from mcp.log_utils import ...`` directly because the external
# ``mcp`` package (from the MCP SDK) shadows our local src/mcp directory
# when it is installed in the venv.
import importlib.util as _ilu
_lutils_spec = _ilu.spec_from_file_location(
    "lobster_mcp_log_utils",
    Path(__file__).resolve().parent.parent / "mcp" / "log_utils.py",
)
_lutils_mod = _ilu.module_from_spec(_lutils_spec)  # type: ignore[arg-type]
_lutils_spec.loader.exec_module(_lutils_mod)  # type: ignore[union-attr]
GzipRotatingFileHandler = _lutils_mod.GzipRotatingFileHandler
_fh = GzipRotatingFileHandler(
    LOG_DIR / "sms-router.log",
    maxBytes=1 * 1024 * 1024 * 1024,  # 1 GB per file
    backupCount=5,                      # 5 gzip-compressed backups → ~5 GB history
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
log.addHandler(logging.StreamHandler())

# ---------------------------------------------------------------------------
# Twilio clients (lazy)
# ---------------------------------------------------------------------------

_twilio_client: TwilioClient | None = None
_twilio_validator: RequestValidator | None = None


def _get_twilio_client() -> TwilioClient:
    global _twilio_client
    if _twilio_client is None:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set"
            )
        _twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


def _get_validator() -> RequestValidator:
    global _twilio_validator
    if _twilio_validator is None:
        if not TWILIO_AUTH_TOKEN:
            raise RuntimeError("TWILIO_AUTH_TOKEN must be set")
        _twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return _twilio_validator


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _make_msg_id() -> str:
    return f"{int(time.time() * 1000)}_sms"


def _twiml_ok() -> Response:
    """Return an empty TwiML 200 response (no auto-reply)."""
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
        status_code=200,
    )


def _twiml_error(status: int = 403) -> Response:
    return Response(content="Forbidden", status_code=status)


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def _is_valid_twilio_request(request: Request, body: bytes) -> bool:
    """Validate X-Twilio-Signature using the Twilio SDK helper."""
    if not TWILIO_AUTH_TOKEN:
        log.warning("TWILIO_AUTH_TOKEN not set; skipping signature validation")
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        log.warning("Missing X-Twilio-Signature header")
        return False

    try:
        from urllib.parse import parse_qs
        params = {
            k: v[0]
            for k, v in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()
        }
        validator = _get_validator()
        return validator.validate(WEBHOOK_URL, params, signature)
    except Exception as e:
        log.error(f"Signature validation error: {e}")
        return False


# ---------------------------------------------------------------------------
# Inbox writer
# ---------------------------------------------------------------------------

def write_to_inbox(msg_data: dict) -> None:
    """Write a message dict atomically to the Lobster inbox."""
    msg_id = msg_data["id"]
    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)
    log.info(f"Wrote SMS message to inbox: {msg_id}")


def build_text_message(form: dict) -> dict:
    """Build a standard text message from Twilio SMS form fields."""
    from_number = form.get("From", "").strip()
    body = form.get("Body", "").strip()
    msg_sid = form.get("MessageSid", "")
    msg_id = _make_msg_id()

    return {
        "id": msg_id,
        "source": "sms",
        "chat_id": from_number,
        "user_id": from_number,
        "username": from_number,
        "user_name": from_number,
        "text": body,
        "twilio_message_sid": msg_sid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_media_message(form: dict) -> dict:
    """Build a message that includes MMS media (image/document)."""
    msg = build_text_message(form)
    num_media = int(form.get("NumMedia", "0"))
    if num_media == 0:
        return msg

    msg_id = msg["id"]
    media_items = []

    for i in range(num_media):
        media_url = form.get(f"MediaUrl{i}", "")
        media_type = form.get(f"MediaContentType{i}", "")
        if not media_url:
            continue

        try:
            saved_path = _download_media(media_url, msg_id, i, media_type)
            item = {
                "url": media_url,
                "content_type": media_type,
                "local_path": str(saved_path),
            }
            media_items.append(item)

            if i == 0:
                if media_type.startswith("image/"):
                    msg["type"] = "photo"
                    msg["image_file"] = str(saved_path)
                    msg["text"] = msg.get("text") or "[Image]"
                else:
                    msg["type"] = "document"
                    msg["file_path"] = str(saved_path)
                    msg["text"] = msg.get("text") or f"[File: {media_type}]"
        except Exception as e:
            log.error(f"Failed to download media {i} ({media_url}): {e}")

    if media_items:
        msg["media"] = media_items

    return msg


def _download_media(url: str, msg_id: str, index: int, content_type: str) -> Path:
    """Download a Twilio media URL to local storage."""
    import urllib.request
    import base64

    creds = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {creds}")

    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }
    ext = ext_map.get(content_type, "")
    if not ext and "/" in content_type:
        ext = "." + content_type.split("/")[-1].split(";")[0]

    if content_type.startswith("image/"):
        save_dir = IMAGES_DIR
    else:
        save_dir = FILES_DIR

    filename = f"{msg_id}_media{index}{ext}"
    save_path = save_dir / filename

    with urllib.request.urlopen(req) as response:
        with open(save_path, "wb") as f:
            f.write(response.read())

    log.info(f"Downloaded SMS media to: {save_path}")
    return save_path


# ---------------------------------------------------------------------------
# Outbox watcher — sends SMS replies
# ---------------------------------------------------------------------------

def send_sms_message(to: str, text: str) -> bool:
    """Send an SMS message via Twilio REST API.

    Args:
        to: Recipient phone number in E.164 format
        text: Message body

    Returns:
        True on success, False on failure.
    """
    if not TWILIO_SMS_NUMBER:
        log.error("TWILIO_SMS_NUMBER not configured — cannot send SMS reply")
        return False

    try:
        client = _get_twilio_client()
        message = client.messages.create(
            from_=TWILIO_SMS_NUMBER,
            to=to,
            body=text,
        )
        log.info(f"Sent SMS reply to {to}: sid={message.sid}")
        return True
    except Exception as e:
        log.error(f"Failed to send SMS to {to}: {e}")
        return False


def _send_sms_reply(reply: dict) -> bool:
    """Pure send function for the shared OutboxFileHandler.

    Extracts ``chat_id`` and ``text`` from *reply* and delegates to the
    existing :func:`send_sms_message` helper.
    """
    return send_sms_message(reply["chat_id"], reply["text"])


# Backward-compatible alias -- code that imports OutboxHandler directly still works.
OutboxHandler = OutboxFileHandler


def process_existing_outbox() -> None:
    """Deliver any SMS outbox files that piled up before startup."""
    drain_outbox(OUTBOX_DIR, source="sms", send_fn=_send_sms_reply, log=log)


# ---------------------------------------------------------------------------
# Starlette webhook endpoint
# ---------------------------------------------------------------------------

async def sms_webhook(request: Request) -> Response:
    """POST /webhook/sms — receives inbound SMS messages from Twilio."""

    body = await request.body()

    if TWILIO_AUTH_TOKEN and not _is_valid_twilio_request(request, body):
        log.warning(f"Invalid Twilio signature from {request.client.host}")
        return _twiml_error(403)

    from urllib.parse import parse_qs
    form = {
        k: v[0]
        for k, v in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()
    }

    from_number = form.get("From", "").strip()

    if not from_number:
        log.warning("Received webhook with no From field — ignoring")
        return _twiml_ok()

    # Optional allow-list check
    if ALLOWED_NUMBERS and from_number not in ALLOWED_NUMBERS:
        log.warning(f"Rejected message from unlisted number: {from_number}")
        return _twiml_ok()

    # Build and write message
    num_media = int(form.get("NumMedia", "0"))
    if num_media > 0:
        msg_data = build_media_message(form)
    else:
        msg_data = build_text_message(form)

    write_to_inbox(msg_data)

    return _twiml_ok()


async def health_check(request: Request) -> Response:
    """GET /webhook/sms/health — basic liveness probe."""
    configured = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_SMS_NUMBER)
    status = "ok" if configured else "unconfigured"
    return Response(
        content=json.dumps({"status": status, "source": "sms"}),
        media_type="application/json",
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> Starlette:
    return Starlette(
        routes=[
            Route(WEBHOOK_PATH, sms_webhook, methods=["POST"]),
            Route(WEBHOOK_PATH + "/health", health_check, methods=["GET"]),
        ]
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    port = int(os.environ.get("SMS_ROUTER_PORT", "8744"))

    log.info("Starting Lobster SMS Router...")
    log.info(f"Inbox: {INBOX_DIR}")
    log.info(f"Outbox: {OUTBOX_DIR}")
    log.info(f"Webhook URL: {WEBHOOK_URL}")
    log.info(f"SMS Number: {TWILIO_SMS_NUMBER}")
    log.info(f"Listening on port: {port}")

    if not TWILIO_ACCOUNT_SID:
        log.warning("TWILIO_ACCOUNT_SID not set — outbound messages will fail")
    if not TWILIO_AUTH_TOKEN:
        log.warning("TWILIO_AUTH_TOKEN not set — signature validation disabled")
    if not TWILIO_SMS_NUMBER:
        log.warning("TWILIO_SMS_NUMBER not set — outbound messages will fail")
    if ALLOWED_NUMBERS:
        log.info(f"Allowed SMS numbers: {ALLOWED_NUMBERS}")

    # Start outbox watcher thread
    observer = Observer()
    observer.schedule(
        OutboxWatcher(source="sms", send_fn=_send_sms_reply, log=log),
        str(OUTBOX_DIR),
        recursive=False,
    )
    observer.daemon = True
    observer.start()
    log.info("Watching outbox for SMS replies...")

    # Drain any replies that queued up before we started
    drain_outbox(OUTBOX_DIR, source="sms", send_fn=_send_sms_reply, log=log)

    # Start HTTP server
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
