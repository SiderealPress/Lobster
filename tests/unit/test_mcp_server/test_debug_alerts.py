"""
Tests for debug alert hooks:
  - memory_store debug alert (Feature 1)
  - memory_search debug alert (Feature 1)
  - write_result debug alert (Feature 2)

All tests mock _emit_event so no real I/O or event bus interactions occur.
The old _emit_debug_observation / _DEBUG_MODE / _DEBUG_ALERTS_ENABLED pattern
was removed in issue #891 and replaced with _emit_event → event bus delivery.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/mcp is on sys.path (mirrors the pattern used throughout this package).
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401  — pre-load for patch.multiple resolution


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_memory_provider(result_count: int = 3):
    """Return a minimal fake memory provider."""

    class FakeEvent:
        id = 1
        type = "note"
        source = "internal"
        project = None
        timestamp = None
        content = "fake event content"
        metadata = {}

    class FakeMemoryProvider:
        def store(self, event) -> int:
            return 42

        def search(self, query, limit=10, project=None):
            return [FakeEvent() for _ in range(result_count)]

    return FakeMemoryProvider()


class MemoryEvent:
    """Thin stand-in so handle_memory_store can construct an event."""

    def __init__(self, *, id, timestamp, type, source, project, content, metadata):
        self.id = id
        self.timestamp = timestamp
        self.type = type
        self.source = source
        self.project = project
        self.content = content
        self.metadata = metadata


# ---------------------------------------------------------------------------
# Feature 1: memory_store debug alerts
# ---------------------------------------------------------------------------


class TestMemoryStoreDebugAlert:
    """memory_store emits a debug event via _emit_event."""

    def _run(self, arguments: dict, memory_provider=None) -> tuple:
        if memory_provider is None:
            memory_provider = _make_fake_memory_provider()

        emitted: list[dict] = []

        def fake_emit(
            text: str,
            event_type: str = "debug.observation",
            severity: str = "debug",
            source: str = "inbox-server",
            emitter: str | None = None,
            task_id: str | None = None,
            chat_id=None,
        ) -> None:
            emitted.append(
                {
                    "text": text,
                    "event_type": event_type,
                    "severity": severity,
                    "source": source,
                    "emitter": emitter,
                    "task_id": task_id,
                }
            )

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=memory_provider,
            _emit_event=fake_emit,
            MemoryEvent=MemoryEvent,
        ):
            from src.mcp.inbox_server import handle_memory_store

            result = asyncio.run(handle_memory_store(arguments))

        return result, emitted

    def test_debug_alert_fires_on_successful_store(self):
        """A successful memory_store emits exactly one debug event."""
        _, emitted = self._run({"content": "Remember this important fact."})
        assert len(emitted) == 1

    def test_debug_alert_contains_memory_write_label(self):
        """The debug alert text contains the [memory write] label."""
        _, emitted = self._run({"content": "Something to store."})
        assert "[memory write]" in emitted[0]["text"]

    def test_debug_alert_contains_content_preview(self):
        """The debug alert text contains (up to 80 chars of) the stored content."""
        content = "This is the content to store."
        _, emitted = self._run({"content": content})
        assert content in emitted[0]["text"]

    def test_content_truncated_at_80_chars(self):
        """Content longer than 80 chars is truncated with an ellipsis in the alert."""
        long_content = "x" * 100
        _, emitted = self._run({"content": long_content})
        assert "x" * 80 in emitted[0]["text"]
        assert "\u2026" in emitted[0]["text"]  # ellipsis character
        assert "x" * 81 not in emitted[0]["text"]

    def test_debug_alert_uses_task_id_as_emitter(self):
        """When task_id is passed, it appears as agent label and emitter."""
        _, emitted = self._run(
            {"content": "Store this.", "task_id": "my-subagent-42"}
        )
        assert "my-subagent-42" in emitted[0]["text"]
        assert emitted[0]["emitter"] == "my-subagent-42"

    def test_debug_alert_falls_back_to_dispatcher_label(self):
        """When task_id is absent, alert uses 'dispatcher' as the agent label."""
        _, emitted = self._run({"content": "Store this."})
        assert "dispatcher" in emitted[0]["text"]

    def test_debug_alert_includes_memory_type(self):
        """The alert includes the event type field."""
        _, emitted = self._run({"content": "Note.", "type": "decision"})
        assert "decision" in emitted[0]["text"]

    def test_debug_alert_event_type_is_memory_write(self):
        """Memory store alerts use event_type='memory.write'."""
        _, emitted = self._run({"content": "Any content."})
        assert emitted[0]["event_type"] == "memory.write"

    def test_debug_alert_severity_is_debug(self):
        """Memory store alerts use severity='debug'."""
        _, emitted = self._run({"content": "Any content."})
        assert emitted[0]["severity"] == "debug"

    def test_no_alert_when_emit_event_raises(self):
        """Even if _emit_event raises, handle_memory_store still returns success."""
        provider = _make_fake_memory_provider()

        def raising_emit(*args, **kwargs):
            raise RuntimeError("bus offline")

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=provider,
            _emit_event=raising_emit,
            MemoryEvent=MemoryEvent,
        ):
            from src.mcp.inbox_server import handle_memory_store

            result = asyncio.run(handle_memory_store({"content": "Still stored."}))

        assert len(result) == 1
        assert "Stored memory event" in result[0].text

    def test_alert_does_not_affect_return_value(self):
        """The debug alert is additive — it does not change the handler's return value."""
        result, _ = self._run({"content": "Return value check."})
        assert len(result) == 1
        assert "Stored memory event" in result[0].text


# ---------------------------------------------------------------------------
# Feature 1: memory_search debug alerts
# ---------------------------------------------------------------------------


class TestMemorySearchDebugAlert:
    """memory_search emits a debug event via _emit_event."""

    def _run(self, arguments: dict, result_count: int = 2) -> tuple:
        provider = _make_fake_memory_provider(result_count=result_count)
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            event_type: str = "debug.observation",
            severity: str = "debug",
            source: str = "inbox-server",
            emitter: str | None = None,
            task_id: str | None = None,
            chat_id=None,
        ) -> None:
            emitted.append(
                {
                    "text": text,
                    "event_type": event_type,
                    "severity": severity,
                    "source": source,
                    "emitter": emitter,
                    "task_id": task_id,
                }
            )

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=provider,
            _emit_event=fake_emit,
        ):
            from src.mcp.inbox_server import handle_memory_search

            result = asyncio.run(handle_memory_search(arguments))

        return result, emitted

    def test_debug_alert_fires_on_search(self):
        """A memory_search emits exactly one debug event."""
        _, emitted = self._run({"query": "something"})
        assert len(emitted) == 1

    def test_debug_alert_contains_memory_read_label(self):
        """The debug alert text contains the [memory read] label."""
        _, emitted = self._run({"query": "some query"})
        assert "[memory read]" in emitted[0]["text"]

    def test_debug_alert_contains_query_text(self):
        """The debug alert text contains the search query."""
        _, emitted = self._run({"query": "find my notes"})
        assert "find my notes" in emitted[0]["text"]

    def test_debug_alert_contains_result_count(self):
        """The debug alert text includes the number of results found."""
        _, emitted = self._run({"query": "count test"}, result_count=5)
        assert "5" in emitted[0]["text"]

    def test_debug_alert_zero_results(self):
        """When no results are found, the alert still fires and shows 0."""
        _, emitted = self._run({"query": "nothing here"}, result_count=0)
        assert len(emitted) == 1
        assert "0" in emitted[0]["text"]

    def test_debug_alert_uses_task_id_as_emitter(self):
        """When task_id is passed, it appears as agent label and emitter."""
        _, emitted = self._run({"query": "test", "task_id": "search-agent-7"})
        assert "search-agent-7" in emitted[0]["text"]
        assert emitted[0]["emitter"] == "search-agent-7"

    def test_debug_alert_falls_back_to_dispatcher_label(self):
        """When task_id is absent, alert uses 'dispatcher' as the agent label."""
        _, emitted = self._run({"query": "test"})
        assert "dispatcher" in emitted[0]["text"]

    def test_debug_alert_event_type_is_memory_search(self):
        """Memory search alerts use event_type='memory.search'."""
        _, emitted = self._run({"query": "any"})
        assert emitted[0]["event_type"] == "memory.search"

    def test_no_alert_when_debug_alerts_disabled(self):
        """When _emit_event is a no-op (e.g. bus unavailable), search still works."""
        provider = _make_fake_memory_provider(result_count=2)

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=provider,
            _EVENT_BUS_AVAILABLE=False,
        ):
            from src.mcp.inbox_server import handle_memory_search

            result = asyncio.run(handle_memory_search({"query": "silent search"}))

        # Result is still returned correctly — debug is best-effort
        assert len(result) == 1

    def test_alert_is_additive_does_not_affect_return_value(self):
        """The debug alert does not affect the search results returned."""
        result, _ = self._run({"query": "test query"}, result_count=3)
        # Should return results text (not "No memory events found")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Feature 2: write_result debug alerts
# ---------------------------------------------------------------------------


class TestWriteResultDebugAlert:
    """write_result emits a debug event via _emit_event."""

    def _run(self, args: dict, inbox_dir: Path) -> tuple:
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            event_type: str = "debug.observation",
            severity: str = "debug",
            source: str = "inbox-server",
            emitter: str | None = None,
            task_id: str | None = None,
            chat_id=None,
        ) -> None:
            emitted.append(
                {
                    "text": text,
                    "event_type": event_type,
                    "severity": severity,
                    "source": source,
                    "emitter": emitter,
                    "task_id": task_id,
                }
            )

        # Minimal session store stub
        class FakeSessionStore:
            def session_end(self, **kwargs):
                pass

            def set_notified(self, *args, **kwargs):
                pass

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _emit_event=fake_emit,
            _session_store=FakeSessionStore(),
        ):
            # Patch asyncio.create_task to be a no-op (wire server notify)
            with patch("asyncio.create_task"):
                from src.mcp.inbox_server import handle_write_result

                result = asyncio.run(handle_write_result(args))

        return result, emitted

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    def test_debug_alert_fires_on_write_result(self, inbox_dir: Path):
        """write_result emits exactly one debug event."""
        _, emitted = self._run(
            {
                "task_id": "test-task-1",
                "chat_id": 123,
                "text": "Task complete.",
                "status": "success",
            },
            inbox_dir,
        )
        assert len(emitted) == 1

    def test_debug_alert_contains_subagent_dispatcher_label(self, inbox_dir: Path):
        """The alert text contains the [subagent→dispatcher] label."""
        _, emitted = self._run(
            {"task_id": "t1", "chat_id": 1, "text": "Done."},
            inbox_dir,
        )
        assert "subagent" in emitted[0]["text"]
        assert "dispatcher" in emitted[0]["text"]

    def test_debug_alert_includes_task_id(self, inbox_dir: Path):
        """The alert text includes the task_id."""
        _, emitted = self._run(
            {"task_id": "my-special-task", "chat_id": 1, "text": "Done."},
            inbox_dir,
        )
        assert "my-special-task" in emitted[0]["text"]

    def test_debug_alert_includes_message_type_subagent_result(self, inbox_dir: Path):
        """The alert includes the resolved message type (subagent_result)."""
        _, emitted = self._run(
            {
                "task_id": "t2",
                "chat_id": 1,
                "text": "Result text.",
                "status": "success",
                "sent_reply_to_user": False,
            },
            inbox_dir,
        )
        assert "subagent_result" in emitted[0]["text"]

    def test_debug_alert_includes_message_type_subagent_error(self, inbox_dir: Path):
        """The alert includes the resolved message type (subagent_error)."""
        _, emitted = self._run(
            {
                "task_id": "t3",
                "chat_id": 1,
                "text": "Something failed.",
                "status": "error",
                "sent_reply_to_user": False,
            },
            inbox_dir,
        )
        assert "subagent_error" in emitted[0]["text"]

    def test_debug_alert_includes_message_type_subagent_notification(
        self, inbox_dir: Path
    ):
        """When sent_reply_to_user=True the type is subagent_notification."""
        _, emitted = self._run(
            {
                "task_id": "t4",
                "chat_id": 1,
                "text": "Already replied.",
                "sent_reply_to_user": True,
            },
            inbox_dir,
        )
        assert "subagent_notification" in emitted[0]["text"]

    def test_debug_alert_includes_status(self, inbox_dir: Path):
        """The alert text includes the status field."""
        _, emitted = self._run(
            {"task_id": "t5", "chat_id": 1, "text": "Done.", "status": "success"},
            inbox_dir,
        )
        assert "success" in emitted[0]["text"]

    def test_debug_alert_includes_sent_reply_flag(self, inbox_dir: Path):
        """The alert includes the sent_reply_to_user value."""
        _, emitted = self._run(
            {
                "task_id": "t6",
                "chat_id": 1,
                "text": "Done.",
                "sent_reply_to_user": True,
            },
            inbox_dir,
        )
        assert "True" in emitted[0]["text"]

    def test_debug_alert_does_not_include_text_content(self, inbox_dir: Path):
        """The alert is a compact routing summary — result text is NOT inlined.

        Text preview was removed from the alert format. The full result text
        appears in the inbox message body, not in the debug alert.
        """
        _, emitted = self._run(
            {"task_id": "t7", "chat_id": 1, "text": "Short message."},
            inbox_dir,
        )
        # Alert must contain task_id but must NOT inline the payload text.
        assert "t7" in emitted[0]["text"]
        assert "Short message." not in emitted[0]["text"]

    def test_debug_alert_emitter_includes_task_id(self, inbox_dir: Path):
        """The emitter passed to _emit_event is task:<task_id>."""
        _, emitted = self._run(
            {"task_id": "emitter-check", "chat_id": 1, "text": "Done."},
            inbox_dir,
        )
        assert emitted[0]["emitter"] == "task:emitter-check"

    def test_no_debug_alert_when_event_bus_unavailable(self, inbox_dir: Path):
        """When _EVENT_BUS_AVAILABLE=False, _emit_event is a no-op for write_result.

        The handler calls _emit_event but it returns early because the event bus
        is not available. The inbox file is still written.
        """
        class FakeSessionStore:
            def session_end(self, **kwargs):
                pass

            def set_notified(self, *args, **kwargs):
                pass

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _EVENT_BUS_AVAILABLE=False,
            _session_store=FakeSessionStore(),
        ):
            with patch("asyncio.create_task"):
                from src.mcp.inbox_server import handle_write_result

                asyncio.run(
                    handle_write_result(
                        {"task_id": "quiet", "chat_id": 1, "text": "Silent."}
                    )
                )

        # Inbox file should still be written even without event bus
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1

    def test_debug_alert_is_additive_inbox_file_still_written(self, inbox_dir: Path):
        """The debug alert is best-effort and does not affect inbox file creation."""
        self._run(
            {"task_id": "file-check", "chat_id": 1, "text": "Written."},
            inbox_dir,
        )
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        content = json.loads(files[0].read_text())
        assert content["task_id"] == "file-check"

    def test_debug_alert_event_type_is_agent_write_result(self, inbox_dir: Path):
        """write_result alerts use event_type='agent.write_result'."""
        _, emitted = self._run(
            {"task_id": "evt-type-check", "chat_id": 1, "text": "Done."},
            inbox_dir,
        )
        assert emitted[0]["event_type"] == "agent.write_result"


# ---------------------------------------------------------------------------
# Feature: _emit_event bus delivery
# ---------------------------------------------------------------------------


class TestEmitEventBusDelivery:
    """_emit_event emits to the event bus when available.

    Replaces the old TestEmitDebugObservationOutboxDelivery tests.
    The new architecture delivers debug events via the event bus (bus listeners
    handle final delivery), not via direct outbox writes in inbox_server.
    """

    def test_emit_event_calls_bus_emit_sync(self):
        """_emit_event calls bus.emit_sync when _EVENT_BUS_AVAILABLE is True."""
        captured_events = []
        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: captured_events.append(e)
        mock_event_cls = MagicMock(side_effect=lambda **kw: kw)

        with patch.multiple(
            "src.mcp.inbox_server",
            _EVENT_BUS_AVAILABLE=True,
            get_event_bus=MagicMock(return_value=mock_bus),
            LobsterEvent=mock_event_cls,
        ):
            from src.mcp.inbox_server import _emit_event

            _emit_event("test text", event_type="debug.observation", severity="debug")

        assert mock_bus.emit_sync.called
        assert len(captured_events) == 1

    def test_emit_event_noop_when_bus_unavailable(self):
        """_emit_event is a no-op when _EVENT_BUS_AVAILABLE is False."""
        mock_bus = MagicMock()

        with patch.multiple(
            "src.mcp.inbox_server",
            _EVENT_BUS_AVAILABLE=False,
            get_event_bus=mock_bus,
        ):
            from src.mcp.inbox_server import _emit_event

            # Should not raise, should not call bus
            _emit_event("silent text")

        mock_bus.assert_not_called()

    def test_emit_event_includes_text_in_payload(self):
        """_emit_event includes the text in the LobsterEvent payload."""
        captured = []
        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: captured.append(e)

        def make_event(**kw):
            return kw

        with patch.multiple(
            "src.mcp.inbox_server",
            _EVENT_BUS_AVAILABLE=True,
            get_event_bus=MagicMock(return_value=mock_bus),
            LobsterEvent=make_event,
        ):
            from src.mcp.inbox_server import _emit_event

            _emit_event("my important text")

        assert len(captured) == 1
        assert captured[0]["payload"]["text"] == "my important text"

    def test_emit_event_never_raises(self):
        """_emit_event must never raise even if the bus raises."""
        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = RuntimeError("bus is down")

        with patch.multiple(
            "src.mcp.inbox_server",
            _EVENT_BUS_AVAILABLE=True,
            get_event_bus=MagicMock(return_value=mock_bus),
            LobsterEvent=MagicMock(return_value=MagicMock()),
        ):
            from src.mcp.inbox_server import _emit_event

            # Should not raise
            _emit_event("text that triggers error")

    def test_emit_event_passes_severity_to_event(self):
        """_emit_event forwards the severity argument to LobsterEvent."""
        captured = []

        def make_event(**kw):
            captured.append(kw)
            return kw

        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: None

        with patch.multiple(
            "src.mcp.inbox_server",
            _EVENT_BUS_AVAILABLE=True,
            get_event_bus=MagicMock(return_value=mock_bus),
            LobsterEvent=make_event,
        ):
            from src.mcp.inbox_server import _emit_event

            _emit_event("text", severity="warn")

        assert len(captured) == 1
        assert captured[0]["severity"] == "warn"

    def test_emit_event_passes_source_to_event(self):
        """_emit_event forwards the source argument to LobsterEvent."""
        captured = []

        def make_event(**kw):
            captured.append(kw)
            return kw

        mock_bus = MagicMock()
        mock_bus.emit_sync.side_effect = lambda e: None

        with patch.multiple(
            "src.mcp.inbox_server",
            _EVENT_BUS_AVAILABLE=True,
            get_event_bus=MagicMock(return_value=mock_bus),
            LobsterEvent=make_event,
        ):
            from src.mcp.inbox_server import _emit_event

            _emit_event("text", source="write-result")

        assert len(captured) == 1
        assert captured[0]["source"] == "write-result"
