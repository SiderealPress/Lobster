"""
Tests for the claim_and_ack MCP tool.

claim_and_ack atomically moves a message from inbox/ to processing/ AND
queues an acknowledgement reply. It is the preferred way to claim a long-
running message so the user is notified in the same operation as the claim.
"""

import json
import sys
import asyncio
import pytest
from pathlib import Path
from unittest.mock import patch

# Ensure src/mcp is on sys.path so that sibling modules resolve correctly.
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401 — pre-load for patch.multiple


class TestClaimAndAck:
    """Tests for handle_claim_and_ack."""

    @pytest.fixture
    def dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        outbox = temp_messages_dir / "outbox"
        sent = temp_messages_dir / "sent"
        sent.mkdir(exist_ok=True)
        return inbox, processing, outbox, sent

    def _write_inbox_message(self, inbox: Path, chat_id: int = 123456) -> str:
        """Write a minimal inbox message and return its ID."""
        msg_id = "1700000000000_telegram"
        msg = {
            "id": msg_id,
            "source": "telegram",
            "chat_id": chat_id,
            "type": "text",
            "text": "Please do the thing",
            "timestamp": "2026-03-16T10:00:00.000000",
        }
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))
        return msg_id

    def test_moves_message_to_processing(self, dirs):
        """Message is moved from inbox/ to processing/ on success."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        assert not (inbox / f"{msg_id}.json").exists(), "Message should be gone from inbox/"
        assert (processing / f"{msg_id}.json").exists(), "Message should be in processing/"

    def test_sends_ack_reply(self, dirs):
        """An ack reply file is written to outbox/ after claiming."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        outbox_files = list(outbox.glob("*.json"))
        assert len(outbox_files) == 1, "Expected exactly one ack reply in outbox/"
        reply = json.loads(outbox_files[0].read_text())
        assert reply["text"] == "On it."
        assert reply["chat_id"] == 123456
        assert reply["source"] == "telegram"

    def test_stamps_processing_started_at(self, dirs):
        """The processing/ copy carries _processing_started_at for stale detection."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        msg_data = json.loads((processing / f"{msg_id}.json").read_text())
        assert "_processing_started_at" in msg_data, (
            "processing/ copy must carry _processing_started_at for stale detection"
        )

    def test_returns_error_when_message_not_found(self, dirs):
        """Returns an error without sending an ack if the message is not in inbox/."""
        inbox, processing, outbox, sent = dirs
        # Inbox is empty — message does not exist

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            result = asyncio.run(handle_claim_and_ack({
                "message_id": "nonexistent_1234",
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        assert "Error" in result[0].text or "not found" in result[0].text.lower()
        # No ack sent
        assert list(outbox.glob("*.json")) == [], "No ack should be sent when claim fails"

    def test_no_ack_sent_when_claim_fails(self, dirs):
        """The ack is never sent when the message cannot be claimed (already claimed)."""
        inbox, processing, outbox, sent = dirs
        # Simulate a message already in processing/ (not in inbox/)
        msg_id = "1700000000000_telegram"
        msg = {"id": msg_id, "source": "telegram", "chat_id": 123456, "text": "Hello"}
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            result = asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        assert "not found" in result[0].text.lower() or "Error" in result[0].text
        assert list(outbox.glob("*.json")) == [], "Ack must not be sent when claim fails"

    def test_success_result_text_contains_message_id(self, dirs):
        """The success result text includes the claimed message ID."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            result = asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        # The partial message ID should appear in the result text
        assert "1700000000000" in result[0].text or msg_id in result[0].text

    def test_requires_message_id(self, dirs):
        """Missing message_id raises ValidationError (caught at call_tool boundary)."""
        inbox, processing, outbox, sent = dirs

        # ValidationError is raised by validate_message_id and propagates up through
        # asyncio.run. The call_tool dispatcher catches it — but we test the handler
        # directly here, so we expect the exception to surface.
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack
            import reliability

            raised = False
            try:
                asyncio.run(handle_claim_and_ack({
                    "ack_text": "On it.",
                    "chat_id": 123456,
                }))
            except reliability.ValidationError:
                raised = True

            assert raised, "Expected ValidationError when message_id is missing"

    def test_partial_failure_when_ack_send_fails(self, dirs):
        """When mark_processing succeeds but send_reply raises, returns Warning: prefix.

        The message must remain in processing/ (no rollback — documented behaviour).
        """
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            with patch("src.mcp.inbox_server.handle_send_reply", side_effect=RuntimeError("network timeout")):
                result = asyncio.run(handle_claim_and_ack({
                    "message_id": msg_id,
                    "ack_text": "On it.",
                    "chat_id": 123456,
                    "source": "telegram",
                }))

        # Partial-failure response starts with Warning:
        assert result[0].text.startswith("Warning:"), (
            f"Expected result text to start with 'Warning:', got: {result[0].text!r}"
        )
        assert "network timeout" in result[0].text, "Error detail should appear in Warning text"

        # Message must remain in processing/ — no rollback
        assert not (inbox / f"{msg_id}.json").exists(), "Message should not be back in inbox/"
        assert (processing / f"{msg_id}.json").exists(), (
            "Message must stay in processing/ when ack fails (no rollback)"
        )

    def test_reply_to_message_id_forwarded_to_ack(self, dirs):
        """reply_to_message_id is passed through to the ack reply for Telegram threading."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
                "reply_to_message_id": 9001,
            }))

        outbox_files = list(outbox.glob("*.json"))
        assert len(outbox_files) == 1
        reply = json.loads(outbox_files[0].read_text())
        assert reply.get("reply_to_message_id") == 9001, (
            "reply_to_message_id must be forwarded to the ack for Telegram threading"
        )

    def test_counter_increments_for_user_message(self, dirs):
        """Processing a real user message via claim_and_ack increments _user_message_counter.

        This verifies that the counter ticks even when the dispatcher uses claim_and_ack
        rather than mark_processing, so the session_note_reminder interval is not skipped.
        """
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            import src.mcp.inbox_server as srv
            srv._user_message_counter = 0

            from src.mcp.inbox_server import handle_claim_and_ack

            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

            assert srv._user_message_counter == 1, (
                "claim_and_ack must increment _user_message_counter for real user messages"
            )

    def test_counter_injects_reminder_at_interval(self, dirs):
        """When claim_and_ack processes the Nth user message (N == SESSION_NOTE_REMINDER_INTERVAL),
        a session_note_reminder is injected into the inbox.

        This ensures the session-note-appender trigger fires whether the dispatcher
        uses mark_processing or claim_and_ack to claim messages.
        """
        inbox, processing, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            import src.mcp.inbox_server as srv
            interval = srv.SESSION_NOTE_REMINDER_INTERVAL
            # Prime the counter to one below the trigger threshold
            srv._user_message_counter = interval - 1

            from src.mcp.inbox_server import handle_claim_and_ack

            # Write and claim the Nth message, which should trigger the reminder
            msg_id = f"{interval}_telegram"
            msg = {
                "id": msg_id,
                "source": "telegram",
                "chat_id": 123456,
                "type": "text",
                "text": "Trigger message",
                "timestamp": "2026-03-31T10:00:00.000000",
            }
            (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "Got it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

            # The inbox should now contain a session_note_reminder file
            reminder_files = [
                f for f in inbox.glob("*.json")
                if "session_note_reminder" in f.name
            ]
            assert len(reminder_files) == 1, (
                f"Expected 1 session_note_reminder in inbox after claim_and_ack at "
                f"message {interval}, found {len(reminder_files)}"
            )
            reminder = json.loads(reminder_files[0].read_text())
            assert reminder["type"] == "session_note_reminder"
            assert reminder["user_message_count"] == interval

    def test_counter_does_not_increment_for_system_messages(self, dirs):
        """Claiming a system/subagent message via claim_and_ack must NOT increment the counter."""
        inbox, processing, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
        ):
            import src.mcp.inbox_server as srv
            srv._user_message_counter = 0

            from src.mcp.inbox_server import handle_claim_and_ack

            # Write a subagent_result message — should not count
            msg_id = "1700000000001_system"
            msg = {
                "id": msg_id,
                "source": "system",
                "chat_id": 0,
                "type": "subagent_result",
                "text": "PR opened.",
                "timestamp": "2026-03-31T10:01:00.000000",
            }
            (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "Noted.",
                "chat_id": 0,
                "source": "system",
            }))

            assert srv._user_message_counter == 0, (
                "System/subagent messages must not increment _user_message_counter"
            )
