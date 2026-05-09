"""
Tests for the IPC bridge that allows hooks (subprocesses) to emit events into
the in-process EventBus (issue #2003).

Covers:
- IPC server: starts and accepts connections on a Unix domain socket
- IPC server: deserializes LobsterEvent JSON and calls get_event_bus().emit()
- IPC server: malformed JSON is silently dropped (hook never blocked)
- IPC server: unknown severity is silently dropped
- IPC server: socket file is removed on cleanup
- emit_to_bus (hook-side helper): sends JSON to the socket
- emit_to_bus: is fire-and-forget — never raises even if server is unavailable
- emit_to_bus: is fire-and-forget — never raises when socket is missing
- EventBusIPCServer: start() is idempotent (second call is a no-op)
- Integration: event sent via socket reaches listeners registered on the bus
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Add src/mcp to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from event_bus import EventBus, EventFilter, LobsterEvent, get_event_bus, init_event_bus
import event_bus as _event_bus_module

# Import the IPC bridge module under test
from event_bus_ipc import EventBusIPCServer, emit_to_bus, SOCK_FILENAME


# ---------------------------------------------------------------------------
# Named constants (spec-derived)
# ---------------------------------------------------------------------------

# The socket filename as defined in the spec
EXPECTED_SOCK_FILENAME = "event-bus.sock"

# Timeout for socket connection in the hook helper (spec: 0.5s)
EMIT_TO_BUS_TIMEOUT_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event_dict(
    event_type: str = "test.event",
    severity: str = "info",
    source: str = "test-hook",
    payload: dict | None = None,
) -> dict:
    """Return a dict matching LobsterEvent.to_dict() format."""
    return {
        "event_type": event_type,
        "severity": severity,
        "source": source,
        "payload": payload or {"msg": "hello"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": None,
        "chat_id": None,
    }


class _CollectingListener:
    """Collects every event it receives, for assertion in tests."""

    name = "collecting"

    def __init__(self) -> None:
        self.received: list[LobsterEvent] = []
        self._lock = threading.Lock()

    def accepts(self, event: LobsterEvent) -> bool:
        return True

    async def deliver(self, event: LobsterEvent) -> None:
        with self._lock:
            self.received.append(event)

    def wait_for_event(self, timeout: float = 2.0) -> LobsterEvent | None:
        """Poll until an event is received or timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.received:
                    return self.received[0]
            time.sleep(0.05)
        return None


# ---------------------------------------------------------------------------
# SOCK_FILENAME constant
# ---------------------------------------------------------------------------

class TestSockFilenameConstant:
    def test_sock_filename_matches_spec(self):
        """The socket filename must match the spec exactly."""
        assert SOCK_FILENAME == EXPECTED_SOCK_FILENAME


# ---------------------------------------------------------------------------
# EventBusIPCServer: basic behavior
# ---------------------------------------------------------------------------

class TestEventBusIPCServerBasic:
    def test_start_creates_socket_file(self):
        """start() must create the Unix domain socket at the specified path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus = EventBus()
            server = EventBusIPCServer(sock_path=sock_path, bus=bus)

            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            try:
                fut = asyncio.run_coroutine_threadsafe(server.start(), loop)
                fut.result(timeout=2.0)
                assert sock_path.exists(), "Socket file must be created by start()"
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()

    def test_start_is_idempotent(self):
        """Calling start() twice does not raise and does not create two servers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus = EventBus()
            server = EventBusIPCServer(sock_path=sock_path, bus=bus)

            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            try:
                fut1 = asyncio.run_coroutine_threadsafe(server.start(), loop)
                fut1.result(timeout=2.0)
                # Second start — must not raise
                fut2 = asyncio.run_coroutine_threadsafe(server.start(), loop)
                fut2.result(timeout=2.0)
                assert sock_path.exists()
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()

    def test_stop_removes_socket_file(self):
        """stop() must remove the socket file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus = EventBus()
            server = EventBusIPCServer(sock_path=sock_path, bus=bus)

            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            try:
                fut = asyncio.run_coroutine_threadsafe(server.start(), loop)
                fut.result(timeout=2.0)
                assert sock_path.exists()
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()

            assert not sock_path.exists(), "stop() must remove the socket file"


# ---------------------------------------------------------------------------
# EventBusIPCServer: event routing
# ---------------------------------------------------------------------------

class TestEventBusIPCServerRouting:
    def _run_server_with_listener(self, sock_path: Path) -> tuple[EventBus, _CollectingListener, asyncio.AbstractEventLoop, EventBusIPCServer]:
        """Start an IPC server and return the bus, listener, loop, and server."""
        bus = EventBus()
        listener = _CollectingListener()
        bus.register(listener)
        server = EventBusIPCServer(sock_path=sock_path, bus=bus)

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        fut = asyncio.run_coroutine_threadsafe(server.start(), loop)
        fut.result(timeout=2.0)

        return bus, listener, loop, server

    def test_event_received_via_socket_reaches_listener(self):
        """An event sent through the IPC socket must be delivered to bus listeners."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus, listener, loop, server = self._run_server_with_listener(sock_path)
            try:
                event_dict = make_event_dict(event_type="hook.test", source="test-hook")
                emit_to_bus(event_dict, sock_path=sock_path)

                received = listener.wait_for_event(timeout=2.0)
                assert received is not None, "Event must arrive at listener within 2s"
                assert received.event_type == "hook.test"
                assert received.source == "test-hook"
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()

    def test_event_preserves_all_fields(self):
        """Fields from the hook payload are preserved: event_type, severity, source, payload, task_id, chat_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus, listener, loop, server = self._run_server_with_listener(sock_path)
            try:
                event_dict = {
                    "event_type": "session.end",
                    "severity": "info",
                    "source": "dispatcher-state-stop",
                    "payload": {"context_pct": 42.5, "in_flight_agents": []},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "task_id": "task-123",
                    "chat_id": 99999,
                }
                emit_to_bus(event_dict, sock_path=sock_path)

                received = listener.wait_for_event(timeout=2.0)
                assert received is not None
                assert received.event_type == "session.end"
                assert received.severity == "info"
                assert received.source == "dispatcher-state-stop"
                assert received.payload["context_pct"] == 42.5
                assert received.task_id == "task-123"
                assert received.chat_id == 99999
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()

    def test_multiple_events_all_arrive(self):
        """Multiple events sent sequentially all reach the listener."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus, listener, loop, server = self._run_server_with_listener(sock_path)
            try:
                for i in range(3):
                    emit_to_bus(make_event_dict(event_type=f"hook.batch.{i}"), sock_path=sock_path)

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    with listener._lock:
                        if len(listener.received) >= 3:
                            break
                    time.sleep(0.05)

                with listener._lock:
                    types = [e.event_type for e in listener.received]
                assert types == ["hook.batch.0", "hook.batch.1", "hook.batch.2"]
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()


# ---------------------------------------------------------------------------
# EventBusIPCServer: malformed input handling
# ---------------------------------------------------------------------------

class TestEventBusIPCServerMalformedInput:
    def test_malformed_json_is_silently_dropped(self):
        """Malformed JSON sent to the socket must be silently dropped, not crash the server."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus = EventBus()
            listener = _CollectingListener()
            bus.register(listener)
            server = EventBusIPCServer(sock_path=sock_path, bus=bus)

            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            fut = asyncio.run_coroutine_threadsafe(server.start(), loop)
            fut.result(timeout=2.0)

            try:
                import socket as _sock
                with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(str(sock_path))
                    s.sendall(b"not-valid-json\n")

                # Server must remain responsive after malformed input
                time.sleep(0.2)

                # Send a valid event after — server must still be running
                emit_to_bus(make_event_dict(event_type="after.malformed"), sock_path=sock_path)
                received = listener.wait_for_event(timeout=2.0)
                assert received is not None, "Server must still accept events after malformed input"
                assert received.event_type == "after.malformed"
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()

    def test_unknown_severity_is_silently_dropped(self):
        """Event dict with unknown severity is silently dropped, not crash the server."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus = EventBus()
            listener = _CollectingListener()
            bus.register(listener)
            server = EventBusIPCServer(sock_path=sock_path, bus=bus)

            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            fut = asyncio.run_coroutine_threadsafe(server.start(), loop)
            fut.result(timeout=2.0)

            try:
                bad_event = make_event_dict(severity="NOTAREAL")
                emit_to_bus(bad_event, sock_path=sock_path)
                time.sleep(0.3)

                # Listener must not have received anything
                with listener._lock:
                    assert len(listener.received) == 0, "Unknown severity must be dropped silently"
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()

    def test_missing_required_fields_dropped_silently(self):
        """Event dict missing required fields (event_type, severity, source) is silently dropped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "event-bus.sock"
            bus = EventBus()
            listener = _CollectingListener()
            bus.register(listener)
            server = EventBusIPCServer(sock_path=sock_path, bus=bus)

            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            fut = asyncio.run_coroutine_threadsafe(server.start(), loop)
            fut.result(timeout=2.0)

            try:
                import socket as _sock
                # Missing event_type
                with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(str(sock_path))
                    s.sendall(json.dumps({"severity": "info", "source": "test"}).encode() + b"\n")
                time.sleep(0.3)

                with listener._lock:
                    assert len(listener.received) == 0, "Missing required fields must be dropped silently"
            finally:
                loop.call_soon_threadsafe(loop.stop)
                server.stop()


# ---------------------------------------------------------------------------
# emit_to_bus: hook-side helper
# ---------------------------------------------------------------------------

class TestEmitToBusHelper:
    def test_emit_to_bus_does_not_raise_when_server_unavailable(self):
        """emit_to_bus must never raise, even if the server is not running."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "nonexistent.sock"
            # Must not raise — fire-and-forget
            emit_to_bus(make_event_dict(), sock_path=sock_path)

    def test_emit_to_bus_does_not_raise_when_socket_missing(self):
        """emit_to_bus must never raise when the socket file does not exist."""
        emit_to_bus(make_event_dict(), sock_path=Path("/tmp/__no_such_socket__.sock"))

    def test_emit_to_bus_does_not_raise_on_bad_payload(self):
        """emit_to_bus must never raise even when given a non-serializable payload."""
        # Pass an object that isn't JSON-serializable
        try:
            emit_to_bus({"event_type": "x", "not_serializable": object()})
        except Exception as exc:
            pytest.fail(f"emit_to_bus raised unexpectedly: {exc}")

    def test_emit_to_bus_timeout_is_short(self):
        """emit_to_bus must time out quickly (<=1s) when server is unreachable."""
        import socket as _sock

        # Create a socket that accepts but never reads — simulates a hung server.
        # The client connect() will succeed but sendall() hangs.
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "hung.sock"

            # Create a passive listener (never reads) to simulate a hung server
            server_sock = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            server_sock.bind(str(sock_path))
            server_sock.listen(1)
            server_sock.settimeout(5.0)

            try:
                start = time.time()
                emit_to_bus(make_event_dict(), sock_path=sock_path)
                elapsed = time.time() - start
                assert elapsed < 2.0, f"emit_to_bus took too long ({elapsed:.2f}s) — should time out quickly"
            finally:
                server_sock.close()

    def test_emit_to_bus_sends_valid_json_to_socket(self):
        """emit_to_bus sends a valid JSON line that the server can parse."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "test.sock"
            received_data = []

            import socket as _sock

            server_sock = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            server_sock.bind(str(sock_path))
            server_sock.listen(1)
            server_sock.settimeout(2.0)

            def _accept():
                try:
                    conn, _ = server_sock.accept()
                    conn.settimeout(1.0)
                    data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    received_data.append(data)
                    conn.close()
                except Exception:
                    pass

            t = threading.Thread(target=_accept, daemon=True)
            t.start()

            event_dict = make_event_dict(event_type="probe.send", source="test")
            emit_to_bus(event_dict, sock_path=sock_path)
            t.join(timeout=2.0)
            server_sock.close()

            assert received_data, "emit_to_bus must have sent data to the socket"
            line = received_data[0].decode("utf-8").strip()
            parsed = json.loads(line)
            assert parsed["event_type"] == "probe.send"
            assert parsed["source"] == "test"


# ---------------------------------------------------------------------------
# Default sock path resolution
# ---------------------------------------------------------------------------

class TestDefaultSockPath:
    def test_default_sock_path_uses_lobster_workspace(self):
        """The default socket path must be under $LOBSTER_WORKSPACE/run/."""
        from event_bus_ipc import _default_sock_path

        with patch.dict(os.environ, {"LOBSTER_WORKSPACE": "/tmp/test-workspace"}):
            path = _default_sock_path()
        assert str(path) == "/tmp/test-workspace/run/event-bus.sock"

    def test_default_sock_path_falls_back_to_home(self):
        """Without LOBSTER_WORKSPACE, falls back to ~/lobster-workspace/run/."""
        from event_bus_ipc import _default_sock_path

        env_without_workspace = {k: v for k, v in os.environ.items() if k != "LOBSTER_WORKSPACE"}
        with patch.dict(os.environ, env_without_workspace, clear=True):
            path = _default_sock_path()
        expected = Path.home() / "lobster-workspace" / "run" / "event-bus.sock"
        assert path == expected
