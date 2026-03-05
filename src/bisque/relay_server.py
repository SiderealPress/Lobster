#!/usr/bin/env python3
"""
Lobster Bisque Relay Server

A WebSocket relay that bridges bisque-chat (browser PWA) with the Lobster
message queue. Authenticates connections using session tokens from the
bisque-chat token store, injects incoming messages into Lobster's inbox,
and pushes outgoing replies to connected clients by watching the bisque-outbox.

Protocol:
    Client → Server: {"type": "message", "text": "<user message>"}
    Client → Server: {"type": "ping"}
    Server → Client: {"type": "hello", "email": "<user email>"}
    Server → Client: {"type": "message", "text": "<lobster reply>", "id": "<id>"}
    Server → Client: {"type": "pong"}
    Server → Client: {"type": "error", "message": "<description>"}

Usage:
    python3 relay_server.py [--host 0.0.0.0] [--port 9101]
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import serve

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))

INBOX_DIR = _MESSAGES / "inbox"
BISQUE_OUTBOX_DIR = _MESSAGES / "bisque-outbox"

# Token store for bisque-chat session tokens (managed by bisque-chat Next.js app)
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
# Directory setup
# ---------------------------------------------------------------------------

for _d in [INBOX_DIR, BISQUE_OUTBOX_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Token validation (pure functions operating on immutable store snapshots)
# ---------------------------------------------------------------------------

def _read_token_store() -> dict:
    """Read the bisque-chat token store from disk.

    Returns an empty store structure if the file is missing or corrupt.
    This is a pure read — no side effects beyond I/O.
    """
    empty: dict = {"bootstrapTokens": {}, "sessionTokens": {}}
    try:
        raw = _TOKENS_FILE.read_text(encoding="utf-8")
        return json.loads(raw)
    except FileNotFoundError:
        log.warning("Token store not found at %s — all auth will fail", _TOKENS_FILE)
        return empty
    except json.JSONDecodeError as exc:
        log.error("Token store is corrupt: %s", exc)
        return empty


def _validate_session_token(token: str) -> tuple[bool, str]:
    """Validate a session token and return (valid, email).

    Returns (True, email) on success, (False, "") on failure.
    Pure: only reads from disk, no mutations.
    """
    if not token or len(token) > 256:
        return False, ""
    store = _read_token_store()
    session_tokens = store.get("sessionTokens", {})
    record = session_tokens.get(token)
    if not record:
        return False, ""
    email = record.get("email", "")
    return bool(email), email


# ---------------------------------------------------------------------------
# Message frame builders (pure — return JSON strings)
# ---------------------------------------------------------------------------

def _frame(msg_type: str, **fields) -> str:
    """Build a JSON protocol frame."""
    return json.dumps(
        {"type": msg_type, "timestamp": datetime.now(tz=timezone.utc).isoformat(), **fields},
        default=str,
    )


def _frame_hello(email: str) -> str:
    return _frame("hello", email=email, server="lobster-bisque-relay")


def _frame_pong() -> str:
    return _frame("pong")


def _frame_error(message: str) -> str:
    return _frame("error", message=message)


def _frame_message(text: str, msg_id: str) -> str:
    return _frame("message", text=text, id=msg_id)


# ---------------------------------------------------------------------------
# Inbox injection (side effect — isolated here)
# ---------------------------------------------------------------------------

def _inject_into_inbox(email: str, text: str) -> str:
    """Write a bisque message into Lobster's inbox as a JSON file.

    Returns the generated message ID.
    Format mirrors Telegram messages so the main dispatcher handles it normally.
    """
    msg_id = f"bisque_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    payload = {
        "id": msg_id,
        "source": "bisque",
        "chat_id": email,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "text",
    }
    dest = INBOX_DIR / f"{msg_id}.json"
    # Atomic write: temp → rename (prevents partial reads by watchdog)
    tmp = INBOX_DIR / f".{msg_id}.tmp"
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
# URL token extraction
# ---------------------------------------------------------------------------

def _extract_token(path: str) -> str | None:
    """Parse ?token=<value> from the WebSocket request path."""
    try:
        params = parse_qs(urlparse(path).query)
        tokens = params.get("token")
        return tokens[0] if tokens else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Relay server
# ---------------------------------------------------------------------------

class BisqueRelayServer:
    """WebSocket relay between bisque-chat browser clients and Lobster inbox.

    Each connection is authenticated with a session token. Authenticated
    connections are stored keyed by email so outbox messages can be routed
    to the correct client.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9101):
        self.host = host
        self.port = port
        self._running = True
        # email -> set of websocket connections (supports multiple tabs)
        self._clients: dict[str, set] = {}

    # --- Connection registry (pure helpers) ---

    def _register(self, email: str, ws) -> None:
        self._clients.setdefault(email, set()).add(ws)

    def _unregister(self, email: str, ws) -> None:
        connections = self._clients.get(email, set())
        connections.discard(ws)
        if not connections:
            self._clients.pop(email, None)

    # --- WebSocket handler ---

    async def handler(self, websocket) -> None:
        """Handle a single client connection lifecycle."""
        remote = websocket.remote_address
        request_path = getattr(websocket.request, "path", "") if websocket.request else ""

        # Authenticate
        token = _extract_token(request_path)
        valid, email = _validate_session_token(token or "")

        if not valid:
            log.warning("Rejected unauthenticated connection from %s (path=%r)", remote, request_path)
            await websocket.send(_frame_error("Unauthorized: invalid or missing session token"))
            await websocket.close(code=4401, reason="Unauthorized")
            return

        log.info("Authenticated bisque client: %s (%s)", remote, email)
        self._register(email, websocket)

        try:
            await websocket.send(_frame_hello(email))

            async for raw in websocket:
                await self._handle_client_message(websocket, email, raw)

        except websockets.ConnectionClosed:
            log.info("Bisque client disconnected: %s (%s)", remote, email)
        except Exception as exc:
            log.error("Error in handler for %s: %s", email, exc)
        finally:
            self._unregister(email, websocket)

    async def _handle_client_message(self, websocket, email: str, raw: str) -> None:
        """Dispatch a single client message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(_frame_error("Invalid JSON"))
            return

        msg_type = msg.get("type", "")

        if msg_type == "ping":
            await websocket.send(_frame_pong())

        elif msg_type == "message":
            text = str(msg.get("text", "")).strip()
            if not text:
                await websocket.send(_frame_error("Empty message text"))
                return
            if len(text) > 32_000:
                await websocket.send(_frame_error("Message too long (max 32000 chars)"))
                return
            _inject_into_inbox(email, text)
            # Acknowledgement (client will show the reply when it arrives via outbox)
            log.debug("Queued message from %s: %r", email, text[:80])

        else:
            await websocket.send(_frame_error(f"Unknown message type: {msg_type!r}"))

    # --- Outbox watcher ---

    async def outbox_watcher(self) -> None:
        """Poll bisque-outbox/ and push pending messages to connected clients.

        Uses polling rather than inotify so it works in all environments
        without additional dependencies. Poll interval is intentionally short
        (0.25s) for near-real-time delivery.
        """
        seen: set[str] = set()

        while self._running:
            await asyncio.sleep(0.25)
            try:
                for path in sorted(BISQUE_OUTBOX_DIR.glob("*.json")):
                    if path.name in seen:
                        continue
                    seen.add(path.name)
                    await self._deliver_outbox_file(path)
            except Exception as exc:
                log.error("Outbox watcher error: %s", exc)

    async def _deliver_outbox_file(self, path: Path) -> None:
        """Read an outbox file, push to the target client, then remove it."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("Could not read outbox file %s: %s", path, exc)
            return

        email = str(payload.get("chat_id", ""))
        text = str(payload.get("text", ""))
        msg_id = str(payload.get("id", path.stem))

        if not email or not text:
            log.warning("Skipping malformed outbox file %s", path.name)
            path.unlink(missing_ok=True)
            return

        connections = self._clients.get(email, set())
        if not connections:
            # Client not connected — leave file for retry (up to 60s)
            age = time.time() - path.stat().st_mtime
            if age > 60:
                log.warning("Dropping stale outbox message for %s (age=%.0fs)", email, age)
                path.unlink(missing_ok=True)
            return

        frame = _frame_message(text, msg_id)
        delivered = False
        dead: set = set()

        for ws in connections.copy():
            try:
                await ws.send(frame)
                delivered = True
            except websockets.ConnectionClosed:
                dead.add(ws)
            except Exception as exc:
                log.error("Error sending to %s: %s", email, exc)

        for ws in dead:
            self._unregister(email, ws)

        if delivered:
            path.unlink(missing_ok=True)
            log.info("Delivered outbox message %s to %s", msg_id, email)

    # --- Server lifecycle ---

    async def run(self) -> None:
        """Start the WebSocket server and outbox watcher."""
        log.info(
            "Starting Lobster Bisque Relay on ws://%s:%d",
            self.host, self.port,
        )

        loop = asyncio.get_running_loop()
        stop = loop.create_future()

        def _on_signal():
            self._running = False
            if not stop.done():
                stop.set_result(None)

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal)

        watcher_task = asyncio.create_task(self.outbox_watcher())

        async with serve(self.handler, self.host, self.port) as server:
            log.info("Bisque relay ready. Token store: %s", _TOKENS_FILE)
            await stop

        log.info("Shutting down bisque relay...")
        self._running = False
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Lobster Bisque Relay Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9101, help="Bind port (default: 9101)")
    args = parser.parse_args()

    server = BisqueRelayServer(host=args.host, port=args.port)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
