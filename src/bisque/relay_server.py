#!/usr/bin/env python3
"""
Lobster Bisque Relay Server — Wire Protocol v2

A WebSocket relay that bridges bisque-chat (browser PWA) with the Lobster
message queue. Supports session-token auth, snapshot-on-connect, event replay,
and inotify-based delivery via an event bus.

Protocol v2:
    Client → Server: auth, send_message, ack, ping
    Server → Client: auth_success, auth_error, snapshot, message, inbox_update,
                     status, tool_call, tool_result, stream_start, stream_delta,
                     stream_end, agent_started, agent_completed, error, pong

Usage:
    python3 relay_server.py [--host 0.0.0.0] [--port 9101]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json
import logging
import logging.handlers
import mimetypes
import os
import signal
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from bisque.auth import TokenStore, create_bootstrap_token, handle_auth_exchange
from bisque.event_bus import EventBus, OutboxEventSource, FileSystemEventSource
from bisque.event_log import EventLog
from bisque.protocol import (
    ProtocolError,
    deserialize,
    frame_auth_error,
    frame_auth_success,
    frame_error,
    frame_hello,
    frame_pong,
    frame_snapshot,
    validate_client_frame,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))

INBOX_DIR = _MESSAGES / "inbox"
BISQUE_OUTBOX_DIR = _MESSAGES / "bisque-outbox"
WIRE_EVENTS_DIR = _MESSAGES / "wire-events"
SENT_DIR = _MESSAGES / "sent"
UPLOADS_DIR = _MESSAGES / "bisque-uploads"

# Maximum file upload size: 50 MB
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# MIME types we serve inline (images, video); everything else is an attachment
_INLINE_MIME_PREFIXES = ("image/", "video/", "audio/")

# Map content-type prefixes to file extensions for raw binary uploads
_MIME_EXT_MAP: dict[str, str] = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def _mime_to_ext(mime_type: str) -> str:
    """Return a file extension for a MIME type, or empty string if unknown."""
    # Strip parameters like ;codecs=opus
    base_type = mime_type.split(";")[0].strip().lower()
    return _MIME_EXT_MAP.get(base_type, "")


# P1.3: Token store lives outside the repo at ~/messages/config/bisque-tokens.json.
# Fall back to the legacy in-repo path for environments that have not yet migrated.
_EXTERNAL_TOKENS_FILE = _MESSAGES / "config" / "bisque-tokens.json"
_BISQUE_CHAT_PROJECT = _WORKSPACE / "projects" / "bisque-chat"
_LEGACY_TOKENS_FILE = _BISQUE_CHAT_PROJECT / "data" / "tokens.json"


def _resolve_tokens_file() -> Path:
    """Return the active token-store path, migrating the legacy file if needed."""
    config_dir = _MESSAGES / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    # Migrate legacy in-repo file to new location on first run
    if _LEGACY_TOKENS_FILE.exists() and not _EXTERNAL_TOKENS_FILE.exists():
        import shutil
        shutil.copy2(_LEGACY_TOKENS_FILE, _EXTERNAL_TOKENS_FILE)
        log.info("Migrated token store from %s to %s", _LEGACY_TOKENS_FILE, _EXTERNAL_TOKENS_FILE)
    return _EXTERNAL_TOKENS_FILE


_TOKENS_FILE = _resolve_tokens_file()

# ---------------------------------------------------------------------------
# P3.12: Structured JSON log formatter
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """P3.12: Emit one-line JSON objects for every log record.

    Each record includes: ts (ISO 8601), level, logger, message, and any
    ``extra`` fields passed via ``logging.info(..., extra={...})``.

    Used for the rotating log file so that observability tooling (e.g. the
    relay health monitor) can parse log lines without fragile regex.
    The console handler keeps the human-readable formatter for readability.
    """

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any extra fields injected via ``logging.info(msg, extra={...})``
        for key, val in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and not key.startswith("_"):
                # Skip standard LogRecord attributes; include custom extras only
                entry[key] = val
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = _WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("lobster-bisque-relay")
log.setLevel(logging.INFO)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bisque-relay.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
# P3.12: Use JSON formatter for the file handler so log lines are machine-parseable
_file_handler.setFormatter(_JsonFormatter())
log.addHandler(_file_handler)
# Console handler keeps human-readable format
log.addHandler(logging.StreamHandler())


# ---------------------------------------------------------------------------
# P3.6: Rate limiter — token bucket per IP, applied to auth and upload endpoints
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Token-bucket rate limiter keyed by remote IP.

    Each IP gets `capacity` tokens that refill at `rate` tokens/second.
    A call to ``is_allowed(ip)`` consumes one token and returns True if the
    bucket was non-empty, False if the IP should be throttled.

    Uses a lazy-refill strategy: tokens are added proportional to elapsed
    time since the last request, capped at `capacity`.  This avoids a
    background thread while still being accurate.

    Thread-safety note: the relay is asyncio-based (single-threaded event
    loop), so plain dict access is safe without a mutex.
    """

    def __init__(self, rate: float = 5.0, capacity: float = 10.0) -> None:
        """
        Args:
            rate:     Tokens refilled per second (default 5 — 5 req/s steady state).
            capacity: Maximum burst size (default 10).
        """
        self._rate = rate
        self._capacity = capacity
        # ip -> (tokens: float, last_refill_ts: float)
        self._buckets: dict[str, tuple[float, float]] = {}

    def is_allowed(self, ip: str) -> bool:
        """Consume one token for `ip`. Returns True if allowed, False if throttled."""
        now = time.monotonic()
        tokens, last_ts = self._buckets.get(ip, (self._capacity, now))
        # Refill proportional to elapsed time
        elapsed = now - last_ts
        tokens = min(self._capacity, tokens + elapsed * self._rate)
        if tokens < 1.0:
            self._buckets[ip] = (tokens, now)
            return False
        self._buckets[ip] = (tokens - 1.0, now)
        return True

    def purge_old(self, max_age: float = 300.0) -> int:
        """Remove buckets that have been idle for `max_age` seconds. Returns count removed."""
        now = time.monotonic()
        stale = [ip for ip, (_, ts) in self._buckets.items() if now - ts > max_age]
        for ip in stale:
            del self._buckets[ip]
        return len(stale)


# Auth endpoints: 5 req/s steady, burst 10
_AUTH_RATE_LIMITER = _RateLimiter(rate=5.0, capacity=10.0)
# Upload endpoint: 2 req/s steady, burst 5 (uploads are heavier)
_UPLOAD_RATE_LIMITER = _RateLimiter(rate=2.0, capacity=5.0)


# ---------------------------------------------------------------------------
# Admin configuration (read from environment at startup)
# ---------------------------------------------------------------------------

# Secret used to protect the POST /auth/admin/token endpoint.
# Reads BISQUE_ADMIN_SECRET first, falls back to ADMIN_SECRET.
_ADMIN_SECRET: str = os.environ.get("BISQUE_ADMIN_SECRET", "") or os.environ.get("ADMIN_SECRET", "")

# Public relay URL embedded in login tokens so the browser knows where to connect.
# Example: wss://178.104.15.109.nip.io/bisque-relay
_RELAY_URL: str = os.environ.get("BISQUE_RELAY_URL", "")


# ---------------------------------------------------------------------------
# Inbox injection
# ---------------------------------------------------------------------------

def _resolve_voice_local_path(attachment_url: str) -> Path | None:
    """Resolve a voice attachment URL to its local file path in UPLOADS_DIR.

    The relay server stores files at UPLOADS_DIR/<uuid>.<ext> and serves them at
    /files/<uuid>.<ext> (or /bisque-relay/files/<uuid>.<ext>).  Given the public
    URL we strip the path prefix and return the corresponding local Path.

    Returns None if the URL cannot be resolved to a local file.
    """
    if not attachment_url:
        return None
    # Accept both absolute URLs (https://host/files/foo.webm) and relative paths (/files/foo.webm)
    for prefix in ("/bisque-relay/files/", "/files/"):
        idx = attachment_url.find(prefix)
        if idx != -1:
            filename = attachment_url[idx + len(prefix):]
            # Strip any query-string / fragment
            filename = filename.split("?")[0].split("#")[0]
            # Safety: reject path traversal
            if filename and "/" not in filename and not filename.startswith("."):
                candidate = UPLOADS_DIR / filename
                if candidate.exists():
                    return candidate
    return None


def _inject_into_inbox(
    inbox_dir: Path,
    email: str,
    text: str,
    reply_to_id: str | None = None,
    reply_to_context: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Write a bisque message into Lobster's inbox. Returns message ID."""
    msg_id = f"bisque_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

    # Determine message type from attachments (first attachment wins)
    msg_type = "text"
    if attachments:
        first_type = attachments[0].get("type", "file")
        if first_type in ("image", "video", "file", "voice"):
            msg_type = first_type

    payload: dict[str, Any] = {
        "id": msg_id,
        "source": "bisque",
        "chat_id": email,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": msg_type,
    }
    if reply_to_id:
        payload["reply_to_id"] = reply_to_id
    if reply_to_context:
        payload["reply_to"] = reply_to_context
    if attachments:
        payload["attachments"] = attachments

    # For voice messages: resolve the attachment URL to a local file path so that
    # the MCP transcribe_audio tool (which looks for audio_file) can find the file.
    if msg_type == "voice" and attachments:
        voice_url = attachments[0].get("url", "")
        local_path = _resolve_voice_local_path(voice_url)
        if local_path:
            payload["audio_file"] = str(local_path)
            log.info("Resolved voice attachment to local path: %s", local_path)
        else:
            log.warning("Could not resolve voice URL to local path: %s", voice_url)
    dest = inbox_dir / f"{msg_id}.json"
    tmp = inbox_dir / f".{msg_id}.tmp"
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.rename(dest)
        log.info("Injected inbox message %s for %s", msg_id, email)
    except Exception as exc:
        log.error("Failed to inject inbox message: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return msg_id


# ---------------------------------------------------------------------------
# Relay server
# ---------------------------------------------------------------------------

class BisqueRelayServer:
    """Wire Protocol v2 relay server.

    Uses aiohttp for HTTP + WebSocket on the same port.
    POST /auth/exchange — bootstrap token → session token
    GET  /              — WebSocket upgrade → v2 protocol
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9101,
        token_store: TokenStore | None = None,
        event_log: EventLog | None = None,
        event_bus: EventBus | None = None,
        inbox_dir: Path | None = None,
        outbox_dir: Path | None = None,
        wire_events_dir: Path | None = None,
        sent_dir: Path | None = None,
    ) -> None:
        self.host = host
        self._requested_port = port
        self.port: int | None = port if port != 0 else None
        self._token_store = token_store if token_store is not None else TokenStore(_TOKENS_FILE)
        self._event_log = event_log if event_log is not None else EventLog()
        self._event_bus = event_bus if event_bus is not None else EventBus()
        self._inbox_dir = inbox_dir or INBOX_DIR
        self._outbox_dir = outbox_dir or BISQUE_OUTBOX_DIR
        self._wire_events_dir = wire_events_dir or WIRE_EVENTS_DIR
        self._sent_dir = sent_dir or SENT_DIR
        self._running = True
        self._clients: set[web.WebSocketResponse] = set()
        self._client_emails: dict[int, str] = {}  # ws id -> email
        # In-memory message cache for reply context lookup (id -> {text, sender})
        # Bounded to the most recent 500 messages to avoid unbounded growth.
        # P4.25: use collections.deque for O(1) FIFO eviction
        import collections
        self._message_cache: dict[str, dict[str, str]] = {}
        self._message_cache_order: collections.deque = collections.deque()
        # P3.2: track startup time for /health uptime reporting
        self._start_time: float = time.time()
        # P3.2: track last event timestamp
        self._last_event_ts: float | None = None
        # Event sources
        self._outbox_source: OutboxEventSource | None = None
        self._fs_source: FileSystemEventSource | None = None
        self._runner: web.AppRunner | None = None

    # --- HTTP handler: GET /health ---

    async def _http_health(self, request: web.Request) -> web.Response:
        """P3.4: Health check endpoint — reports relay liveness and key metrics.

        Returns JSON with: status, uptime_seconds, client_count, active_sessions,
        event_log_depth, last_event_ts, version.

        No auth required — the endpoint contains no sensitive data.
        """
        uptime = time.time() - self._start_time
        last_event = self._last_event_ts
        return web.json_response(
            {
                "status": "ok",
                "uptime_seconds": round(uptime, 1),
                "client_count": len(self._clients),
                "active_sessions": self._token_store.active_session_count,
                "event_log_depth": len(self._event_log),
                "last_event_ts": last_event,
                "version": 2,
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # --- HTTP handler: POST /auth/exchange ---

    async def _http_auth_exchange(self, request: web.Request) -> web.Response:
        """Handle bootstrap token exchange via HTTP POST.

        P3.6: Rate-limited to 5 req/s per IP (burst 10).
        """
        # P3.6: rate limit auth exchange
        remote_ip = request.remote or "unknown"
        if not _AUTH_RATE_LIMITER.is_allowed(remote_ip):
            log.warning("Rate limit hit on /auth/exchange from %s", remote_ip)
            return web.json_response(
                {"error": "Too many requests — please wait a moment"},
                status=429,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Retry-After": "1",
                },
            )

        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)

        status_code, response_body = handle_auth_exchange(body, self._token_store)

        return web.json_response(
            response_body,
            status=status_code,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def _http_options(self, request: web.Request) -> web.Response:
        """Handle CORS preflight."""
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )

    # --- HTTP handler: POST /auth/admin/token ---

    async def _http_admin_create_token(self, request: web.Request) -> web.Response:
        """Create a bootstrap token and return an encoded login token.

        Protected by the BISQUE_ADMIN_SECRET / ADMIN_SECRET environment variable.

        Request:
            POST /auth/admin/token
            Authorization: Bearer <admin_secret>
            Content-Type: application/json
            {"email": "user@example.com"}

        Response:
            {"loginToken": "<base64url-encoded>", "bootstrapToken": "<raw>", "email": "..."}

        The loginToken is base64url(JSON.stringify({"url": <relay_url>, "token": <bootstrap>})).
        Users paste this token into the bisque-chat login screen.
        """
        _cors_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }

        # P3.6: rate limit admin token creation
        remote_ip = request.remote or "unknown"
        if not _AUTH_RATE_LIMITER.is_allowed(remote_ip):
            log.warning("Rate limit hit on /auth/admin/token from %s", remote_ip)
            return web.json_response(
                {"error": "Too many requests — please wait a moment"},
                status=429,
                headers={**_cors_headers, "Retry-After": "1"},
            )

        # Check admin secret is configured
        if not _ADMIN_SECRET:
            log.error("POST /auth/admin/token called but BISQUE_ADMIN_SECRET/ADMIN_SECRET is not set")
            return web.json_response(
                {"error": "Admin secret not configured on this server"},
                status=500,
                headers=_cors_headers,
            )

        # Validate Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return web.json_response(
                {"error": "Missing or malformed Authorization header"},
                status=401,
                headers=_cors_headers,
            )

        provided_secret = auth_header[len("Bearer "):]
        if provided_secret != _ADMIN_SECRET:
            log.warning("POST /auth/admin/token: invalid admin secret from %s", request.remote)
            return web.json_response(
                {"error": "Invalid admin secret"},
                status=403,
                headers=_cors_headers,
            )

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400, headers=_cors_headers)

        email = (body.get("email") or "").strip()
        if not email or "@" not in email:
            return web.json_response(
                {"error": "Missing or invalid 'email' field"},
                status=400,
                headers=_cors_headers,
            )

        # Determine relay URL (body override > env var)
        relay_url = (body.get("relayUrl") or "").strip() or _RELAY_URL
        if not relay_url:
            return web.json_response(
                {"error": "BISQUE_RELAY_URL is not configured on this server"},
                status=500,
                headers=_cors_headers,
            )

        # Create bootstrap token (writes to disk so /auth/exchange can consume it)
        bootstrap_token = create_bootstrap_token(email, self._token_store)

        # Encode login token: base64url(JSON.stringify({url, token}))
        payload_json = json.dumps({"url": relay_url, "token": bootstrap_token}, separators=(",", ":"))
        login_token = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")

        log.info("Admin created login token for %s", email)
        return web.json_response(
            {
                "loginToken": login_token,
                "bootstrapToken": bootstrap_token,
                "email": email,
            },
            headers=_cors_headers,
        )

    async def _http_options_admin_token(self, request: web.Request) -> web.Response:
        """Handle CORS preflight for /auth/admin/token."""
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    # --- HTTP handler: POST /upload (BIS-119 voice/file upload) ---

    async def _http_upload(self, request: web.Request) -> web.Response:
        """Accept a multipart or raw binary upload and store it under a UUID filename.

        Authentication: session token required in Authorization header or ?token= query param.

        Request (multipart/form-data):
            POST /upload
            Authorization: Bearer <session_token>
            Content-Type: multipart/form-data; boundary=...
            file=<binary>

        Request (raw binary, content-type = audio/webm etc.):
            POST /upload
            Authorization: Bearer <session_token>
            Content-Type: audio/webm
            <raw bytes>

        Response:
            {"url": "/files/<uuid>.<ext>", "filename": "<uuid>.<ext>"}
        """
        _cors = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }

        # --- P3.6: Rate limit uploads per IP (2 req/s, burst 5) ---
        remote_ip = request.remote or "unknown"
        if not _UPLOAD_RATE_LIMITER.is_allowed(remote_ip):
            log.warning("Rate limit hit on /upload from %s", remote_ip)
            return web.json_response(
                {"error": "Too many requests — please wait a moment"},
                status=429,
                headers={**_cors, "Retry-After": "1"},
            )

        # --- Auth ---
        token = request.rel_url.query.get("token", "")
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer "):]
        if not token:
            return web.json_response({"error": "Unauthorized"}, status=401, headers=_cors)
        valid, email = self._token_store.validate_session(token)
        if not valid:
            return web.json_response({"error": "Invalid session token"}, status=401, headers=_cors)

        # --- Read body ---
        content_type = request.content_type or ""
        file_data: bytes
        original_ext = ".bin"

        if content_type.startswith("multipart/"):
            try:
                reader = await request.multipart()
                field = await reader.next()
                if field is None:
                    return web.json_response({"error": "Empty multipart body"}, status=400, headers=_cors)
                # Use the submitted filename extension if available
                if field.filename:
                    original_ext = Path(field.filename).suffix or ".bin"
                file_data = await field.read(decode=True)
            except Exception as exc:
                log.warning("Upload multipart read error: %s", exc)
                return web.json_response({"error": "Failed to read upload"}, status=400, headers=_cors)
        else:
            # Raw binary body — derive extension from Content-Type
            ext_from_mime = _mime_to_ext(content_type)
            if ext_from_mime:
                original_ext = ext_from_mime
            try:
                file_data = await request.read()
            except Exception as exc:
                log.warning("Upload raw read error: %s", exc)
                return web.json_response({"error": "Failed to read upload"}, status=400, headers=_cors)

        if len(file_data) == 0:
            return web.json_response({"error": "Empty file"}, status=400, headers=_cors)
        if len(file_data) > MAX_UPLOAD_BYTES:
            return web.json_response(
                {"error": f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"},
                status=413,
                headers=_cors,
            )

        # --- Store file ---
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        file_uuid = uuid.uuid4().hex
        filename = f"{file_uuid}{original_ext}"
        dest = UPLOADS_DIR / filename
        try:
            dest.write_bytes(file_data)
        except OSError as exc:
            log.error("Upload write error: %s", exc)
            return web.json_response({"error": "Storage error"}, status=500, headers=_cors)

        log.info("Stored upload %s (%d bytes) for %s", filename, len(file_data), email)
        return web.json_response(
            {"url": f"/files/{filename}", "filename": filename},
            headers=_cors,
        )

    async def _http_upload_options(self, request: web.Request) -> web.Response:
        """Handle CORS preflight for /upload."""
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    # --- HTTP handler: GET /files/{filename} (BIS-119 file serving) ---

    async def _http_serve_file(self, request: web.Request) -> web.Response:
        """Serve a previously uploaded file.

        No auth required (URLs are unguessable UUIDs). Files are served inline
        when the MIME type is audio/*, image/*, or video/*; otherwise as attachment.
        """
        filename = request.match_info.get("filename", "")
        # Safety: reject path traversal attempts
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            return web.Response(status=404)

        path = UPLOADS_DIR / filename
        if not path.exists() or not path.is_file():
            return web.Response(status=404)

        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = "application/octet-stream"

        is_inline = any(mime_type.startswith(p) for p in _INLINE_MIME_PREFIXES)
        disposition = "inline" if is_inline else f'attachment; filename="{filename}"'

        return web.Response(
            body=path.read_bytes(),
            content_type=mime_type,
            headers={
                "Content-Disposition": disposition,
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",
            },
        )

    # --- WebSocket handler ---

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections via aiohttp.

        Auth is accepted via two paths (in order of preference):

        1. Query-param auth (bisque-chat clients): the session token is passed as
           ``?token=<value>`` in the WS URL.  The connection is marked authenticated
           as soon as ``ws.onopen`` fires — no first-frame handshake required.
           On success the server sends ``{type:"hello"}`` followed by a snapshot.

        2. Frame auth (legacy / programmatic clients): the first frame must be
           ``{type:"auth", token:"<value>"}``.  On success the server sends
           ``auth_success`` followed by a snapshot.

        Both paths perform the same session-token validation via the token store.
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        remote = request.remote

        # --- Fix 1 / Fix 4: Query-param auth path ---
        qp_token = request.rel_url.query.get("token", "")
        if qp_token:
            valid, email = self._token_store.validate_session(qp_token)
            if not valid:
                log.warning("Rejected invalid query-param session from %s", remote)
                await ws.send_str(frame_auth_error("Invalid session token"))
                await ws.close(code=4401, message=b"Unauthorized")
                return ws

            self._token_store.touch_session(qp_token)
            log.info("Authenticated bisque client (query-param): %s (%s)", remote, email)
            self._clients.add(ws)
            self._client_emails[id(ws)] = email

            try:
                # Fix 4: Send hello frame immediately (clients expect this on ws.onopen)
                await ws.send_str(frame_hello())

                # Send snapshot (no last_event_id available via query-param path)
                await self._send_initial_state(ws, None)

                # Client message loop
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_client_message(ws, email, msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws.send_str(frame_error("Binary frames not supported"))
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.error("WS error for %s: %s", email, ws.exception())

            except Exception as exc:
                log.error("Error in handler for %s: %s", email, exc)
            finally:
                self._clients.discard(ws)
                self._client_emails.pop(id(ws), None)
                log.info("Bisque client disconnected: %s (%s)", remote, email)

            return ws

        # --- Legacy frame-auth path ---
        # Auth handshake: wait up to 5 seconds for auth frame
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("Auth timeout from %s", remote)
            await ws.send_str(frame_auth_error("Authentication timeout"))
            await ws.close(code=4401, message=b"Authentication timeout")
            return ws

        if msg.type == aiohttp.WSMsgType.BINARY:
            await ws.send_str(frame_error("Binary frames not supported"))
            await ws.close(code=4400, message=b"Binary not supported")
            return ws

        if msg.type != aiohttp.WSMsgType.TEXT:
            return ws

        # Parse and validate auth frame
        try:
            envelope = deserialize(msg.data)
        except ProtocolError as exc:
            await ws.send_str(frame_auth_error(str(exc)))
            await ws.close(code=4400, message=b"Invalid frame")
            return ws

        if envelope.type != "auth":
            await ws.send_str(frame_auth_error(
                f"Expected 'auth' frame, got '{envelope.type}'"
            ))
            await ws.close(code=4401, message=b"Auth required")
            return ws

        token = envelope.payload.get("token", "")
        valid, email = self._token_store.validate_session(token)
        if not valid:
            log.warning("Rejected invalid session from %s", remote)
            await ws.send_str(frame_auth_error("Invalid session token"))
            await ws.close(code=4401, message=b"Unauthorized")
            return ws

        # Touch session
        self._token_store.touch_session(token)

        log.info("Authenticated bisque client (frame-auth): %s (%s)", remote, email)
        self._clients.add(ws)
        self._client_emails[id(ws)] = email

        try:
            # Send auth_success (legacy path)
            await ws.send_str(frame_auth_success(email))

            # Replay or snapshot
            last_event_id = envelope.payload.get("last_event_id")
            await self._send_initial_state(ws, last_event_id)

            # Client message loop
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_client_message(ws, email, msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_str(frame_error("Binary frames not supported"))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.error("WS error for %s: %s", email, ws.exception())

        except Exception as exc:
            log.error("Error in handler for %s: %s", email, exc)
        finally:
            self._clients.discard(ws)
            self._client_emails.pop(id(ws), None)
            log.info("Bisque client disconnected: %s (%s)", remote, email)

        return ws

    async def _send_initial_state(self, ws: web.WebSocketResponse, last_event_id: str | None) -> None:
        """Send replay or snapshot to a newly connected client."""
        if last_event_id:
            frames = self._event_log.replay_after(last_event_id)
            if frames is not None:
                for frame in frames:
                    await ws.send_str(frame)
                return
            # else: stale ID, fall through to snapshot

        snapshot = self._build_snapshot()
        await ws.send_str(snapshot)

    def _build_snapshot(self) -> str:
        """Build a snapshot frame from current filesystem state."""
        recent_messages = self._load_recent_messages()
        last_event_id = self._event_log.get_latest_id()

        # Populate message cache from snapshot history so reply lookups work
        # immediately after a reconnect without waiting for new messages.
        for msg in recent_messages:
            self._cache_message(
                msg_id=msg["id"],
                text=msg.get("text", ""),
                sender=msg.get("role", "user"),
            )

        return frame_snapshot(
            status="idle",
            recent_messages=recent_messages,
            last_event_id=last_event_id,
        )

    # ---------------------------------------------------------------------------
    # Message cache helpers (BIS-118)
    # ---------------------------------------------------------------------------

    _MESSAGE_CACHE_LIMIT = 500

    def _cache_message(self, msg_id: str, text: str, sender: str) -> None:
        """Store a message in the bounded in-memory cache for reply lookups.

        P4.25: Uses deque.popleft() for O(1) FIFO eviction instead of list.pop(0).
        """
        if msg_id in self._message_cache:
            return
        self._message_cache[msg_id] = {"text": text, "sender": sender}
        self._message_cache_order.append(msg_id)
        # Evict oldest entries once the cache exceeds the limit
        while len(self._message_cache_order) > self._MESSAGE_CACHE_LIMIT:
            oldest = self._message_cache_order.popleft()
            self._message_cache.pop(oldest, None)

    def _resolve_reply_context(
        self, reply_to_id: str | None
    ) -> dict[str, Any] | None:
        """Return reply context dict for a given message ID, or None if unknown."""
        if not reply_to_id:
            return None
        cached = self._message_cache.get(reply_to_id)
        if cached:
            return {"id": reply_to_id, "text": cached["text"], "sender": cached["sender"]}
        return None

    def _load_recent_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        """Load recent conversation history for the snapshot frame.

        Primary path (when LOBSTER_USE_DB=1): query the messages.db SQLite
        database which is the authoritative store after the BIS-159 cutover.
        The bisque_events table holds both user (inbound) and assistant
        (outbound) messages with source='bisque'.

        Fallback path: scan JSON files in sent/ and processed/ directories.
        Used when the DB is unavailable or returns no results.

        Results are sorted chronologically so history renders oldest-first.
        """
        use_db = os.environ.get("LOBSTER_USE_DB", "0").strip() == "1"

        if use_db:
            db_messages = self._load_recent_messages_from_db(limit)
            if db_messages:
                return db_messages
            # DB returned nothing — fall through to filesystem scan
            log.info("DB returned no bisque history; falling back to filesystem scan")

        return self._load_recent_messages_from_fs(limit)

    def _load_recent_messages_from_db(self, limit: int) -> list[dict[str, Any]]:
        """Load recent bisque conversation from messages.db (SQLite).

        Reads from the bisque_events table which stores both user messages
        (source='bisque', inbound) and assistant replies (source='bisque',
        outbound).  Rows are ordered by timestamp ascending so the snapshot
        presents history in chronological order.

        Returns an empty list if the DB is unavailable or has no rows.
        """
        db_path_env = os.environ.get("LOBSTER_MESSAGES_DB", "")
        db_path = Path(db_path_env) if db_path_env else _HOME / "messages" / "messages.db"

        if not db_path.exists():
            return []

        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                # bisque_events stores user inbound AND assistant outbound messages.
                # The 'id' prefix convention is:
                #   bisque_<ts>_<hex>  — user messages (inbound)
                #   <ts>_bisque        — assistant replies (outbound)
                # We derive role from the id prefix pattern.
                rows = conn.execute(
                    """
                    SELECT id, chat_id, type, text, reply_to_id, reply_to, timestamp
                    FROM bisque_events
                    WHERE text IS NOT NULL AND text != ''
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            log.warning("Failed to load bisque history from DB: %s", exc)
            return []

        if not rows:
            return []

        messages: list[dict[str, Any]] = []
        for row in rows:
            msg_id = row["id"] or ""
            # Determine role from message ID prefix convention:
            #   IDs starting with 'bisque_' are inbound (user → Lobster)
            #   IDs ending with '_bisque' or containing '_bisque' outbound are assistant
            if msg_id.startswith("bisque_"):
                role = "user"
            else:
                role = "assistant"

            entry: dict[str, Any] = {
                "id": msg_id,
                "role": role,
                "text": row["text"],
                "timestamp": row["timestamp"] or "",
            }

            # Include reply context if present
            reply_to_raw = row["reply_to"]
            if reply_to_raw:
                try:
                    import json as _json
                    if isinstance(reply_to_raw, str):
                        entry["reply_to"] = _json.loads(reply_to_raw)
                    else:
                        entry["reply_to"] = reply_to_raw
                except (ValueError, TypeError):
                    pass

            messages.append(entry)

        # Reverse to chronological order (we fetched DESC for the LIMIT)
        messages.reverse()
        return messages

    def _load_recent_messages_from_fs(self, limit: int) -> list[dict[str, Any]]:
        """Load recent bisque conversation from JSON files (filesystem fallback).

        Combines Lobster's outgoing messages (sent/) and user messages (processed/)
        to reconstruct the full conversation. Only bisque messages are included.
        Results are sorted by timestamp so history is in chronological order.
        """
        raw: list[dict[str, Any]] = []

        # Load sent messages (Lobster → user), role = "assistant"
        try:
            for path in self._sent_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    # Only include bisque messages
                    if data.get("source") != "bisque":
                        continue
                    text = data.get("text", "")
                    if not text:
                        continue
                    raw.append({
                        "id": data.get("id", path.stem),
                        "role": "assistant",
                        "text": text,
                        "timestamp": data.get("timestamp", ""),
                        "_sort_key": data.get("timestamp", ""),
                    })
                except (json.JSONDecodeError, OSError):
                    continue
        except OSError:
            pass

        # Load processed messages (user → Lobster), role = "user"
        # Use the processed/ directory for user messages
        processed_dir = _MESSAGES / "processed"
        try:
            for path in processed_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    # Only include bisque source messages with text content
                    if data.get("source") != "bisque":
                        continue
                    # Skip subagent results and notifications (type != "text")
                    msg_type = data.get("type", "text")
                    if msg_type not in ("text", ""):
                        continue
                    text = data.get("text", "")
                    if not text:
                        continue
                    raw.append({
                        "id": data.get("id", path.stem),
                        "role": "user",
                        "text": text,
                        "timestamp": data.get("timestamp", ""),
                        "_sort_key": data.get("timestamp", ""),
                    })
                except (json.JSONDecodeError, OSError):
                    continue
        except OSError:
            pass

        # Sort by timestamp (ISO strings sort lexicographically), take most recent `limit`
        raw.sort(key=lambda m: m["_sort_key"])
        recent = raw[-limit:] if len(raw) > limit else raw

        # Strip internal sort key before returning
        return [
            {k: v for k, v in m.items() if k != "_sort_key"}
            for m in recent
        ]

    async def _handle_client_message(self, ws: web.WebSocketResponse, email: str, raw: str) -> None:
        """Dispatch a single client message."""
        try:
            envelope = deserialize(raw)
            validate_client_frame(envelope)
        except ProtocolError as exc:
            await ws.send_str(frame_error(str(exc)))
            return

        if envelope.type == "ping":
            await ws.send_str(frame_pong())

        elif envelope.type in ("send_message", "message"):
            # Fix 3: "message" is accepted as an alias for "send_message"
            text = str(envelope.payload.get("text", "")).strip()
            # BIS-120: attachments are optional; text may be empty when sending a file
            attachments: list[dict[str, Any]] | None = envelope.payload.get("attachments") or None
            if not text and not attachments:
                await ws.send_str(frame_error("Empty message"))
                return
            if len(text) > 32_000:
                await ws.send_str(frame_error("Message too long (max 32000 chars)"))
                return

            # BIS-118: Extract optional reply reference from client frame
            reply_to_id: str | None = envelope.payload.get("reply_to_id") or None
            reply_context = self._resolve_reply_context(reply_to_id)

            msg_id = _inject_into_inbox(
                self._inbox_dir,
                email,
                text,
                reply_to_id=reply_to_id,
                reply_to_context=reply_context,
                attachments=attachments,
            )

            # Cache the user's outgoing message for future reply lookups
            self._cache_message(msg_id=msg_id, text=text, sender="user")

            ack_payload: dict[str, Any] = {
                "v": 2,
                "id": str(uuid.uuid4()),
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "ack",
                "message_id": msg_id,
                "status": "received",
            }
            # Echo reply context back in the ack so the PWA can render it immediately
            if reply_context:
                ack_payload["reply_to"] = reply_context

            await ws.send_str(json.dumps(ack_payload))
            log.debug("Queued message from %s: %r", email, text[:80])

        elif envelope.type == "ack":
            pass  # Client acknowledges — no action needed

        else:
            await ws.send_str(frame_error(f"Unknown message type: {envelope.type!r}"))

    # ---------------------------------------------------------------------------
    # Typing indicator helpers (BIS-122)
    # ---------------------------------------------------------------------------

    def _make_typing_frame(self, is_typing: bool) -> str:
        """Build a serialized typing indicator frame."""
        return json.dumps({
            "v": 2,
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "typing",
            "is_typing": is_typing,
        })

    async def broadcast_typing(self, is_typing: bool) -> None:
        """Broadcast a typing indicator to all connected clients.

        Called externally when the AI starts (is_typing=True) or finishes
        (is_typing=False) generating a response.
        """
        frame = self._make_typing_frame(is_typing)
        await self._fan_out(frame)

    # --- Event bus callback ---

    async def _on_event(self, event_id: str, frame: str) -> None:
        """Called by the event bus when a new event is available."""
        self._event_log.append(event_id, frame)
        self._last_event_ts = time.time()  # P3.2: track for /health + heartbeat

        # Parse once for both BIS-118 and BIS-122 side-effects.
        target_email: str | None = None
        try:
            data = json.loads(frame)

            # P3.3: Extract target email from the frame for per-user routing.
            # Outbox frames carry chat_id (the email) so we can isolate delivery.
            target_email = data.get("chat_id") or data.get("email") or None

            if data.get("type") == "message":
                # BIS-118: cache assistant messages so reply context is available
                # for future reply_to_id lookups.
                msg_id = data.get("message_id") or data.get("id")
                text = data.get("text", "")
                role = data.get("role", "assistant")
                if msg_id and text:
                    sender = "assistant" if role == "assistant" else "user"
                    self._cache_message(msg_id=msg_id, text=text, sender=sender)

                # BIS-122: send is_typing=false before the message frame so the
                # typing indicator dismisses immediately when a reply arrives.
                # P3.3: scope the typing clear to the same user as the message.
                await self._fan_out(self._make_typing_frame(False), target_email=target_email)
        except (json.JSONDecodeError, Exception):
            pass

        await self._fan_out(frame, target_email=target_email)

    async def _fan_out(self, frame: str, target_email: str | None = None) -> None:
        """Send a frame to connected clients.

        P3.3: When target_email is provided, only clients authenticated as that
        email receive the frame (per-user isolation). When None, all clients
        receive the frame (used for global events like typing indicators that
        are scoped per-connection by the caller).

        P3.11: Send failures are logged rather than swallowed silently.
        """
        dead: set = set()
        for ws in self._clients.copy():
            # P3.3: filter by email when a target is specified
            if target_email is not None:
                ws_email = self._client_emails.get(id(ws))
                if ws_email != target_email:
                    continue
            try:
                await ws.send_str(frame)
            except Exception as exc:
                ws_email = self._client_emails.get(id(ws), "<unknown>")
                log.debug("Send failed for %s: %s", ws_email, exc)  # P3.11
                dead.add(ws)

        for ws in dead:
            self._clients.discard(ws)
            self._client_emails.pop(id(ws), None)

    # --- Server lifecycle ---

    # --- P3.13: Heartbeat logging ---

    async def _heartbeat_loop(self, interval: float = 60.0) -> None:
        """P3.13: Emit a periodic heartbeat log with client count and last event ts.

        Runs every `interval` seconds for the lifetime of the server. The log
        entry is structured to match the JsonFormatter (Op6) output format and
        is used by the observability service to detect stale relays.
        """
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            client_count = len(self._clients)
            last_ts = self._last_event_ts
            last_ts_str = (
                datetime.fromtimestamp(last_ts, timezone.utc).isoformat()
                if last_ts else "never"
            )
            log.info(
                "Heartbeat: clients=%d sessions=%d event_log_depth=%d last_event=%s",
                client_count,
                self._token_store.active_session_count,
                len(self._event_log),
                last_ts_str,
            )
            # P3.6: Purge stale rate limiter buckets to prevent unbounded memory growth
            _AUTH_RATE_LIMITER.purge_old()
            _UPLOAD_RATE_LIMITER.purge_old()

    def shutdown(self) -> None:
        """Signal the server to stop."""
        self._running = False

    async def run(self) -> None:
        """Start the HTTP/WS server and event sources."""
        for d in [self._inbox_dir, self._outbox_dir, self._wire_events_dir, self._sent_dir, UPLOADS_DIR]:
            d.mkdir(parents=True, exist_ok=True)

        log.info("Starting Lobster Bisque Relay v2 on %s:%s", self.host, self._requested_port)

        loop = asyncio.get_running_loop()
        stop = loop.create_future()

        try:
            def _on_signal():
                self._running = False
                if not stop.done():
                    stop.set_result(None)

            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            pass

        # Subscribe to event bus
        self._event_bus.subscribe(self._on_event)

        # Start event sources
        self._outbox_source = OutboxEventSource(self._outbox_dir, self._event_bus, loop)
        self._outbox_source.start()

        self._fs_source = FileSystemEventSource(self._wire_events_dir, self._event_bus, loop)
        self._fs_source.start()

        # Build aiohttp app
        app = web.Application()
        # P3.4: Health check endpoint
        app.router.add_get("/health", self._http_health)
        app.router.add_get("/bisque-relay/health", self._http_health)
        app.router.add_post("/auth/exchange", self._http_auth_exchange)
        app.router.add_route("OPTIONS", "/auth/exchange", self._http_options)
        app.router.add_post("/auth/admin/token", self._http_admin_create_token)
        app.router.add_route("OPTIONS", "/auth/admin/token", self._http_options_admin_token)
        # Nginx-prefixed aliases (nginx may forward /bisque-relay/auth/... without stripping prefix)
        app.router.add_post("/bisque-relay/auth/exchange", self._http_auth_exchange)
        app.router.add_route("OPTIONS", "/bisque-relay/auth/exchange", self._http_options)
        app.router.add_post("/bisque-relay/auth/admin/token", self._http_admin_create_token)
        app.router.add_route("OPTIONS", "/bisque-relay/auth/admin/token", self._http_options_admin_token)
        # File upload/serve (BIS-119)
        app.router.add_post("/upload", self._http_upload)
        app.router.add_route("OPTIONS", "/upload", self._http_upload_options)
        app.router.add_get("/files/{filename}", self._http_serve_file)
        # Nginx-prefixed aliases for upload/files endpoints
        app.router.add_post("/bisque-relay/upload", self._http_upload)
        app.router.add_route("OPTIONS", "/bisque-relay/upload", self._http_upload_options)
        app.router.add_get("/bisque-relay/files/{filename}", self._http_serve_file)
        app.router.add_get("/", self._ws_handler)
        # Catch-all for WS connections on any path
        app.router.add_get("/{path:.*}", self._ws_handler)

        # P3.13: Start heartbeat logging task
        asyncio.get_running_loop().create_task(self._heartbeat_loop())

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self._requested_port)
        await site.start()

        # Extract actual port
        for site_obj in self._runner.addresses:
            if isinstance(site_obj, tuple) and len(site_obj) >= 2:
                self.port = site_obj[1]
                break

        log.info("Bisque relay v2 ready on port %s", self.port)

        try:
            await stop
        except asyncio.CancelledError:
            pass

        log.info("Shutting down bisque relay v2...")
        self._event_bus.unsubscribe(self._on_event)

        if self._outbox_source:
            self._outbox_source.stop()
        if self._fs_source:
            self._fs_source.stop()

        if self._runner:
            await self._runner.cleanup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Lobster Bisque Relay Server v2")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9101, help="Bind port (default: 9101)")
    args = parser.parse_args()

    token_store = TokenStore(_TOKENS_FILE)
    event_log = EventLog()
    event_bus = EventBus()

    server = BisqueRelayServer(
        host=args.host,
        port=args.port,
        token_store=token_store,
        event_log=event_log,
        event_bus=event_bus,
    )
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
