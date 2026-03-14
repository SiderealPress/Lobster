"""Tests for bisque event bus -- pub/sub, outbox source, filesystem source."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bisque.event_bus import EventBus, OutboxEventSource, FileSystemEventSource


# =============================================================================
# EventBus pub/sub
# =============================================================================


class TestEventBus:
    async def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append((event_id, frame))

        bus.subscribe(handler)
        await bus.emit("evt-1", '{"type":"pong"}')
        assert len(received) == 1
        assert received[0] == ("evt-1", '{"type":"pong"}')

    async def test_multiple_subscribers(self):
        bus = EventBus()
        received_a = []
        received_b = []

        async def handler_a(event_id: str, frame: str):
            received_a.append(event_id)

        async def handler_b(event_id: str, frame: str):
            received_b.append(event_id)

        bus.subscribe(handler_a)
        bus.subscribe(handler_b)
        await bus.emit("evt-1", "frame")
        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_unsubscribe(self):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append(event_id)

        bus.subscribe(handler)
        bus.unsubscribe(handler)
        await bus.emit("evt-1", "frame")
        assert len(received) == 0

    async def test_unsubscribe_nonexistent(self):
        bus = EventBus()

        async def handler(event_id: str, frame: str):
            pass

        bus.unsubscribe(handler)  # should not raise

    async def test_exception_isolation(self):
        bus = EventBus()
        received = []

        async def bad_handler(event_id: str, frame: str):
            raise RuntimeError("boom")

        async def good_handler(event_id: str, frame: str):
            received.append(event_id)

        bus.subscribe(bad_handler)
        bus.subscribe(good_handler)
        await bus.emit("evt-1", "frame")
        # Good handler should still receive despite bad handler
        assert len(received) == 1

    async def test_subscriber_count(self):
        bus = EventBus()

        async def h1(eid, f): pass
        async def h2(eid, f): pass

        assert bus.subscriber_count == 0
        bus.subscribe(h1)
        assert bus.subscriber_count == 1
        bus.subscribe(h2)
        assert bus.subscriber_count == 2
        bus.unsubscribe(h1)
        assert bus.subscriber_count == 1

    async def test_duplicate_subscribe_ignored(self):
        bus = EventBus()

        async def handler(eid, f): pass

        bus.subscribe(handler)
        bus.subscribe(handler)
        assert bus.subscriber_count == 1


# =============================================================================
# OutboxEventSource
# =============================================================================


class TestOutboxEventSource:
    async def test_outbox_file_emitted(self, tmp_path: Path):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append((event_id, json.loads(frame)))

        bus.subscribe(handler)
        outbox_dir = tmp_path / "bisque-outbox"
        outbox_dir.mkdir()

        loop = asyncio.get_running_loop()
        source = OutboxEventSource(outbox_dir, bus, loop)
        source.start()

        try:
            # Write an outbox file
            msg = {"id": "msg-1", "source": "bisque", "chat_id": "user@test.com", "text": "Hello!", "timestamp": "2025-01-01T00:00:00Z"}
            (outbox_dir / "msg-1.json").write_text(json.dumps(msg))

            # Wait for watchdog + async processing
            for _ in range(50):
                await asyncio.sleep(0.05)
                if received:
                    break

            assert len(received) >= 1
            eid, data = received[0]
            assert data["type"] == "message"
            assert data["text"] == "Hello!"
            assert data["role"] == "assistant"

            # File should be deleted
            await asyncio.sleep(0.1)
            assert not (outbox_dir / "msg-1.json").exists()
        finally:
            source.stop()

    async def test_outbox_drain_existing(self, tmp_path: Path):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append(event_id)

        bus.subscribe(handler)
        outbox_dir = tmp_path / "bisque-outbox"
        outbox_dir.mkdir()

        # Pre-existing file
        msg = {"id": "pre-1", "text": "Pre-existing", "chat_id": "u@t.com"}
        (outbox_dir / "pre-1.json").write_text(json.dumps(msg))

        loop = asyncio.get_running_loop()
        source = OutboxEventSource(outbox_dir, bus, loop)
        source.start()

        try:
            for _ in range(50):
                await asyncio.sleep(0.05)
                if received:
                    break
            assert len(received) >= 1
        finally:
            source.stop()

    async def test_outbox_empty_text_skipped(self, tmp_path: Path):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append(event_id)

        bus.subscribe(handler)
        outbox_dir = tmp_path / "bisque-outbox"
        outbox_dir.mkdir()

        msg = {"id": "empty-1", "text": "", "chat_id": "u@t.com"}
        (outbox_dir / "empty-1.json").write_text(json.dumps(msg))

        loop = asyncio.get_running_loop()
        source = OutboxEventSource(outbox_dir, bus, loop)
        source.start()

        try:
            await asyncio.sleep(0.5)
            assert len(received) == 0
            # File should still be deleted
            assert not (outbox_dir / "empty-1.json").exists()
        finally:
            source.stop()

    async def test_outbox_non_json_ignored(self, tmp_path: Path):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append(event_id)

        bus.subscribe(handler)
        outbox_dir = tmp_path / "bisque-outbox"
        outbox_dir.mkdir()

        (outbox_dir / "readme.txt").write_text("not a json file")

        loop = asyncio.get_running_loop()
        source = OutboxEventSource(outbox_dir, bus, loop)
        source.start()

        try:
            await asyncio.sleep(0.3)
            assert len(received) == 0
        finally:
            source.stop()


# =============================================================================
# FileSystemEventSource
# =============================================================================


class TestFileSystemEventSource:
    async def test_wire_event_emitted(self, tmp_path: Path):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append((event_id, json.loads(frame)))

        bus.subscribe(handler)
        events_dir = tmp_path / "wire-events"
        events_dir.mkdir()

        loop = asyncio.get_running_loop()
        source = FileSystemEventSource(events_dir, bus, loop)
        source.start()

        try:
            event = {"type": "status", "status": "thinking", "detail": "Working on it"}
            (events_dir / "evt-1.json").write_text(json.dumps(event))

            for _ in range(50):
                await asyncio.sleep(0.05)
                if received:
                    break

            assert len(received) >= 1
            eid, data = received[0]
            assert data["type"] == "status"
            assert data["status"] == "thinking"
        finally:
            source.stop()

    async def test_wire_event_malformed_ignored(self, tmp_path: Path):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            received.append(event_id)

        bus.subscribe(handler)
        events_dir = tmp_path / "wire-events"
        events_dir.mkdir()

        # File with no "type" key
        (events_dir / "bad.json").write_text(json.dumps({"data": "no type"}))

        loop = asyncio.get_running_loop()
        source = FileSystemEventSource(events_dir, bus, loop)
        source.start()

        try:
            await asyncio.sleep(0.5)
            assert len(received) == 0
        finally:
            source.stop()

    async def test_ordering_preserved(self, tmp_path: Path):
        bus = EventBus()
        received = []

        async def handler(event_id: str, frame: str):
            data = json.loads(frame)
            received.append(data.get("seq"))

        bus.subscribe(handler)
        events_dir = tmp_path / "wire-events"
        events_dir.mkdir()

        loop = asyncio.get_running_loop()
        source = FileSystemEventSource(events_dir, bus, loop)
        source.start()

        try:
            # Write files with sequential names for ordering
            for i in range(5):
                event = {"type": "status", "status": "thinking", "seq": i}
                (events_dir / f"evt-{i:04d}.json").write_text(json.dumps(event))
                await asyncio.sleep(0.05)

            for _ in range(100):
                await asyncio.sleep(0.05)
                if len(received) >= 5:
                    break

            assert len(received) == 5
        finally:
            source.stop()
