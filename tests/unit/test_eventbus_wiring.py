"""
Unit tests for EventBus wiring of remaining event sources (issue #1352).

Verifies that the four event sources emit correctly-typed events to the bus:
- telegram.inbound  — emitted in handle_mark_processing when a human message is claimed
- telegram.outbound — emitted in handle_send_reply when a reply is sent
- job.started / job.completed — emitted in handle_write_task_output
- inbox.processed   — emitted in handle_mark_processed
- inbox.failed      — emitted in handle_mark_failed

Each test follows the same pattern as test_emit_event.py:
- Install a collecting listener on the module-level bus
- Patch _EVENT_BUS_AVAILABLE + get_event_bus to inject the bus
- Assert the event fields match what the spec requires

Tests are named after behavior, not mechanism.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _make_bus_with_listener() -> tuple[EventBus, _CollectingListener]:
    """Return a fresh EventBus with a collecting listener attached."""
    import event_bus as _eb
    _eb._EVENT_BUS = None
    bus = _eb.get_event_bus()
    listener = _CollectingListener()
    bus.register(listener)
    return bus, listener


def _events_of_type(listener: _CollectingListener, event_type: str) -> list[LobsterEvent]:
    return [e for e in listener.received if e.event_type == event_type]


# ---------------------------------------------------------------------------
# telegram.outbound — emitted when send_reply delivers a message
# ---------------------------------------------------------------------------

class TestTelegramOutboundEvent:
    """send_reply emits a telegram.outbound event to the bus."""

    def _run_send_reply(self, bus: EventBus, outbox_dir: Path) -> None:
        import inbox_server
        # Patch everything that send_reply depends on for filesystem I/O
        with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
             patch("inbox_server.get_event_bus", return_value=bus), \
             patch.object(inbox_server, "OUTBOX_DIR", outbox_dir), \
             patch.object(inbox_server, "SENT_DIR", outbox_dir), \
             patch.object(inbox_server, "BISQUE_OUTBOX_DIR", outbox_dir), \
             patch.object(inbox_server, "_db_persist_outbound", None), \
             patch.object(inbox_server, "_track_reply", MagicMock()), \
             patch.object(inbox_server, "_record_direct_send", MagicMock()), \
             patch.object(inbox_server, "_record_task_replied", MagicMock()):
            asyncio.run(inbox_server.handle_send_reply({
                "chat_id": 9999,
                "text": "Hello from Lobster",
                "source": "telegram",
            }))

    def test_send_reply_emits_telegram_outbound_event(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._run_send_reply(bus, Path(tmpdir))
        events = _events_of_type(listener, "telegram.outbound")
        assert len(events) == 1, f"Expected 1 telegram.outbound event, got {len(events)}"

    def test_telegram_outbound_event_has_required_fields(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._run_send_reply(bus, Path(tmpdir))
        ev = _events_of_type(listener, "telegram.outbound")[0]
        assert ev.source == "inbox-server"
        assert "chat_id" in ev.payload
        assert ev.payload["chat_id"] == 9999
        assert "text_len" in ev.payload

    def test_telegram_outbound_event_severity_is_info(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._run_send_reply(bus, Path(tmpdir))
        ev = _events_of_type(listener, "telegram.outbound")[0]
        assert ev.severity == "info"

    def test_send_reply_does_not_emit_outbound_for_bot_talk_source(self):
        """bot-talk messages already go through bot_talk_mirror — no duplicate event."""
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_dir = Path(tmpdir)
            import inbox_server
            # bot_talk_mirror is imported locally inside handle_send_reply, so we
            # patch at the module level inside the bot_talk_mirror module itself.
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "OUTBOX_DIR", outbox_dir), \
                 patch.object(inbox_server, "SENT_DIR", outbox_dir), \
                 patch.object(inbox_server, "BISQUE_OUTBOX_DIR", outbox_dir), \
                 patch.object(inbox_server, "_db_persist_outbound", None), \
                 patch.object(inbox_server, "_track_reply", MagicMock()), \
                 patch.object(inbox_server, "_record_direct_send", MagicMock()), \
                 patch.object(inbox_server, "_record_task_replied", MagicMock()), \
                 patch("bot_talk_mirror.mirror_outbound", MagicMock()):
                asyncio.run(inbox_server.handle_send_reply({
                    "chat_id": "AlbertLobster",
                    "text": "Hi from bot-talk",
                    "source": "bot-talk",
                }))
        # telegram.outbound should NOT be emitted for bot-talk
        events = _events_of_type(listener, "telegram.outbound")
        assert len(events) == 0, "telegram.outbound must not be emitted for bot-talk source"


# ---------------------------------------------------------------------------
# telegram.inbound — emitted when mark_processing claims a human message
# ---------------------------------------------------------------------------

class TestTelegramInboundEvent:
    """mark_processing emits a telegram.inbound event for human messages."""

    def _write_inbox_message(self, inbox_dir: Path, msg_id: str, msg_type: str = "text", source: str = "telegram") -> None:
        msg = {
            "id": msg_id,
            "type": msg_type,
            "source": source,
            "chat_id": 7777,
            "text": "Hello Lobster",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        (inbox_dir / f"{msg_id}.json").write_text(json.dumps(msg))

    def test_mark_processing_emits_telegram_inbound_for_user_message(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            inbox_dir = Path(tmpdir) / "inbox"
            processing_dir = Path(tmpdir) / "processing"
            inbox_dir.mkdir()
            processing_dir.mkdir()
            msg_id = "1234567890_telegram"
            self._write_inbox_message(inbox_dir, msg_id, msg_type="text", source="telegram")

            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "INBOX_DIR", inbox_dir), \
                 patch.object(inbox_server, "PROCESSING_DIR", processing_dir), \
                 patch.object(inbox_server, "_claims_db") as mock_claims, \
                 patch.object(inbox_server, "_queue_observation", MagicMock()), \
                 patch.object(inbox_server, "_user_model", None), \
                 patch.object(inbox_server, "_tick_user_message_counter", MagicMock()), \
                 patch.object(inbox_server, "_get_current_http_session_id", return_value=None):
                mock_claims.claim.return_value = True
                asyncio.run(inbox_server.handle_mark_processing({"message_id": msg_id}))

        events = _events_of_type(listener, "telegram.inbound")
        assert len(events) == 1, f"Expected 1 telegram.inbound event, got {len(events)}"

    def test_telegram_inbound_event_has_required_fields(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            inbox_dir = Path(tmpdir) / "inbox"
            processing_dir = Path(tmpdir) / "processing"
            inbox_dir.mkdir()
            processing_dir.mkdir()
            msg_id = "1234567890_telegram"
            self._write_inbox_message(inbox_dir, msg_id)

            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "INBOX_DIR", inbox_dir), \
                 patch.object(inbox_server, "PROCESSING_DIR", processing_dir), \
                 patch.object(inbox_server, "_claims_db") as mock_claims, \
                 patch.object(inbox_server, "_queue_observation", MagicMock()), \
                 patch.object(inbox_server, "_user_model", None), \
                 patch.object(inbox_server, "_tick_user_message_counter", MagicMock()), \
                 patch.object(inbox_server, "_get_current_http_session_id", return_value=None):
                mock_claims.claim.return_value = True
                asyncio.run(inbox_server.handle_mark_processing({"message_id": msg_id}))

        ev = _events_of_type(listener, "telegram.inbound")[0]
        assert "message_id" in ev.payload
        assert "source" in ev.payload
        assert "msg_type" in ev.payload
        assert ev.severity == "info"


# ---------------------------------------------------------------------------
# inbox.processed — emitted when mark_processed moves a message to processed/
# ---------------------------------------------------------------------------

class TestInboxProcessedEvent:
    """mark_processed emits an inbox.processed event."""

    def _write_processing_message(self, processing_dir: Path, msg_id: str) -> None:
        msg = {
            "id": msg_id,
            "type": "text",
            "source": "telegram",
            "chat_id": 5555,
            "text": "Done",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        (processing_dir / f"{msg_id}.json").write_text(json.dumps(msg))

    def test_mark_processed_emits_inbox_processed_event(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            processing_dir = Path(tmpdir) / "processing"
            processed_dir = Path(tmpdir) / "processed"
            processing_dir.mkdir()
            processed_dir.mkdir()
            msg_id = "9876543210_telegram"
            self._write_processing_message(processing_dir, msg_id)

            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "PROCESSING_DIR", processing_dir), \
                 patch.object(inbox_server, "INBOX_DIR", processing_dir), \
                 patch.object(inbox_server, "PROCESSED_DIR", processed_dir), \
                 patch.object(inbox_server, "_claims_db") as mock_claims, \
                 patch.object(inbox_server, "_db_persist_inbound", None), \
                 patch.object(inbox_server, "_update_lobster_state_fields", MagicMock()), \
                 patch.object(inbox_server, "_recent_replies", {}):
                mock_claims.update_status = MagicMock()
                asyncio.run(inbox_server.handle_mark_processed({"message_id": msg_id, "force": True}))

        events = _events_of_type(listener, "inbox.processed")
        assert len(events) == 1, f"Expected 1 inbox.processed event, got {len(events)}"

    def test_inbox_processed_event_has_message_id_in_payload(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            processing_dir = Path(tmpdir) / "processing"
            processed_dir = Path(tmpdir) / "processed"
            processing_dir.mkdir()
            processed_dir.mkdir()
            msg_id = "9876543210_telegram"
            self._write_processing_message(processing_dir, msg_id)

            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "PROCESSING_DIR", processing_dir), \
                 patch.object(inbox_server, "INBOX_DIR", processing_dir), \
                 patch.object(inbox_server, "PROCESSED_DIR", processed_dir), \
                 patch.object(inbox_server, "_claims_db") as mock_claims, \
                 patch.object(inbox_server, "_db_persist_inbound", None), \
                 patch.object(inbox_server, "_update_lobster_state_fields", MagicMock()), \
                 patch.object(inbox_server, "_recent_replies", {}):
                mock_claims.update_status = MagicMock()
                asyncio.run(inbox_server.handle_mark_processed({"message_id": msg_id, "force": True}))

        ev = _events_of_type(listener, "inbox.processed")[0]
        assert ev.payload.get("message_id") == msg_id
        assert ev.severity == "info"


# ---------------------------------------------------------------------------
# inbox.failed — emitted when mark_failed moves a message to failed/
# ---------------------------------------------------------------------------

class TestInboxFailedEvent:
    """mark_failed emits an inbox.failed event."""

    def _write_processing_message(self, processing_dir: Path, msg_id: str) -> None:
        msg = {
            "id": msg_id,
            "type": "text",
            "source": "telegram",
            "chat_id": 4444,
            "text": "oops",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        (processing_dir / f"{msg_id}.json").write_text(json.dumps(msg))

    def test_mark_failed_emits_inbox_failed_event(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            processing_dir = Path(tmpdir) / "processing"
            failed_dir = Path(tmpdir) / "failed"
            processing_dir.mkdir()
            failed_dir.mkdir()
            msg_id = "1111111111_telegram"
            self._write_processing_message(processing_dir, msg_id)

            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "PROCESSING_DIR", processing_dir), \
                 patch.object(inbox_server, "INBOX_DIR", processing_dir), \
                 patch.object(inbox_server, "FAILED_DIR", failed_dir), \
                 patch.object(inbox_server, "_claims_db") as mock_claims:
                mock_claims.update_status = MagicMock()
                mock_claims.release = MagicMock()
                asyncio.run(inbox_server.handle_mark_failed({
                    "message_id": msg_id,
                    "error": "test error",
                    "max_retries": 0,  # force permanent failure immediately
                }))

        events = _events_of_type(listener, "inbox.failed")
        assert len(events) == 1, f"Expected 1 inbox.failed event, got {len(events)}"

    def test_inbox_failed_event_contains_error_info(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            processing_dir = Path(tmpdir) / "processing"
            failed_dir = Path(tmpdir) / "failed"
            processing_dir.mkdir()
            failed_dir.mkdir()
            msg_id = "1111111111_telegram"
            self._write_processing_message(processing_dir, msg_id)

            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "PROCESSING_DIR", processing_dir), \
                 patch.object(inbox_server, "INBOX_DIR", processing_dir), \
                 patch.object(inbox_server, "FAILED_DIR", failed_dir), \
                 patch.object(inbox_server, "_claims_db") as mock_claims:
                mock_claims.update_status = MagicMock()
                mock_claims.release = MagicMock()
                asyncio.run(inbox_server.handle_mark_failed({
                    "message_id": msg_id,
                    "error": "my error message",
                    "max_retries": 0,
                }))

        ev = _events_of_type(listener, "inbox.failed")[0]
        assert ev.payload.get("message_id") == msg_id
        assert "error" in ev.payload
        assert ev.severity == "warn"


# ---------------------------------------------------------------------------
# job.completed — emitted when write_task_output records a job result
# ---------------------------------------------------------------------------

class TestJobCompletedEvent:
    """write_task_output emits a job.completed event carrying job_name and status."""

    def test_write_task_output_emits_job_completed_event(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs_dir = Path(tmpdir)
            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "TASK_OUTPUTS_DIR", outputs_dir):
                asyncio.run(inbox_server.handle_write_task_output({
                    "job_name": "nightly-check",
                    "output": "All systems green",
                    "status": "success",
                }))

        events = _events_of_type(listener, "job.completed")
        assert len(events) == 1, f"Expected 1 job.completed event, got {len(events)}"

    def test_job_completed_event_has_job_name_and_status(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs_dir = Path(tmpdir)
            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "TASK_OUTPUTS_DIR", outputs_dir):
                asyncio.run(inbox_server.handle_write_task_output({
                    "job_name": "nightly-check",
                    "output": "All systems green",
                    "status": "success",
                }))

        ev = _events_of_type(listener, "job.completed")[0]
        assert ev.payload.get("job_name") == "nightly-check"
        assert ev.payload.get("status") == "success"
        assert ev.severity == "info"

    def test_job_completed_event_severity_is_warn_on_failure(self):
        bus, listener = _make_bus_with_listener()
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs_dir = Path(tmpdir)
            import inbox_server
            with patch.object(inbox_server, "_EVENT_BUS_AVAILABLE", True), \
                 patch("inbox_server.get_event_bus", return_value=bus), \
                 patch.object(inbox_server, "TASK_OUTPUTS_DIR", outputs_dir):
                asyncio.run(inbox_server.handle_write_task_output({
                    "job_name": "nightly-check",
                    "output": "Something went wrong",
                    "status": "failed",
                }))

        ev = _events_of_type(listener, "job.completed")[0]
        assert ev.payload.get("status") == "failed"
        assert ev.severity == "warn"
