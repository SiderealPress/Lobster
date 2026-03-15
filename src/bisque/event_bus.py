"""Bisque Wire Protocol v2 -- event bus with filesystem watchers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Coroutine

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from bisque.protocol import frame_message, make_envelope, serialize, FrameType

log = logging.getLogger("lobster-bisque-relay")


class EventBus:
    """Simple pub/sub event bus for v2 frames.

    Subscribers receive (event_id, serialized_frame) pairs.
    Exceptions in individual subscribers are logged but do not affect others.
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[str, str], Coroutine]] = []

    def subscribe(self, callback: Callable[[str, str], Coroutine]) -> None:
        """Add an async callback: async def cb(event_id: str, frame: str)."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, str], Coroutine]) -> None:
        """Remove a subscriber."""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def emit(self, event_id: str, frame: str) -> None:
        """Emit an event to all subscribers."""
        for cb in self._subscribers.copy():
            try:
                await cb(event_id, frame)
            except Exception as exc:
                log.error("EventBus subscriber error: %s", exc)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class OutboxEventSource:
    """Watches bisque-outbox/ and converts v1 outbox files to v2 message frames.

    Files in bisque-outbox/ have the format:
        {"id": "...", "source": "bisque", "chat_id": "...", "text": "...", "timestamp": "..."}

    This source reads each file, builds a v2 message frame, emits it on the bus,
    and deletes the file.
    """

    def __init__(self, outbox_dir: Path, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        self._outbox_dir = outbox_dir
        self._bus = bus
        self._loop = loop
        self._observer: Observer | None = None

    def start(self) -> None:
        """Start watching the outbox directory. Drains pre-existing files first."""
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        # Drain existing files
        self._drain_existing()
        # Start watchdog
        handler = _OutboxHandler(self._outbox_dir, self._bus, self._loop)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._outbox_dir), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        log.info("OutboxEventSource watching: %s", self._outbox_dir)

    def stop(self) -> None:
        """Stop the watchdog observer."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _drain_existing(self) -> None:
        """Process any pre-existing outbox files."""
        for path in sorted(self._outbox_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            asyncio.run_coroutine_threadsafe(
                _process_outbox_file(path, self._bus), self._loop
            )


class _OutboxHandler(FileSystemEventHandler):
    """Watchdog handler for bisque-outbox/ files."""

    def __init__(self, outbox_dir: Path, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._outbox_dir = outbox_dir
        self._bus = bus
        self._loop = loop

    def on_created(self, event: Any) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix == ".json" and not p.name.startswith(".") and not p.name.endswith(".tmp"):
            asyncio.run_coroutine_threadsafe(
                _process_outbox_file(p, self._bus), self._loop
            )

    def on_moved(self, event: Any) -> None:
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if p.suffix == ".json" and not p.name.startswith(".") and not p.name.endswith(".tmp"):
            asyncio.run_coroutine_threadsafe(
                _process_outbox_file(p, self._bus), self._loop
            )


async def _process_outbox_file(path: Path, bus: EventBus) -> None:
    """Read an outbox file, convert to v2 frame, emit, delete."""
    try:
        if not path.exists():
            return
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read outbox file %s: %s", path.name, exc)
        return

    text = data.get("text", "")
    msg_id = data.get("id", path.stem)
    chat_id = data.get("chat_id", "")

    if not text:
        log.warning("Skipping empty outbox file: %s", path.name)
        path.unlink(missing_ok=True)
        return

    event_id = str(uuid.uuid4())
    frame = frame_message(text, "assistant", source="bisque", chat_id=chat_id, msg_id=msg_id)

    await bus.emit(event_id, frame)

    # Delete after successful emit
    path.unlink(missing_ok=True)
    log.debug("Processed outbox file: %s → event %s", path.name, event_id)


class FileSystemEventSource:
    """Watches wire-events/ for pre-built JSON event files.

    Files in wire-events/ should contain:
        {"type": "status", "status": "thinking", ...}

    The source wraps them in a v2 envelope, emits on the bus, and deletes.
    """

    def __init__(self, watch_dir: Path, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        self._watch_dir = watch_dir
        self._bus = bus
        self._loop = loop
        self._observer: Observer | None = None

    def start(self) -> None:
        """Start watching the wire-events directory."""
        self._watch_dir.mkdir(parents=True, exist_ok=True)
        self._drain_existing()
        handler = _WireEventHandler(self._watch_dir, self._bus, self._loop)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._watch_dir), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        log.info("FileSystemEventSource watching: %s", self._watch_dir)

    def stop(self) -> None:
        """Stop the watchdog observer."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _drain_existing(self) -> None:
        """Process any pre-existing wire event files."""
        for path in sorted(self._watch_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            asyncio.run_coroutine_threadsafe(
                _process_wire_event_file(path, self._bus), self._loop
            )


class _WireEventHandler(FileSystemEventHandler):
    """Watchdog handler for wire-events/ files."""

    def __init__(self, watch_dir: Path, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._watch_dir = watch_dir
        self._bus = bus
        self._loop = loop

    def on_created(self, event: Any) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix == ".json" and not p.name.startswith(".") and not p.name.endswith(".tmp"):
            asyncio.run_coroutine_threadsafe(
                _process_wire_event_file(p, self._bus), self._loop
            )

    def on_moved(self, event: Any) -> None:
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if p.suffix == ".json" and not p.name.startswith(".") and not p.name.endswith(".tmp"):
            asyncio.run_coroutine_threadsafe(
                _process_wire_event_file(p, self._bus), self._loop
            )


async def _process_wire_event_file(path: Path, bus: EventBus) -> None:
    """Read a wire event file, wrap in v2 envelope, emit, delete."""
    try:
        if not path.exists():
            return
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read wire event file %s: %s", path.name, exc)
        return

    if not isinstance(data, dict) or "type" not in data:
        log.warning("Ignoring malformed wire event file: %s", path.name)
        path.unlink(missing_ok=True)
        return

    frame_type = data.pop("type")
    event_id = data.pop("event_id", str(uuid.uuid4()))
    envelope = make_envelope(frame_type, **data)
    frame = serialize(envelope)

    await bus.emit(event_id, frame)

    path.unlink(missing_ok=True)
    log.debug("Processed wire event: %s → %s", path.name, event_id)
