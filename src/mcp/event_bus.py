"""
Lobster Event Bus — structured event infrastructure for observability and routing.

Design: pure dataclasses + a protocol-based listener interface. The EventBus is a
module-level singleton initialized at server startup. Listeners register against it
and receive events matching their filter. The JsonlFileListener writes all events to
~/lobster-workspace/logs/events.jsonl. The TelegramOutboxListener replicates the
existing _emit_debug_observation outbox-write behaviour.

No existing callsites are changed by this module (Step 1 / issue #890). Step 2
(#891) migrates _emit_debug_observation callsites; Step 3 (#892) routes
subagent_observation through the bus.

Issue #1665 additions:
- CRITICAL severity level (added to VALID_SEVERITIES and default EventFilter)
- CriticalAlertListener: forwards critical-severity events to Telegram, no debug gate
- MetricsListener: in-memory counters (events_by_type, events_by_severity, errors_last_1h)
- emit_event MCP tool handler (in inbox_server.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

log = logging.getLogger(__name__)
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
try:
    from .log_utils import GzipRotatingFileHandler
except ImportError:
    from log_utils import GzipRotatingFileHandler  # type: ignore[no-redef]
from pathlib import Path
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Severity constants
# ---------------------------------------------------------------------------

# Ordered from lowest to highest. "critical" was added in issue #1665.
VALID_SEVERITIES: frozenset[str] = frozenset({"debug", "info", "warn", "error", "critical"})


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LobsterEvent:
    """An immutable event emitted anywhere in the Lobster system."""

    event_type: str          # e.g. "memory.write", "agent.spawn", "debug.observation"
    severity: str            # "debug" | "info" | "warn" | "error" | "critical"
    source: str              # component that emitted this event
    payload: dict            # arbitrary structured data; keep serialisable
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    task_id: str | None = None
    chat_id: int | str | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict representation."""
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class EventFilter:
    """
    Declarative filter for deciding which events a listener accepts.

    event_types: set of event_type strings to accept; use {"*"} for wildcard.
    severity: set of severity strings to accept (e.g. {"warn", "error"}).
    require_debug_mode: when True, only accept events if LOBSTER_DEBUG=true.
    """

    severity: set[str] = field(default_factory=lambda: set(VALID_SEVERITIES))
    event_types: set[str] = field(default_factory=lambda: {"*"})
    require_debug_mode: bool = False

    def accepts(self, event: LobsterEvent) -> bool:
        """Pure predicate — returns True if this filter passes the event."""
        if self.require_debug_mode:
            debug_env = os.environ.get("LOBSTER_DEBUG", "").lower()
            if debug_env != "true":
                return False

        if event.severity not in self.severity:
            return False

        if "*" in self.event_types:
            return True

        return event.event_type in self.event_types


# ---------------------------------------------------------------------------
# Listener protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class EventListener(Protocol):
    """
    Protocol every event listener must satisfy.

    Listeners are registered with the EventBus and called for each event that
    passes their filter. Delivery is async; implementations must not block.
    """

    name: str

    def accepts(self, event: LobsterEvent) -> bool:
        """Return True if this listener wants to process the event."""
        ...

    async def deliver(self, event: LobsterEvent) -> None:
        """Deliver the event. Must not raise — swallow exceptions internally."""
        ...


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """
    Fanout event bus with registered listeners.

    Thread-safe: emit_sync() can be called from any thread. emit() is a coroutine
    for use in async contexts. Both fan out to all accepting listeners.

    Listener delivery errors are swallowed individually so one broken listener
    cannot affect others or the caller.
    """

    def __init__(self) -> None:
        self._listeners: list[EventListener] = []
        self._lock = threading.Lock()

    def register(self, listener: EventListener) -> None:
        """Register a listener. Safe to call before or after the event loop starts."""
        with self._lock:
            self._listeners.append(listener)

    def _accepting_listeners(self, event: LobsterEvent) -> list[EventListener]:
        with self._lock:
            return [l for l in self._listeners if l.accepts(event)]

    async def emit(self, event: LobsterEvent) -> None:
        """
        Emit an event to all accepting listeners.

        Must be called from within an asyncio event loop. Each listener's
        deliver() coroutine is awaited in sequence (not gathered) to keep
        ordering deterministic and simplify error isolation.
        """
        for listener in self._accepting_listeners(event):
            try:
                await listener.deliver(event)
            except Exception:
                pass  # individual listener failures must never propagate

    def emit_sync(self, event: LobsterEvent) -> None:
        """
        Thread-safe synchronous emit.

        If a running event loop exists (e.g. we are inside an async server),
        schedules each listener as a fire-and-forget task. If no loop is
        running (e.g. tests, scripts), runs the emit coroutine synchronously.

        Never blocks the caller beyond scheduling overhead.

        Logs a warning if no listeners are registered — this indicates
        init_event_bus() was not called at server startup and events will be
        silently dropped.
        """
        with self._lock:
            no_listeners = len(self._listeners) == 0
        if no_listeners:
            log.warning(
                "EventBus.emit_sync called with no listeners registered — "
                "init_event_bus() was not called at startup; event will be dropped: "
                "event_type=%s source=%s",
                event.event_type,
                event.source,
            )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.emit(event))
        except RuntimeError:
            # No running loop — run synchronously (test / script context)
            asyncio.run(self.emit(event))


# ---------------------------------------------------------------------------
# Built-in listeners
# ---------------------------------------------------------------------------

class JsonlFileListener:
    """
    Writes every accepted event to ~/lobster-workspace/logs/events.jsonl.

    One JSON object per line. Uses a GzipRotatingFileHandler (1 GB x 5 backups,
    gzip-compressed) so the file never grows unboundedly while preserving up to
    ~5 GB of history. The handler is initialised lazily on first deliver() call
    so that importing this module does not create any files.
    """

    name = "jsonl-file"

    _MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB per file
    _BACKUP_COUNT = 5                      # keep 5 gzip-compressed rotated files

    def __init__(
        self,
        path: Path | None = None,
        event_filter: EventFilter | None = None,
    ) -> None:
        self._path = path or (
            Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
            / "logs"
            / "events.jsonl"
        )
        self._filter = event_filter or EventFilter(
            severity=set(VALID_SEVERITIES),
            event_types={"*"},
            require_debug_mode=False,
        )
        self._handler: GzipRotatingFileHandler | None = None
        self._handler_lock = threading.Lock()

    def _get_handler(self) -> GzipRotatingFileHandler:
        """Return (and lazily initialise) the GzipRotatingFileHandler."""
        if self._handler is None:
            with self._handler_lock:
                if self._handler is None:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    self._handler = GzipRotatingFileHandler(
                        self._path,
                        maxBytes=self._MAX_BYTES,
                        backupCount=self._BACKUP_COUNT,
                        encoding="utf-8",
                    )
        return self._handler

    def accepts(self, event: LobsterEvent) -> bool:
        return self._filter.accepts(event)

    async def deliver(self, event: LobsterEvent) -> None:
        try:
            line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
            handler = self._get_handler()
            handler.acquire()
            try:
                if handler.stream is None:
                    handler.stream = handler._open()  # type: ignore[attr-defined]
                if handler.shouldRollover(logging.makeLogRecord({"msg": line})):
                    handler.doRollover()
                handler.stream.write(line)
                handler.stream.flush()
            finally:
                handler.release()
        except Exception:
            pass  # file listener failures must never propagate


class TelegramOutboxListener:
    """
    Replicates the _emit_debug_observation outbox-write behaviour via the bus.

    Accepts only events with severity in {"warn", "error"} (or "debug" in debug
    mode) and writes them directly to the bot outbox directory so they are
    delivered to Telegram without entering the dispatcher inbox.

    This listener mirrors the existing _emit_debug_observation function. The
    actual migration of callsites to use the bus instead of calling
    _emit_debug_observation directly is done in issue #891.
    """

    name = "telegram-outbox"

    def __init__(
        self,
        outbox_dir: Path | None = None,
        event_filter: EventFilter | None = None,
    ) -> None:
        self._outbox_dir = outbox_dir  # resolved lazily so it doesn't break imports
        self._filter = event_filter or EventFilter(
            # Mirror _emit_debug_observation: only deliver when LOBSTER_DEBUG=true.
            # The filter is debug-mode-gated; callers do not need to check.
            # Note: "critical" is intentionally excluded here — CriticalAlertListener
            # handles critical events without requiring LOBSTER_DEBUG=true.
            severity={"warn", "error", "debug", "info"},
            event_types={"*"},
            require_debug_mode=True,
        )

    def _get_outbox_dir(self) -> Path:
        if self._outbox_dir is not None:
            return self._outbox_dir
        messages_base = Path(
            os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")
        )
        return messages_base / "outbox"

    def _resolve_owner(self) -> tuple[int | str | None, str]:
        """Return (chat_id, source) for the configured debug owner."""
        chat_id: int | str | None = None
        source = "telegram"
        try:
            config_dir = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")) / "config"
            config_file = config_dir / "config.env"
            slack_enabled = False
            slack_channel: str | None = None
            telegram_chat_id: int | None = None
            if config_file.exists():
                for line in config_file.read_text().splitlines():
                    stripped = line.strip()
                    if stripped.startswith("TELEGRAM_ALLOWED_USERS="):
                        val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                        first = val.split(",")[0].strip()
                        if first.lstrip("-").isdigit():
                            telegram_chat_id = int(first)
                    elif stripped.startswith("LOBSTER_ENABLE_SLACK="):
                        slack_enabled = stripped.split("=", 1)[1].strip().strip('"').strip("'").lower() == "true"
                    elif stripped.startswith("LOBSTER_SLACK_ALLOWED_CHANNELS="):
                        val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                        first_chan = val.split(",")[0].strip()
                        if first_chan:
                            slack_channel = first_chan
            if slack_enabled and slack_channel:
                source = "slack"
                chat_id = slack_channel
            elif telegram_chat_id is not None:
                source = "telegram"
                chat_id = telegram_chat_id
        except Exception:
            pass
        return chat_id, source

    def accepts(self, event: LobsterEvent) -> bool:
        # system_context events are always suppressed — never forward to Telegram
        if event.event_type.startswith("system_context"):
            return False
        return self._filter.accepts(event)

    async def deliver(self, event: LobsterEvent) -> None:
        try:
            chat_id, source = self._resolve_owner()
            if chat_id is None:
                return

            outbox_dir = self._get_outbox_dir()
            outbox_dir.mkdir(parents=True, exist_ok=True)

            ts_ms = int(event.timestamp.timestamp() * 1000)
            safe_source = "".join(c if c.isalnum() or c in "-_" else "_" for c in event.source)[:40]
            message_id = f"{ts_ms}_debug_{safe_source}"

            emitter_label = event.task_id or event.source or "unknown"
            label = f"[debug|event-bus] {event.event_type} from {emitter_label}"
            full_text = f"{label}\n{event.payload.get('text', json.dumps(event.payload))}"

            message = {
                "id": message_id,
                "type": "debug_observation",
                "source": source,
                "chat_id": chat_id,
                "text": full_text,
                "timestamp": event.timestamp.isoformat(),
            }

            outbox_file = outbox_dir / f"{message_id}.json"
            # Atomic write: write to tmp then rename
            tmp_file = outbox_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(message), encoding="utf-8")
            tmp_file.rename(outbox_file)
        except Exception:
            pass  # telegram listener failures must never propagate


class CriticalAlertListener:
    """
    Forwards severity=critical events to the Telegram outbox without a debug-mode gate.

    Unlike TelegramOutboxListener (which requires LOBSTER_DEBUG=true), this listener
    always delivers critical-severity events. Gate: LOBSTER_SILENT_ERRORS=true suppresses
    delivery entirely (useful in tests and maintenance windows).

    Delivery is atomic: write to .tmp then rename, same as TelegramOutboxListener.
    """

    name = "critical-alert"

    def __init__(self, outbox_dir: Path | None = None) -> None:
        self._outbox_dir = outbox_dir  # resolved lazily

    def _get_outbox_dir(self) -> Path:
        if self._outbox_dir is not None:
            return self._outbox_dir
        messages_base = Path(
            os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")
        )
        return messages_base / "outbox"

    def _resolve_owner(self) -> tuple[int | str | None, str]:
        """Return (chat_id, source) for the configured owner — identical to TelegramOutboxListener."""
        chat_id: int | str | None = None
        source = "telegram"
        try:
            config_dir = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")) / "config"
            config_file = config_dir / "config.env"
            slack_enabled = False
            slack_channel: str | None = None
            telegram_chat_id: int | None = None
            if config_file.exists():
                for line in config_file.read_text().splitlines():
                    stripped = line.strip()
                    if stripped.startswith("TELEGRAM_ALLOWED_USERS="):
                        val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                        first = val.split(",")[0].strip()
                        if first.lstrip("-").isdigit():
                            telegram_chat_id = int(first)
                    elif stripped.startswith("LOBSTER_ENABLE_SLACK="):
                        slack_enabled = stripped.split("=", 1)[1].strip().strip('"').strip("'").lower() == "true"
                    elif stripped.startswith("LOBSTER_SLACK_ALLOWED_CHANNELS="):
                        val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                        first_chan = val.split(",")[0].strip()
                        if first_chan:
                            slack_channel = first_chan
            if slack_enabled and slack_channel:
                source = "slack"
                chat_id = slack_channel
            elif telegram_chat_id is not None:
                source = "telegram"
                chat_id = telegram_chat_id
        except Exception:
            pass
        return chat_id, source

    def accepts(self, event: LobsterEvent) -> bool:
        return event.severity == "critical"

    async def deliver(self, event: LobsterEvent) -> None:
        try:
            # LOBSTER_SILENT_ERRORS=true suppresses all alert delivery
            if os.environ.get("LOBSTER_SILENT_ERRORS", "").lower() == "true":
                return

            chat_id, source = self._resolve_owner()
            if chat_id is None:
                return

            outbox_dir = self._get_outbox_dir()
            outbox_dir.mkdir(parents=True, exist_ok=True)

            ts_ms = int(event.timestamp.timestamp() * 1000)
            safe_source = "".join(c if c.isalnum() or c in "-_" else "_" for c in event.source)[:40]
            message_id = f"{ts_ms}_critical_{safe_source}"

            emitter_label = event.task_id or event.source or "unknown"
            label = f"[CRITICAL|event-bus] {event.event_type} from {emitter_label}"
            full_text = f"{label}\n{event.payload.get('text', event.payload.get('msg', json.dumps(event.payload)))}"

            message = {
                "id": message_id,
                "type": "critical_alert",
                "source": source,
                "chat_id": chat_id,
                "text": full_text,
                "timestamp": event.timestamp.isoformat(),
            }

            outbox_file = outbox_dir / f"{message_id}.json"
            tmp_file = outbox_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(message), encoding="utf-8")
            tmp_file.rename(outbox_file)
        except Exception:
            pass  # critical listener failures must never propagate


class MetricsListener:
    """
    In-memory event counters for observability.

    Maintains three counters updated on every deliver() call:
    - events_by_type: dict[str, int] — total count per event_type
    - events_by_severity: dict[str, int] — total count per severity level
    - _error_timestamps: list of (datetime, severity) for events with
      severity in {"error", "critical"}, used to compute errors_last_1h

    get_snapshot() returns a copy of all counters as a plain dict. The copy
    is intentionally shallow — callers receive their own dict so mutations
    do not affect internal state.

    Thread-safe: all mutations are protected by a lock.
    """

    name = "metrics"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events_by_type: dict[str, int] = defaultdict(int)
        self._events_by_severity: dict[str, int] = defaultdict(int)
        # Store timestamps of error/critical events for sliding-window count
        self._error_timestamps: list[datetime] = []

    def accepts(self, event: LobsterEvent) -> bool:
        return True  # count everything

    async def deliver(self, event: LobsterEvent) -> None:
        try:
            with self._lock:
                self._events_by_type[event.event_type] += 1
                self._events_by_severity[event.severity] += 1
                if event.severity in ("error", "critical"):
                    self._error_timestamps.append(event.timestamp)
        except Exception:
            pass  # metrics failures must never propagate

    def get_snapshot(self) -> dict:
        """
        Return a point-in-time snapshot of all counters.

        Returns a fresh dict — mutating the result does not affect internal state.
        errors_last_1h is computed from the sliding window at snapshot time.
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=1)
            # Prune old entries while holding the lock (keeps the list bounded)
            self._error_timestamps = [
                ts for ts in self._error_timestamps
                if ts >= cutoff
            ]
            errors_last_1h = len(self._error_timestamps)
            return {
                "events_by_type": dict(self._events_by_type),
                "events_by_severity": dict(self._events_by_severity),
                "errors_last_1h": errors_last_1h,
            }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_EVENT_BUS: EventBus | None = None
_BUS_LOCK = threading.Lock()



def get_event_bus() -> EventBus:
    """
    Return the module-level EventBus singleton.

    Thread-safe double-checked locking. The bus is created on first call with
    no listeners registered. Listeners are added by init_event_bus() at server
    startup.

    Emitting events before init_event_bus() is called means no listeners are
    registered and all events are silently dropped. To make this misconfiguration
    visible, a warning is logged when emit() or emit_sync() is called on a bus
    with no listeners.
    """
    global _EVENT_BUS
    if _EVENT_BUS is None:
        with _BUS_LOCK:
            if _EVENT_BUS is None:
                _EVENT_BUS = EventBus()
    return _EVENT_BUS


def init_event_bus(
    jsonl_path: Path | None = None,
    outbox_dir: Path | None = None,
) -> EventBus:
    """
    Initialize the module-level singleton with standard listeners.

    Called once at server startup (in inbox_server.py main()). Safe to call
    multiple times — subsequent calls are no-ops that return the existing bus.

    Registers:
    - JsonlFileListener: writes all events to events.jsonl
    - TelegramOutboxListener: forwards debug events to Telegram outbox when
      LOBSTER_DEBUG=true
    - CriticalAlertListener: forwards critical-severity events to Telegram,
      no debug-mode gate (issue #1665)
    - MetricsListener: in-memory counters for observability (issue #1665)
    """
    bus = get_event_bus()
    # Idempotency guard: check if already initialised
    with _BUS_LOCK:
        if any(getattr(l, "name", None) == "jsonl-file" for l in bus._listeners):
            return bus
        bus.register(JsonlFileListener(path=jsonl_path))
        bus.register(TelegramOutboxListener(outbox_dir=outbox_dir))
        bus.register(CriticalAlertListener(outbox_dir=outbox_dir))
        bus.register(MetricsListener())
    return bus


def get_metrics_listener() -> MetricsListener | None:
    """
    Return the MetricsListener registered with the module-level bus, or None.

    Used by the observability server to expose in-memory counters on the
    /observability HTTP endpoint without coupling the two modules via import.
    """
    bus = get_event_bus()
    with _BUS_LOCK:
        for listener in bus._listeners:
            if isinstance(listener, MetricsListener):
                return listener
    return None
