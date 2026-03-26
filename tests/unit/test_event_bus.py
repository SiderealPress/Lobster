"""
Unit tests for the event bus infrastructure (issue #890).

Covers:
- EventFilter.accepts() — all cases: severity gate, event_type gate, wildcard,
  debug-mode gate
- EventBus.emit() fanout — correct listeners receive events, broken listeners
  do not affect others
- JsonlFileListener — writes valid JSONL to the configured path
- TelegramOutboxListener — rejects system_context events, respects debug gate
- Module-level singleton — get_event_bus() returns the same instance
- init_event_bus() idempotency — second call does not double-register listeners
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src/mcp to path so we can import without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from event_bus import (
    EventBus,
    EventFilter,
    JsonlFileListener,
    LobsterEvent,
    TelegramOutboxListener,
    get_event_bus,
    init_event_bus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(
    event_type: str = "test.event",
    severity: str = "info",
    source: str = "test",
    payload: dict | None = None,
    task_id: str | None = None,
) -> LobsterEvent:
    return LobsterEvent(
        event_type=event_type,
        severity=severity,
        source=source,
        payload=payload or {"msg": "hello"},
        task_id=task_id,
    )


class _CollectingListener:
    """Test listener that collects every event it receives."""

    name = "collecting"

    def __init__(self, event_filter: EventFilter | None = None) -> None:
        self._filter = event_filter or EventFilter()
        self.received: list[LobsterEvent] = []

    def accepts(self, event: LobsterEvent) -> bool:
        return self._filter.accepts(event)

    async def deliver(self, event: LobsterEvent) -> None:
        self.received.append(event)


class _BrokenListener:
    """Test listener that always raises in deliver()."""

    name = "broken"

    def accepts(self, event: LobsterEvent) -> bool:
        return True

    async def deliver(self, event: LobsterEvent) -> None:
        raise RuntimeError("broken listener")


# ---------------------------------------------------------------------------
# EventFilter.accepts()
# ---------------------------------------------------------------------------

class TestEventFilter:
    def test_wildcard_accepts_any_event_type(self):
        f = EventFilter(event_types={"*"}, severity={"info"})
        assert f.accepts(make_event(event_type="anything", severity="info"))

    def test_specific_type_match(self):
        f = EventFilter(event_types={"agent.spawn"}, severity={"info"})
        assert f.accepts(make_event(event_type="agent.spawn", severity="info"))

    def test_specific_type_no_match(self):
        f = EventFilter(event_types={"agent.spawn"}, severity={"info"})
        assert not f.accepts(make_event(event_type="memory.write", severity="info"))

    def test_severity_gate_blocks_wrong_severity(self):
        f = EventFilter(event_types={"*"}, severity={"error"})
        assert not f.accepts(make_event(severity="info"))

    def test_severity_gate_passes_correct_severity(self):
        f = EventFilter(event_types={"*"}, severity={"error", "warn"})
        assert f.accepts(make_event(severity="warn"))

    def test_debug_mode_gate_blocks_when_debug_off(self):
        f = EventFilter(require_debug_mode=True)
        with patch.dict(os.environ, {"LOBSTER_DEBUG": "false"}):
            assert not f.accepts(make_event())

    def test_debug_mode_gate_passes_when_debug_on(self):
        f = EventFilter(require_debug_mode=True)
        with patch.dict(os.environ, {"LOBSTER_DEBUG": "true"}):
            assert f.accepts(make_event())

    def test_no_debug_requirement_ignores_env(self):
        f = EventFilter(require_debug_mode=False)
        with patch.dict(os.environ, {"LOBSTER_DEBUG": "false"}):
            assert f.accepts(make_event())


# ---------------------------------------------------------------------------
# EventBus.emit() fanout
# ---------------------------------------------------------------------------

class TestEventBusFanout:
    def test_single_listener_receives_matching_event(self):
        bus = EventBus()
        listener = _CollectingListener(EventFilter(event_types={"*"}, severity={"info"}))
        bus.register(listener)
        event = make_event(severity="info")
        asyncio.run(bus.emit(event))
        assert listener.received == [event]

    def test_listener_does_not_receive_filtered_out_event(self):
        bus = EventBus()
        listener = _CollectingListener(EventFilter(event_types={"agent.spawn"}, severity={"info"}))
        bus.register(listener)
        asyncio.run(bus.emit(make_event(event_type="memory.write", severity="info")))
        assert listener.received == []

    def test_multiple_listeners_each_receive_matching_events(self):
        bus = EventBus()
        l1 = _CollectingListener(EventFilter(event_types={"a"}, severity={"info"}))
        l2 = _CollectingListener(EventFilter(event_types={"b"}, severity={"info"}))
        bus.register(l1)
        bus.register(l2)
        ev_a = make_event(event_type="a")
        ev_b = make_event(event_type="b")
        asyncio.run(bus.emit(ev_a))
        asyncio.run(bus.emit(ev_b))
        assert l1.received == [ev_a]
        assert l2.received == [ev_b]

    def test_broken_listener_does_not_prevent_other_listeners(self):
        bus = EventBus()
        broken = _BrokenListener()
        good = _CollectingListener()
        bus.register(broken)
        bus.register(good)
        event = make_event()
        asyncio.run(bus.emit(event))
        assert good.received == [event]

    def test_emit_sync_with_no_running_loop_delivers(self):
        """emit_sync with no running loop falls back to asyncio.run() and delivers."""
        bus = EventBus()
        listener = _CollectingListener()
        bus.register(listener)
        event = make_event()
        bus.emit_sync(event)
        assert listener.received == [event]


# ---------------------------------------------------------------------------
# JsonlFileListener
# ---------------------------------------------------------------------------

class TestJsonlFileListener:
    def test_writes_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            listener = JsonlFileListener(path=path)
            event = make_event(event_type="test.write", task_id="t1")
            asyncio.run(listener.deliver(event))
            lines = path.read_text().strip().splitlines()
            assert len(lines) == 1
            obj = json.loads(lines[0])
            assert obj["event_type"] == "test.write"
            assert obj["task_id"] == "t1"
            assert "timestamp" in obj

    def test_appends_multiple_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            listener = JsonlFileListener(path=path)
            for i in range(3):
                asyncio.run(listener.deliver(make_event(event_type=f"ev.{i}")))
            lines = path.read_text().strip().splitlines()
            assert len(lines) == 3
            types = [json.loads(l)["event_type"] for l in lines]
            assert types == ["ev.0", "ev.1", "ev.2"]

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "dir" / "events.jsonl"
            listener = JsonlFileListener(path=path)
            asyncio.run(listener.deliver(make_event()))
            assert path.exists()

    def test_accepts_all_events_by_default(self):
        listener = JsonlFileListener(path=Path("/dev/null"))
        assert listener.accepts(make_event(severity="debug", event_type="anything"))
        assert listener.accepts(make_event(severity="error", event_type="anything"))

    def test_delivery_failure_does_not_raise(self):
        """Writing to an unwritable path must not propagate an exception."""
        listener = JsonlFileListener(path=Path("/proc/nonexistent/events.jsonl"))
        # deliver() must not raise even if the write fails
        asyncio.run(listener.deliver(make_event()))


# ---------------------------------------------------------------------------
# TelegramOutboxListener
# ---------------------------------------------------------------------------

class TestTelegramOutboxListener:
    def test_rejects_system_context_event_type(self):
        listener = TelegramOutboxListener()
        event = make_event(event_type="system_context.something")
        with patch.dict(os.environ, {"LOBSTER_DEBUG": "true"}):
            assert not listener.accepts(event)

    def test_respects_debug_gate_off(self):
        listener = TelegramOutboxListener()
        event = make_event(event_type="agent.spawn")
        with patch.dict(os.environ, {"LOBSTER_DEBUG": "false"}):
            assert not listener.accepts(event)

    def test_accepts_non_system_context_in_debug_mode(self):
        listener = TelegramOutboxListener()
        event = make_event(event_type="agent.spawn")
        with patch.dict(os.environ, {"LOBSTER_DEBUG": "true"}):
            assert listener.accepts(event)

    def test_writes_outbox_file_when_chat_id_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_dir = Path(tmpdir) / "outbox"
            listener = TelegramOutboxListener(outbox_dir=outbox_dir)
            with patch.object(listener, "_resolve_owner", return_value=(12345, "telegram")):
                with patch.dict(os.environ, {"LOBSTER_DEBUG": "true"}):
                    asyncio.run(listener.deliver(make_event(event_type="test.event")))
            files = list(outbox_dir.iterdir())
            assert len(files) == 1
            content = json.loads(files[0].read_text())
            assert content["type"] == "debug_observation"
            assert content["chat_id"] == 12345

    def test_deliver_is_no_op_when_no_chat_id(self):
        """No outbox file written when owner cannot be resolved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_dir = Path(tmpdir) / "outbox"
            listener = TelegramOutboxListener(outbox_dir=outbox_dir)
            with patch.object(listener, "_resolve_owner", return_value=(None, "telegram")):
                asyncio.run(listener.deliver(make_event()))
            assert not outbox_dir.exists()


# ---------------------------------------------------------------------------
# Singleton behaviour
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_event_bus_returns_same_instance(self):
        import event_bus as _eb
        _eb._EVENT_BUS = None
        b1 = get_event_bus()
        b2 = get_event_bus()
        assert b1 is b2

    def test_init_event_bus_idempotent(self):
        """Calling init_event_bus() twice must not double-register listeners."""
        import event_bus as _eb
        _eb._EVENT_BUS = None
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            bus1 = init_event_bus(jsonl_path=path)
            listener_count_after_first = len(bus1._listeners)
            bus2 = init_event_bus(jsonl_path=path)
            assert bus1 is bus2
            assert len(bus2._listeners) == listener_count_after_first
