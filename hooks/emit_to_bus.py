#!/usr/bin/env python3
"""
Hook-side IPC helper: emit events to the MCP server's in-process EventBus.

Hooks (PreToolUse, PostToolUse, Stop, SessionStart) run as subprocesses —
they cannot call get_event_bus().emit() because the EventBus singleton only
exists in the MCP server's memory space.

This module provides a fire-and-forget emit_to_bus() function that connects
to the MCP server's Unix domain socket (event-bus.sock) and sends a JSON
payload. The server receives it and routes it through all registered listeners
(MetricsListener, CriticalAlertListener, JsonlFileListener,
TelegramOutboxListener).

## Usage in a hook

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))  # hooks/ directory
    from emit_to_bus import emit_to_bus

    emit_to_bus({
        "event_type": "my.hook.event",
        "severity": "info",
        "source": "my-hook",
        "payload": {"detail": "something happened"},
    })

## Contract

- Never raises — fire-and-forget. A missing server or socket is silently ignored.
- The server validates the payload; invalid events are dropped on the server side.
- Connection timeout: 0.5s — never blocks a hook for long.

## Socket path

Default: $LOBSTER_WORKSPACE/run/event-bus.sock
Override: pass sock_path= argument (for tests).

## Event dict format (LobsterEvent-compatible)

{
    "event_type": str,        # required — e.g. "session.end"
    "severity": str,          # required — "debug"|"info"|"warn"|"error"|"critical"
    "source": str,            # required — component name, e.g. "dispatcher-state-stop"
    "payload": dict,          # optional — defaults to {}; must be JSON-serializable
    "timestamp": str,         # optional — ISO 8601 UTC; defaults to now() on server side
    "task_id": str | None,    # optional
    "chat_id": int | None,    # optional
}

See also: src/mcp/event_bus_ipc.py (server side).
"""

import json
import os
import socket
from pathlib import Path

# Named constant for the hook-side connection timeout (spec: 0.5s)
_HOOK_CONNECT_TIMEOUT_SECONDS = 0.5

# Socket filename — must match event_bus_ipc.SOCK_FILENAME
_SOCK_FILENAME = "event-bus.sock"


def _default_sock_path() -> Path:
    """Return the default socket path from $LOBSTER_WORKSPACE/run/event-bus.sock."""
    workspace = Path(
        os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
    )
    return workspace / "run" / _SOCK_FILENAME


def emit_to_bus(
    event_dict: dict,
    sock_path: Path | None = None,
) -> None:
    """Fire-and-forget: send an event to the MCP server's EventBus via IPC.

    Hooks call this function to emit typed, structured events that flow through
    all registered listeners (MetricsListener, CriticalAlertListener,
    JsonlFileListener, TelegramOutboxListener) — exactly the same way
    in-process code does via get_event_bus().emit_sync().

    Never raises. Never blocks for longer than _HOOK_CONNECT_TIMEOUT_SECONDS.

    Args:
        event_dict: Dict matching LobsterEvent.to_dict() format. Invalid dicts
                    are silently dropped on the server side.
        sock_path:  Override the socket path (used in tests). Defaults to
                    $LOBSTER_WORKSPACE/run/event-bus.sock.
    """
    try:
        resolved_path = sock_path or _default_sock_path()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_HOOK_CONNECT_TIMEOUT_SECONDS)
            s.connect(str(resolved_path))
            payload = json.dumps(event_dict, default=str).encode("utf-8") + b"\n"
            s.sendall(payload)
    except Exception:
        pass  # Never interrupt a hook — fire and forget
