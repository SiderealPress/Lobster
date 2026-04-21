"""
Tests for the `emit_event` MCP tool (issue #1665).

The emit_event tool lets dispatchers and subagents emit structured events
directly into the event bus. It must:
- Accept event_type, level (maps to severity), msg, and optional payload,
  task_id, chat_id
- Emit a LobsterEvent to the bus with the correct fields
- Accept "critical" as a valid level
- Reject unknown levels with a descriptive error
- Never raise or crash the caller on bus errors
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from event_bus import EventBus, LobsterEvent


class _CollectingListener:
    name = "collecting"

    def __init__(self) -> None:
        self.received: list[LobsterEvent] = []

    def accepts(self, event: LobsterEvent) -> bool:
        return True

    async def deliver(self, event: LobsterEvent) -> None:
        self.received.append(event)


def _make_bus_with_listener() -> tuple[EventBus, _CollectingListener]:
    import event_bus as _eb
    _eb._EVENT_BUS = None
    bus = _eb.get_event_bus()
    listener = _CollectingListener()
    bus.register(listener)
    return bus, listener


class TestEmitEventTool:
    """emit_event MCP tool emits structured events into the event bus."""

    def test_emit_event_reaches_bus_with_correct_fields(self):
        bus, listener = _make_bus_with_listener()
        import inbox_server
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
             patch("inbox_server.get_event_bus", return_value=bus):
            asyncio.run(inbox_server.handle_emit_event({
                "event_type": "agent.spawn",
                "level": "info",
                "msg": "Test subagent spawned",
            }))
        assert len(listener.received) == 1
        ev = listener.received[0]
        assert ev.event_type == "agent.spawn"
        assert ev.severity == "info"
        assert "Test subagent spawned" in ev.payload.get("msg", "")

    def test_emit_event_accepts_critical_level(self):
        bus, listener = _make_bus_with_listener()
        import inbox_server
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
             patch("inbox_server.get_event_bus", return_value=bus):
            asyncio.run(inbox_server.handle_emit_event({
                "event_type": "system.error",
                "level": "critical",
                "msg": "Critical failure",
            }))
        assert len(listener.received) == 1
        assert listener.received[0].severity == "critical"

    def test_emit_event_passes_optional_payload(self):
        bus, listener = _make_bus_with_listener()
        import inbox_server
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
             patch("inbox_server.get_event_bus", return_value=bus):
            asyncio.run(inbox_server.handle_emit_event({
                "event_type": "memory.write",
                "level": "debug",
                "msg": "Wrote to memory",
                "payload": {"key": "value", "size": 42},
            }))
        ev = listener.received[0]
        assert ev.payload.get("key") == "value"
        assert ev.payload.get("size") == 42

    def test_emit_event_passes_task_id_and_chat_id(self):
        bus, listener = _make_bus_with_listener()
        import inbox_server
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
             patch("inbox_server.get_event_bus", return_value=bus):
            asyncio.run(inbox_server.handle_emit_event({
                "event_type": "agent.complete",
                "level": "info",
                "msg": "Done",
                "task_id": "task-abc",
                "chat_id": 12345,
            }))
        ev = listener.received[0]
        assert ev.task_id == "task-abc"
        assert ev.chat_id == 12345

    def test_emit_event_rejects_unknown_level_with_error_response(self):
        bus, listener = _make_bus_with_listener()
        import inbox_server
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
             patch("inbox_server.get_event_bus", return_value=bus):
            result = asyncio.run(inbox_server.handle_emit_event({
                "event_type": "test.event",
                "level": "not_a_level",
                "msg": "Bad call",
            }))
        # No event should reach the bus
        assert len(listener.received) == 0
        # Result must describe the error
        assert any("not_a_level" in getattr(r, "text", "") for r in result)

    def test_emit_event_is_no_op_when_bus_unavailable(self):
        bus, listener = _make_bus_with_listener()
        import inbox_server
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", False), \
             patch("inbox_server.get_event_bus", return_value=bus):
            asyncio.run(inbox_server.handle_emit_event({
                "event_type": "agent.spawn",
                "level": "info",
                "msg": "Should not appear",
            }))
        assert len(listener.received) == 0

    def test_emit_event_tool_is_registered_in_tool_list(self):
        """emit_event must appear in the MCP server's list of available tools."""
        import inbox_server
        tools = asyncio.run(inbox_server.list_tools())
        tool_names = [t.name for t in tools]
        assert "emit_event" in tool_names, (
            "emit_event must be registered as an MCP tool"
        )
