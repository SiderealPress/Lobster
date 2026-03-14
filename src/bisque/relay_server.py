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
import json
import logging
import logging.handlers
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from bisque.auth import TokenStore, handle_auth_exchange
from bisque.event_bus import EventBus, OutboxEventSource, FileSystemEventSource
from bisque.event_log import EventLog
from bisque.protocol import (
    ProtocolError,
    deserialize,
    frame_auth_error,
    frame_auth_success,
    frame_error,
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

_BISQUE_CHAT_PROJECT = _WORKSPACE / "projects" / "bisque-chat"
_TOKENS_FILE = _BISQUE_CHAT_PROJECT / "data" / "tokens.json"

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
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_file_handler)
log.addHandler(logging.StreamHandler())


# ---------------------------------------------------------------------------
# Inbox injection
# ---------------------------------------------------------------------------

def _inject_into_inbox(inbox_dir: Path, email: str, text: str) -> str:
    """Write a bisque message into Lobster's inbox. Returns message ID."""
    msg_id = f"bisque_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    payload = {
        "id": msg_id,
        "source": "bisque",
        "chat_id": email,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "text",
    }
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
        # Event sources
        self._outbox_source: OutboxEventSource | None = None
        self._fs_source: FileSystemEventSource | None = None
        self._runner: web.AppRunner | None = None

    # --- HTTP handler: POST /auth/exchange ---

    async def _http_auth_exchange(self, request: web.Request) -> web.Response:
        """Handle bootstrap token exchange via HTTP POST."""
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

    # --- WebSocket handler ---

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections via aiohttp."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        remote = request.remote

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

        log.info("Authenticated bisque client: %s (%s)", remote, email)
        self._clients.add(ws)
        self._client_emails[id(ws)] = email

        try:
            # Send auth_success
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

        return frame_snapshot(
            status="idle",
            recent_messages=recent_messages,
            last_event_id=last_event_id,
        )

    def _load_recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """Load recent messages from the sent/ directory."""
        messages = []
        try:
            sent_files = sorted(
                self._sent_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
            for path in reversed(sent_files):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    messages.append({
                        "id": data.get("id", path.stem),
                        "text": data.get("text", ""),
                        "source": data.get("source", ""),
                        "chat_id": data.get("chat_id", ""),
                        "timestamp": data.get("timestamp", ""),
                    })
                except (json.JSONDecodeError, OSError):
                    continue
        except OSError:
            pass
        return messages

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

        elif envelope.type == "send_message":
            text = str(envelope.payload.get("text", "")).strip()
            if not text:
                await ws.send_str(frame_error("Empty message text"))
                return
            if len(text) > 32_000:
                await ws.send_str(frame_error("Message too long (max 32000 chars)"))
                return

            msg_id = _inject_into_inbox(self._inbox_dir, email, text)
            ack_frame = json.dumps({
                "v": 2,
                "id": str(uuid.uuid4()),
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "ack",
                "message_id": msg_id,
                "status": "received",
            })
            await ws.send_str(ack_frame)
            log.debug("Queued message from %s: %r", email, text[:80])

        elif envelope.type == "ack":
            pass  # Client acknowledges — no action needed

        else:
            await ws.send_str(frame_error(f"Unknown message type: {envelope.type!r}"))

    # --- Event bus callback ---

    async def _on_event(self, event_id: str, frame: str) -> None:
        """Called by the event bus when a new event is available."""
        self._event_log.append(event_id, frame)
        await self._fan_out(frame)

    async def _fan_out(self, frame: str) -> None:
        """Send a frame to all connected clients."""
        dead: set = set()
        for ws in self._clients.copy():
            try:
                await ws.send_str(frame)
            except Exception:
                dead.add(ws)

        for ws in dead:
            self._clients.discard(ws)
            self._client_emails.pop(id(ws), None)

    # --- Server lifecycle ---

    def shutdown(self) -> None:
        """Signal the server to stop."""
        self._running = False

    async def run(self) -> None:
        """Start the HTTP/WS server and event sources."""
        for d in [self._inbox_dir, self._outbox_dir, self._wire_events_dir, self._sent_dir]:
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
        app.router.add_post("/auth/exchange", self._http_auth_exchange)
        app.router.add_route("OPTIONS", "/auth/exchange", self._http_options)
        app.router.add_get("/", self._ws_handler)
        # Catch-all for WS connections on any path
        app.router.add_get("/{path:.*}", self._ws_handler)

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
