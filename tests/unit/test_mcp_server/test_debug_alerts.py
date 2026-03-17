"""
Tests for LOBSTER_DEBUG=true alert hooks:
  - memory_store debug alert (Feature 1)
  - memory_search debug alert (Feature 1)
  - write_result debug alert (Feature 2)

All tests mock _emit_debug_observation so no real I/O is performed,
and rely on the session-scoped block_outbound_http fixture in conftest.py
as a belt-and-suspenders guard.
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
    """LOBSTER_DEBUG=true fires a debug alert on memory_store."""

    def _run(self, arguments: dict, memory_provider=None) -> list:
        if memory_provider is None:
            memory_provider = _make_fake_memory_provider()

        emitted: list[dict] = []

        def fake_emit(
            text: str,
            category: str = "system_context",
            visibility: str = "mcp-only",
            emitter: str | None = None,
        ) -> None:
            emitted.append(
                {
                    "text": text,
                    "category": category,
                    "visibility": visibility,
                    "emitter": emitter,
                }
            )

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=memory_provider,
            _DEBUG_MODE=True,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
            MemoryEvent=MemoryEvent,
        ):
            from src.mcp.inbox_server import handle_memory_store

            result = asyncio.run(handle_memory_store(arguments))

        return result, emitted

    def test_debug_alert_fires_on_successful_store(self):
        """A successful memory_store emits exactly one debug push when debug mode is on."""
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

    def test_debug_alert_category_is_system_context(self):
        """Memory store alerts use system_context category."""
        _, emitted = self._run({"content": "Any content."})
        assert emitted[0]["category"] == "system_context"

    def test_debug_alert_visibility_is_mcp_only(self):
        """Memory store alerts use mcp-only visibility."""
        _, emitted = self._run({"content": "Any content."})
        assert emitted[0]["visibility"] == "mcp-only"

    def test_no_alert_when_debug_alerts_disabled(self):
        """When _DEBUG_ALERTS_ENABLED=False, _emit_debug_observation returns early.

        The outer _DEBUG_MODE gate has been removed; _emit_debug_observation is the
        single authoritative gate.  The handler always calls _emit_debug_observation,
        but the function is a no-op when _DEBUG_ALERTS_ENABLED=False.
        """
        import src.mcp.inbox_server as _mod
        from unittest.mock import patch as _patch

        called_with: list[dict] = []

        original_emit = _mod._emit_debug_observation

        def spying_emit(text, category="system_context", visibility="mcp-only", emitter=None):
            # Record the call but still invoke the real function (which is a no-op here)
            called_with.append({"text": text})
            original_emit(text, category=category, visibility=visibility, emitter=emitter)

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=_make_fake_memory_provider(),
            _DEBUG_MODE=False,
            _DEBUG_ALERTS_ENABLED=False,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=spying_emit,
            MemoryEvent=MemoryEvent,
        ):
            from src.mcp.inbox_server import handle_memory_store

            asyncio.run(handle_memory_store({"content": "Silent store."}))

        # The handler calls _emit_debug_observation unconditionally (single-gate contract);
        # the function itself is a no-op because _DEBUG_ALERTS_ENABLED=False.
        assert len(called_with) == 1  # called exactly once — no outer gate suppresses it

    def test_alert_does_not_affect_return_value(self):
        """The debug alert is additive — it does not change the handler's return value."""
        result, _ = self._run({"content": "Return value check."})
        assert len(result) == 1
        assert "Stored memory event" in result[0].text


# ---------------------------------------------------------------------------
# Feature 1: memory_search debug alerts
# ---------------------------------------------------------------------------


class TestMemorySearchDebugAlert:
    """LOBSTER_DEBUG=true fires a debug alert on memory_search."""

    def _run(self, arguments: dict, result_count: int = 2) -> tuple:
        provider = _make_fake_memory_provider(result_count=result_count)
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            category: str = "system_context",
            visibility: str = "mcp-only",
            emitter: str | None = None,
        ) -> None:
            emitted.append(
                {
                    "text": text,
                    "category": category,
                    "visibility": visibility,
                    "emitter": emitter,
                }
            )

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=provider,
            _DEBUG_MODE=True,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
        ):
            from src.mcp.inbox_server import handle_memory_search

            result = asyncio.run(handle_memory_search(arguments))

        return result, emitted

    def test_debug_alert_fires_on_search(self):
        """A memory_search emits exactly one debug push when debug mode is on."""
        _, emitted = self._run({"query": "something"})
        assert len(emitted) == 1

    def test_debug_alert_contains_memory_read_label(self):
        """The debug alert text contains the [memory read] label."""
        _, emitted = self._run({"query": "anything"})
        assert "[memory read]" in emitted[0]["text"]

    def test_debug_alert_contains_query_text(self):
        """The debug alert includes the search query."""
        _, emitted = self._run({"query": "what did the user say about cats"})
        assert "what did the user say about cats" in emitted[0]["text"]

    def test_debug_alert_contains_result_count(self):
        """The debug alert reports how many results were found."""
        _, emitted = self._run({"query": "test"}, result_count=5)
        assert "5" in emitted[0]["text"]

    def test_debug_alert_zero_results(self):
        """Zero results are reported correctly in the alert."""
        _, emitted = self._run({"query": "obscure query"}, result_count=0)
        assert "0" in emitted[0]["text"]

    def test_debug_alert_uses_task_id_as_emitter(self):
        """When task_id is passed, it appears in the alert and as the emitter."""
        _, emitted = self._run({"query": "test", "task_id": "searcher-task-7"})
        assert "searcher-task-7" in emitted[0]["text"]
        assert emitted[0]["emitter"] == "searcher-task-7"

    def test_debug_alert_falls_back_to_dispatcher_label(self):
        """When task_id is absent, 'dispatcher' is used as the agent label."""
        _, emitted = self._run({"query": "test"})
        assert "dispatcher" in emitted[0]["text"]

    def test_debug_alert_category_is_system_context(self):
        """Memory search alerts use system_context category."""
        _, emitted = self._run({"query": "test"})
        assert emitted[0]["category"] == "system_context"

    def test_no_alert_when_debug_alerts_disabled(self):
        """When _DEBUG_ALERTS_ENABLED=False, _emit_debug_observation returns early.

        The outer _DEBUG_MODE gate has been removed; _emit_debug_observation is the
        single authoritative gate.  The handler always calls _emit_debug_observation,
        but the function is a no-op when _DEBUG_ALERTS_ENABLED=False.
        """
        import src.mcp.inbox_server as _mod

        called_with: list[dict] = []

        original_emit = _mod._emit_debug_observation

        def spying_emit(text, category="system_context", visibility="mcp-only", emitter=None):
            called_with.append({"text": text})
            original_emit(text, category=category, visibility=visibility, emitter=emitter)

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=_make_fake_memory_provider(),
            _DEBUG_MODE=False,
            _DEBUG_ALERTS_ENABLED=False,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=spying_emit,
        ):
            from src.mcp.inbox_server import handle_memory_search

            asyncio.run(handle_memory_search({"query": "silent search"}))

        # The handler calls _emit_debug_observation unconditionally (single-gate contract);
        # the function itself is a no-op because _DEBUG_ALERTS_ENABLED=False.
        assert len(called_with) == 1  # called exactly once — no outer gate suppresses it

    def test_alert_is_additive_does_not_affect_return_value(self):
        """Debug alert does not change the handler's return value."""
        result, _ = self._run({"query": "return value test"})
        assert len(result) == 1
        assert "Memory Search Results" in result[0].text


# ---------------------------------------------------------------------------
# Feature 2: write_result debug alerts
# ---------------------------------------------------------------------------


class TestWriteResultDebugAlert:
    """LOBSTER_DEBUG=true fires a debug alert when write_result is called."""

    def _run(self, args: dict, inbox_dir: Path) -> tuple:
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            category: str = "system_context",
            visibility: str = "mcp-only",
            emitter: str | None = None,
        ) -> None:
            emitted.append(
                {
                    "text": text,
                    "category": category,
                    "visibility": visibility,
                    "emitter": emitter,
                }
            )

        # Minimal session store stub
        class FakeSessionStore:
            def session_end(self, **kwargs):
                pass

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=True,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
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
        """write_result emits exactly one debug push when debug mode is on."""
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

    def test_debug_alert_includes_text_preview(self, inbox_dir: Path):
        """The alert includes a preview of the result text (first 60 chars)."""
        _, emitted = self._run(
            {"task_id": "t7", "chat_id": 1, "text": "Short message."},
            inbox_dir,
        )
        assert "Short message." in emitted[0]["text"]

    def test_text_preview_truncated_at_60_chars(self, inbox_dir: Path):
        """Text longer than 60 chars is truncated with an ellipsis in the alert."""
        long_text = "A" * 80
        _, emitted = self._run(
            {"task_id": "t8", "chat_id": 1, "text": long_text},
            inbox_dir,
        )
        assert "A" * 60 in emitted[0]["text"]
        assert "\u2026" in emitted[0]["text"]  # ellipsis
        assert "A" * 61 not in emitted[0]["text"]

    def test_debug_alert_emitter_includes_task_id(self, inbox_dir: Path):
        """The emitter passed to _emit_debug_observation is task:<task_id>."""
        _, emitted = self._run(
            {"task_id": "emitter-check", "chat_id": 1, "text": "Done."},
            inbox_dir,
        )
        assert emitted[0]["emitter"] == "task:emitter-check"

    def test_no_debug_alert_when_debug_alerts_disabled(self, inbox_dir: Path):
        """When _DEBUG_ALERTS_ENABLED=False, _emit_debug_observation is a no-op for write_result.

        The outer _DEBUG_MODE gate has been removed; _emit_debug_observation is the
        single authoritative gate via _DEBUG_ALERTS_ENABLED.  The handler always calls
        _emit_debug_observation, but the function returns early when alerts are disabled.
        """
        import src.mcp.inbox_server as _mod

        called_with: list[dict] = []

        original_emit = _mod._emit_debug_observation

        def spying_emit(text, category="system_context", visibility="mcp-only", emitter=None):
            called_with.append({"text": text})
            original_emit(text, category=category, visibility=visibility, emitter=emitter)

        class FakeSessionStore:
            def session_end(self, **kwargs):
                pass

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=False,
            _DEBUG_ALERTS_ENABLED=False,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=spying_emit,
            _session_store=FakeSessionStore(),
        ):
            with patch("asyncio.create_task"):
                from src.mcp.inbox_server import handle_write_result

                asyncio.run(
                    handle_write_result(
                        {"task_id": "quiet", "chat_id": 1, "text": "Silent."}
                    )
                )

        # The handler calls _emit_debug_observation unconditionally (single-gate contract);
        # the function itself is a no-op because _DEBUG_ALERTS_ENABLED=False.
        assert len(called_with) == 1  # called exactly once — no outer gate suppresses it

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


# ---------------------------------------------------------------------------
# Feature 4: _emit_debug_observation delivers to OUTBOX_DIR (not INBOX_DIR)
# ---------------------------------------------------------------------------


class TestEmitDebugObservationOutboxDelivery:
    """_emit_debug_observation writes to OUTBOX_DIR so the bot delivers it
    directly to Telegram — the dispatcher inbox is never touched."""

    def _call_emit(
        self,
        outbox_dir: Path,
        inbox_dir: Path,
        text: str = "debug text",
        category: str = "system_context",
        visibility: str = "mcp-only",
        emitter: str | None = "test-emitter",
    ) -> None:
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
            INBOX_DIR=inbox_dir,
            _DEBUG_ALERTS_ENABLED=True,
            _DEBUG_RESOLVED=True,
            _DEBUG_OWNER_CHAT_ID=99999,
            _DEBUG_OWNER_SOURCE="telegram",
        ):
            from src.mcp.inbox_server import _emit_debug_observation

            _emit_debug_observation(text, category=category, visibility=visibility, emitter=emitter)

    @pytest.fixture
    def outbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "outbox"

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    def test_writes_to_outbox_not_inbox(self, outbox_dir: Path, inbox_dir: Path):
        """_emit_debug_observation writes to OUTBOX_DIR, not INBOX_DIR."""
        self._call_emit(outbox_dir, inbox_dir)
        assert len(list(outbox_dir.glob("*.json"))) == 1
        assert len(list(inbox_dir.glob("*.json"))) == 0

    def test_outbox_file_has_correct_type(self, outbox_dir: Path, inbox_dir: Path):
        """The outbox file carries type=debug_observation."""
        self._call_emit(outbox_dir, inbox_dir)
        files = list(outbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content["type"] == "debug_observation"

    def test_outbox_file_has_correct_chat_id(self, outbox_dir: Path, inbox_dir: Path):
        """The outbox file carries the configured debug owner chat_id."""
        self._call_emit(outbox_dir, inbox_dir)
        files = list(outbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content["chat_id"] == 99999

    def test_outbox_file_has_correct_source(self, outbox_dir: Path, inbox_dir: Path):
        """The outbox file carries the configured debug owner source."""
        self._call_emit(outbox_dir, inbox_dir)
        files = list(outbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content["source"] == "telegram"

    def test_outbox_file_contains_full_text(self, outbox_dir: Path, inbox_dir: Path):
        """The outbox file text includes the label and the body."""
        self._call_emit(outbox_dir, inbox_dir, text="my debug message")
        files = list(outbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "my debug message" in content["text"]
        assert "[debug|mcp-only]" in content["text"]

    def test_no_write_when_alerts_disabled(self, outbox_dir: Path, inbox_dir: Path):
        """When _DEBUG_ALERTS_ENABLED=False, nothing is written to outbox or inbox."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
            INBOX_DIR=inbox_dir,
            _DEBUG_ALERTS_ENABLED=False,
            _DEBUG_RESOLVED=True,
        ):
            from src.mcp.inbox_server import _emit_debug_observation

            _emit_debug_observation("silent")

        assert len(list(outbox_dir.glob("*.json"))) == 0
        assert len(list(inbox_dir.glob("*.json"))) == 0

    def test_no_write_when_chat_id_none(self, outbox_dir: Path, inbox_dir: Path):
        """When _DEBUG_OWNER_CHAT_ID is None, nothing is written."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
            INBOX_DIR=inbox_dir,
            _DEBUG_ALERTS_ENABLED=True,
            _DEBUG_RESOLVED=True,
            _DEBUG_OWNER_CHAT_ID=None,
        ):
            from src.mcp.inbox_server import _emit_debug_observation

            _emit_debug_observation("no chat id")

        assert len(list(outbox_dir.glob("*.json"))) == 0
        assert len(list(inbox_dir.glob("*.json"))) == 0
