#!/usr/bin/env python3
"""
Lobster Dashboard WebSocket Server

Streams real-time system and Lobster instance information to connected clients.
Uses a JSON-based protocol with typed message frames.

Usage:
    python3 server.py [--host 0.0.0.0] [--port 9100] [--interval 3]

Protocol:
    On connect, the server sends a "snapshot" message with full state.
    Subsequently, it sends "update" messages at the configured interval.
    Clients can send "ping" messages and will receive "pong" responses.
    Clients can send "request_snapshot" to force a full state dump.
"""

import argparse
import asyncio
import json
import logging
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

from collectors import collect_full_snapshot

log = logging.getLogger("lobster-dashboard")

# Protocol version
PROTOCOL_VERSION = "1.0.0"

# Token storage
_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_TOKEN_FILE = _MESSAGES / "config" / "dashboard-token"


def _load_or_create_token() -> str:
    """Load the dashboard token from disk, creating it if absent.

    The token is a UUID4 generated once and persisted so that bisque-computer
    clients can reconnect across server restarts without re-pairing.
    """
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _TOKEN_FILE.exists():
        token = _TOKEN_FILE.read_text().strip()
        if token:
            log.info("Loaded existing dashboard token from %s", _TOKEN_FILE)
            return token
    token = str(uuid.uuid4())
    _TOKEN_FILE.write_text(token)
    log.info("Generated new dashboard token, saved to %s", _TOKEN_FILE)
    return token


def _extract_token_from_path(path: str) -> str | None:
    """Parse the ?token=<UUID> query parameter from a WebSocket request path."""
    try:
        parsed = urlparse(path)
        params = parse_qs(parsed.query)
        tokens = params.get("token")
        if tokens:
            return tokens[0]
    except Exception:
        pass
    return None


def _make_frame(msg_type: str, data: dict | None = None) -> str:
    """Build a JSON message frame conforming to the Lobster Dashboard Protocol."""
    frame = {
        "version": PROTOCOL_VERSION,
        "type": msg_type,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    if data is not None:
        frame["data"] = data
    return json.dumps(frame, default=str)


def _make_snapshot() -> str:
    """Build a full snapshot frame."""
    return _make_frame("snapshot", collect_full_snapshot())


def _make_update() -> str:
    """Build an update frame (same structure as snapshot for now;
    delta-only optimization can be added later)."""
    return _make_frame("update", collect_full_snapshot())


def _make_pong() -> str:
    """Build a pong response frame."""
    return _make_frame("pong")


def _make_error(message: str) -> str:
    """Build an error frame."""
    return _make_frame("error", {"message": message})


def _make_hello() -> str:
    """Build a hello frame sent on connection."""
    return _make_frame("hello", {
        "server": "lobster-dashboard",
        "protocol_version": PROTOCOL_VERSION,
    })


class DashboardServer:
    """WebSocket server that streams Lobster dashboard data."""

    def __init__(self, host: str = "0.0.0.0", port: int = 9100, interval: float = 3.0):
        self.host = host
        self.port = port
        self.interval = interval
        self.clients: set = set()
        self._running = True
        self._token: str = _load_or_create_token()

    async def handler(self, websocket) -> None:
        """Handle a single client connection."""
        remote = websocket.remote_address

        # --- Token authentication ---
        request_path = getattr(websocket.request, "path", "") if websocket.request else ""
        client_token = _extract_token_from_path(request_path)

        if client_token != self._token:
            log.warning(
                "Client %s rejected: invalid or missing token (path=%r)",
                remote, request_path,
            )
            await websocket.send(_make_error("Unauthorized: invalid or missing token"))
            await websocket.close(code=4401, reason="Unauthorized")
            return

        log.info("Client authenticated: %s", remote)

        self.clients.add(websocket)

        try:
            # Send hello + initial snapshot
            await websocket.send(_make_hello())
            await websocket.send(_make_snapshot())

            # Listen for client messages while streaming updates
            async for raw_message in websocket:
                try:
                    msg = json.loads(raw_message)
                    msg_type = msg.get("type", "")

                    if msg_type == "ping":
                        await websocket.send(_make_pong())
                    elif msg_type == "request_snapshot":
                        await websocket.send(_make_snapshot())
                    else:
                        await websocket.send(
                            _make_error(f"Unknown message type: {msg_type}")
                        )
                except json.JSONDecodeError:
                    await websocket.send(_make_error("Invalid JSON"))

        except websockets.ConnectionClosed:
            log.info("Client disconnected: %s", remote)
        finally:
            self.clients.discard(websocket)

    async def broadcast_loop(self) -> None:
        """Periodically broadcast updates to all connected clients."""
        while self._running:
            await asyncio.sleep(self.interval)
            if not self.clients:
                continue

            update = _make_update()
            # Send to all connected clients, removing any that fail
            disconnected = set()
            for client in self.clients.copy():
                try:
                    await client.send(update)
                except websockets.ConnectionClosed:
                    disconnected.add(client)
            self.clients -= disconnected

    async def run(self) -> None:
        """Start the WebSocket server."""
        log.info(
            "Starting Lobster Dashboard server on ws://%s:%d (interval=%ss)",
            self.host, self.port, self.interval,
        )

        # Handle shutdown signals
        loop = asyncio.get_running_loop()
        stop = loop.create_future()

        def _signal_handler():
            self._running = False
            if not stop.done():
                stop.set_result(None)

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)

        async with serve(self.handler, self.host, self.port) as server:
            # Start the broadcast loop
            broadcast_task = asyncio.create_task(self.broadcast_loop())

            log.info("Dashboard server ready. Listening for connections...")

            # Wait for shutdown signal
            await stop

            log.info("Shutting down...")
            broadcast_task.cancel()
            try:
                await broadcast_task
            except asyncio.CancelledError:
                pass

        log.info("Server stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lobster Dashboard WebSocket Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9100, help="Port (default: 9100)")
    parser.add_argument("--interval", type=float, default=3.0, help="Update interval in seconds (default: 3.0)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    server = DashboardServer(host=args.host, port=args.port, interval=args.interval)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
