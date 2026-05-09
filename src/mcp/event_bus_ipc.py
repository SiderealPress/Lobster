"""
IPC bridge: Unix domain socket server that allows hooks (subprocesses) to emit
events into the in-process EventBus (issue #2003).

## Problem

Hooks (PreToolUse, PostToolUse, Stop, SessionStart) run as subprocesses — they are
shell scripts or Python scripts invoked by Claude Code, not part of the MCP server
process. This means they cannot call get_event_bus().emit(...) because the EventBus
singleton only exists in the MCP server's memory space.

## Solution

A Unix domain socket server (EventBusIPCServer) runs inside the MCP server process.
When a hook wants to emit an event, it calls emit_to_bus() (the hook-side helper,
also in this module) which connects to the socket and sends the LobsterEvent JSON.
The server receives it and calls get_event_bus().emit_sync(event), routing through
all registered listeners (MetricsListener, CriticalAlertListener, JsonlFileListener,
TelegramOutboxListener).

## Protocol

One JSON object per connection, newline-terminated. The JSON must match the
LobsterEvent field names:

    {
        "event_type": str,      # required
        "severity": str,        # required; must be in VALID_SEVERITIES
        "source": str,          # required
        "payload": dict,        # required
        "timestamp": str,       # optional ISO 8601; defaults to now()
        "task_id": str | null,  # optional
        "chat_id": int | null   # optional
    }

Invalid JSON, unknown severity, or missing required fields are silently dropped.
The hook-side emit_to_bus() is fire-and-forget — never raises.

## Socket path

Default: $LOBSTER_WORKSPACE/run/event-bus.sock
The run/ directory is created by migration 98 (upgrade.sh).

## Integration

Called from inbox_server.py main() after init_event_bus():

    from event_bus_ipc import start_ipc_server
    start_ipc_server()   # idempotent, fire-and-forget

## Functional properties

- Pure functions: _parse_event(), _default_sock_path(), emit_to_bus()
- Immutable event objects: LobsterEvent is a frozen dataclass
- Side effects isolated to: EventBusIPCServer.start() and emit_to_bus()
- All side effects in emit_to_bus() are silent (except test probing via sock_path)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Socket filename — matches the spec in issue #2003
SOCK_FILENAME = "event-bus.sock"

# Named constant for the hook-side connection timeout (spec: "0.5")
_HOOK_CONNECT_TIMEOUT_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _default_sock_path() -> Path:
    """Return the default Unix domain socket path.

    Pure function — reads only from os.environ and Path.home().
    """
    workspace = Path(
        os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
    )
    return workspace / "run" / SOCK_FILENAME


def _parse_event(line: str) -> "LobsterEvent | None":
    """Parse a JSON line into a LobsterEvent.

    Pure function — no I/O. Returns None on any parse/validation error so the
    caller can silently drop the message.

    Validation:
    - JSON must be a dict
    - event_type, severity, source must be present and non-empty strings
    - severity must be in VALID_SEVERITIES
    - payload must be a dict (defaults to {} if absent)
    - timestamp is optional; if absent, defaults to now (UTC)
    - task_id, chat_id are optional
    """
    # Late import to avoid circular imports — event_bus is already on sys.path
    # when this module is used inside the MCP server.
    try:
        from event_bus import LobsterEvent, VALID_SEVERITIES  # noqa: F401
    except ImportError:
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _sys.path.insert(0, str(_Path(__file__).parent))
            from event_bus import LobsterEvent, VALID_SEVERITIES  # noqa: F811
        except ImportError:
            return None

    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    event_type = data.get("event_type")
    severity = data.get("severity")
    source = data.get("source")

    if not event_type or not isinstance(event_type, str):
        return None
    if not severity or not isinstance(severity, str):
        return None
    if not source or not isinstance(source, str):
        return None
    if severity not in VALID_SEVERITIES:
        return None

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    # Timestamp: parse ISO string if present; fall back to now(UTC)
    ts_raw = data.get("timestamp")
    if ts_raw:
        try:
            from datetime import datetime, timezone
            # Handle both +00:00 and Z suffix
            ts_raw_norm = ts_raw.replace("Z", "+00:00")
            timestamp = datetime.fromisoformat(ts_raw_norm)
        except (ValueError, TypeError):
            timestamp = datetime.now(timezone.utc)
    else:
        timestamp = datetime.now(timezone.utc)

    task_id = data.get("task_id") or None
    chat_id = data.get("chat_id")

    try:
        return LobsterEvent(
            event_type=event_type,
            severity=severity,
            source=source,
            payload=payload,
            timestamp=timestamp,
            task_id=task_id,
            chat_id=chat_id,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# IPC server
# ---------------------------------------------------------------------------


class EventBusIPCServer:
    """
    Unix domain socket server that accepts LobsterEvent JSON from hooks and
    emits them into the in-process EventBus.

    Thread-safe: start() is idempotent. Multiple calls do nothing after the
    first. stop() removes the socket file and closes the server.

    Typical usage (inside inbox_server.py main()):

        server = EventBusIPCServer()
        await server.start()   # starts listening; idempotent

    The server is driven by the same asyncio event loop that runs the MCP server,
    so emit_sync() is called in that loop's thread via loop.create_task().
    """

    def __init__(
        self,
        sock_path: Path | None = None,
        bus: "EventBus | None" = None,
    ) -> None:
        self._sock_path = sock_path or _default_sock_path()
        self._bus = bus  # None → use module-level singleton at emit time
        self._server: asyncio.AbstractServer | None = None
        self._started = False
        self._lock = threading.Lock()

    async def start(self) -> None:
        """Start the Unix domain socket server.

        Idempotent — second call is a no-op. Creates the parent run/ directory
        if it does not exist.
        """
        with self._lock:
            if self._started:
                return
            self._started = True

        sock_path = self._sock_path
        # Remove stale socket file from a previous run (crash recovery)
        if sock_path.exists():
            try:
                sock_path.unlink()
            except OSError:
                pass

        sock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(sock_path),
            )
            self._server = server
            log.info("[event-bus-ipc] Listening on %s", sock_path)
        except Exception as exc:
            log.warning("[event-bus-ipc] Failed to start IPC server: %s", exc)
            with self._lock:
                self._started = False

    def stop(self) -> None:
        """Stop the server and remove the socket file."""
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
        try:
            if self._sock_path.exists():
                self._sock_path.unlink()
        except OSError:
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one incoming connection: read one JSON line, emit the event."""
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not line:
                return
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                return
            event = _parse_event(decoded)
            if event is None:
                return
            # Get the bus — use the injected instance for tests, module singleton in production
            if self._bus is not None:
                bus = self._bus
            else:
                from event_bus import get_event_bus
                bus = get_event_bus()
            bus.emit_sync(event)
        except Exception:
            pass  # Never let a bad connection crash the server
        finally:
            try:
                writer.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Module-level singleton server
# ---------------------------------------------------------------------------

_IPC_SERVER: EventBusIPCServer | None = None
_IPC_SERVER_LOCK = threading.Lock()


async def start_ipc_server(sock_path: Path | None = None) -> EventBusIPCServer:
    """
    Start the module-level IPC server singleton and return it.

    Idempotent: subsequent calls return the same server without restarting.
    Called from inbox_server.py main() after init_event_bus().

    Args:
        sock_path: Override the socket path (used in tests). Defaults to
                   $LOBSTER_WORKSPACE/run/event-bus.sock.
    """
    global _IPC_SERVER
    with _IPC_SERVER_LOCK:
        if _IPC_SERVER is None:
            _IPC_SERVER = EventBusIPCServer(sock_path=sock_path)

    await _IPC_SERVER.start()
    return _IPC_SERVER


# ---------------------------------------------------------------------------
# Hook-side helper — imported by hooks/*.py subprocesses
# ---------------------------------------------------------------------------


def emit_to_bus(
    event_dict: dict,
    sock_path: Path | None = None,
) -> None:
    """Fire-and-forget: send an event to the MCP server's EventBus via IPC.

    Hooks call this function to emit typed, structured events that flow through
    all registered listeners (MetricsListener, CriticalAlertListener,
    JsonlFileListener, TelegramOutboxListener).

    Pure from the caller's perspective — never raises, never blocks for long.
    The connection timeout is HOOK_CONNECT_TIMEOUT_SECONDS (0.5s) so a hung
    or absent server does not stall hook execution.

    Args:
        event_dict: Dict matching LobsterEvent.to_dict() format. Invalid dicts
                    are silently dropped on the server side.
        sock_path:  Override the socket path (used in tests). Defaults to
                    $LOBSTER_WORKSPACE/run/event-bus.sock.
    """
    try:
        import socket
        resolved_path = sock_path or _default_sock_path()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_HOOK_CONNECT_TIMEOUT_SECONDS)
            s.connect(str(resolved_path))
            payload = json.dumps(event_dict, default=str).encode("utf-8") + b"\n"
            s.sendall(payload)
    except Exception:
        pass  # Never interrupt a hook — fire and forget
