"""Bisque test fixtures -- directories, token store, relay server, authenticated WS."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator

import aiohttp
import pytest

from bisque.auth import TokenStore
from bisque.event_bus import EventBus
from bisque.event_log import EventLog
from bisque.relay_server import BisqueRelayServer


@pytest.fixture
def bisque_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create bisque directory structure."""
    dirs = {
        "base": tmp_path,
        "inbox": tmp_path / "inbox",
        "bisque_outbox": tmp_path / "bisque-outbox",
        "wire_events": tmp_path / "wire-events",
        "sent": tmp_path / "sent",
        "tokens_file": tmp_path / "tokens.json",
    }
    for key in ["inbox", "bisque_outbox", "wire_events", "sent"]:
        dirs[key].mkdir(parents=True)

    # Write a known bootstrap token
    dirs["tokens_file"].write_text(json.dumps({
        "bootstrapTokens": {
            "test-bootstrap-token": {
                "email": "test@example.com",
                "created": "2025-01-01T00:00:00Z",
            },
        },
        "sessionTokens": {},
    }))

    return dirs


@pytest.fixture
def token_store(bisque_dirs: dict[str, Path]) -> TokenStore:
    """TokenStore with a known bootstrap token."""
    return TokenStore(bisque_dirs["tokens_file"])


@pytest.fixture
async def relay_server(
    bisque_dirs: dict[str, Path],
    token_store: TokenStore,
) -> AsyncGenerator[dict[str, Any], None]:
    """Start a v2 relay on OS-assigned port, yield info dict, then shut down."""
    event_log = EventLog()
    event_bus = EventBus()

    server = BisqueRelayServer(
        host="127.0.0.1",
        port=0,  # OS-assigned
        token_store=token_store,
        event_log=event_log,
        event_bus=event_bus,
        inbox_dir=bisque_dirs["inbox"],
        outbox_dir=bisque_dirs["bisque_outbox"],
        wire_events_dir=bisque_dirs["wire_events"],
        sent_dir=bisque_dirs["sent"],
    )

    task = asyncio.create_task(server.run())

    # Wait for server to be ready
    for _ in range(100):
        if server.port is not None and server.port != 0:
            break
        await asyncio.sleep(0.05)

    assert server.port is not None and server.port != 0, "Server failed to start"

    ws_url = f"http://127.0.0.1:{server.port}"

    yield {
        "server": server,
        "ws_url": ws_url,
        "token_store": token_store,
        "dirs": bisque_dirs,
        "event_log": event_log,
        "event_bus": event_bus,
    }

    server.shutdown()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
