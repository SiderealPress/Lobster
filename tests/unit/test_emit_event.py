"""
Unit tests for the _emit_event wrapper (issue #891 — callsite migration).

Verifies that:
- _emit_event() reaches the bus and produces a LobsterEvent with the right fields
- _emit_event() is a no-op when _EVENT_BUS_AVAILABLE is False (graceful fallback)
- _emit_event() never raises even when the bus or listener fails
- The event_type, severity, source, and payload fields are set correctly
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src/mcp to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from event_bus import EventBus, EventFilter, LobsterEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CollectingListener:
    """Collects every event it receives."""

    name = "collecting"

    def __init__(self) -> None:
        self.received: list[LobsterEvent] = []

    def accepts(self, event: LobsterEvent) -> bool:
        return True

    async def deliver(self, event: LobsterEvent) -> None:
        self.received.append(event)


# ---------------------------------------------------------------------------
# Tests for _emit_event
# ---------------------------------------------------------------------------

class TestEmitEvent:
    """Test the _emit_event wrapper in inbox_server."""

    def _import_emit_event(self):
        """Import _emit_event fresh from inbox_server each test to avoid state leakage."""
        import importlib
        import inbox_server
        importlib.reload(inbox_server)  # reset module-level state
        return inbox_server._emit_event, inbox_server

    def test_emit_event_sends_event_to_bus(self):
        """_emit_event() reaches the bus with correct fields."""
        import inbox_server
        # Install a fresh collecting listener on the module-level bus
        import event_bus as _eb
        _eb._EVENT_BUS = None  # reset singleton
        bus = _eb.get_event_bus()
        listener = _CollectingListener()
        bus.register(listener)

        # Patch _EVENT_BUS_AVAILABLE to True and get_event_bus to return our bus
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True):
            with patch("inbox_server.get_event_bus", return_value=bus):
                inbox_server._emit_event(
                    "hello world",
                    event_type="test.event",
                    severity="info",
                    source="test-source",
                    task_id="task-123",
                )

        assert len(listener.received) == 1
        ev = listener.received[0]
        assert ev.event_type == "test.event"
        assert ev.severity == "info"
        assert ev.source == "test-source"
        assert ev.payload == {"text": "hello world"}
        assert ev.task_id == "task-123"

    def test_emit_event_no_op_when_bus_unavailable(self):
        """_emit_event() is a no-op when _EVENT_BUS_AVAILABLE is False."""
        import inbox_server
        import event_bus as _eb
        _eb._EVENT_BUS = None
        bus = _eb.get_event_bus()
        listener = _CollectingListener()
        bus.register(listener)

        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", False):
            inbox_server._emit_event("should not reach bus", event_type="test.noop")

        assert listener.received == []

    def test_emit_event_never_raises_on_bus_error(self):
        """_emit_event() swallows exceptions from a broken bus."""
        import inbox_server

        def _bad_get_bus():
            raise RuntimeError("bus exploded")

        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True):
            with patch("inbox_server.get_event_bus", side_effect=_bad_get_bus):
                # Must not raise
                inbox_server._emit_event("text", event_type="test.event")

    def test_emit_event_defaults(self):
        """_emit_event() uses sensible defaults for optional args."""
        import inbox_server
        import event_bus as _eb
        _eb._EVENT_BUS = None
        bus = _eb.get_event_bus()
        listener = _CollectingListener()
        bus.register(listener)

        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True):
            with patch("inbox_server.get_event_bus", return_value=bus):
                inbox_server._emit_event("minimal call")

        assert len(listener.received) == 1
        ev = listener.received[0]
        assert ev.event_type == "debug.observation"
        assert ev.severity == "debug"
        assert ev.source == "inbox-server"
        assert ev.task_id is None


class TestResolveDebugConfigNoOp:
    """_resolve_debug_config must be a callable no-op (backward compat)."""

    def test_resolve_debug_config_is_callable_no_op(self):
        import inbox_server
        # Should not raise, should not set any globals
        inbox_server._resolve_debug_config()
        inbox_server._resolve_debug_config()  # idempotent
