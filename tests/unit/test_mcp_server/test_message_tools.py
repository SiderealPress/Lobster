"""
Tests for MCP Server Message Tools

Tests check_inbox, send_reply, mark_processed, list_sources, get_stats
"""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Ensure src/mcp is on sys.path so that `reliability` (a sibling module) can
# be resolved when inbox_server is imported via the `src.mcp.inbox_server`
# dotted path.  The root conftest adds `src/` but not `src/mcp/`, so we add
# the latter here; this is a no-op if the path is already present.
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

# Pre-load the module so that unittest.mock can resolve "src.mcp.inbox_server"
# as an attribute of the `src.mcp` package before patch.multiple opens.
import src.mcp.inbox_server  # noqa: F401

# We'll test the handlers directly by importing them
# and patching the directory constants


class TestCheckInbox:
    """Tests for check_inbox tool."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def test_empty_inbox_returns_no_messages(self, inbox_dir: Path):
        """Test that empty inbox returns appropriate message."""
        # Patch the module-level constants
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            assert len(result) == 1
            assert "No new messages" in result[0].text

    def test_returns_messages_from_inbox(
        self, inbox_dir: Path, message_generator
    ):
        """Test that messages in inbox are returned."""
        # Create some messages
        for i in range(3):
            msg = message_generator.generate_text_message(text=f"Test message {i}")
            (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            assert len(result) == 1
            assert "3 new message" in result[0].text
            assert "Test message" in result[0].text

    def test_respects_limit_parameter(self, inbox_dir: Path, message_generator):
        """Test that limit parameter restricts returned messages."""
        # Create 10 messages
        for i in range(10):
            msg = message_generator.generate_text_message()
            (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({"limit": 3}))

            assert "3 new message" in result[0].text

    def test_filters_by_source(self, inbox_dir: Path, message_generator):
        """Test that source filter works correctly."""
        # Create messages from different sources
        for source in ["telegram", "telegram", "sms"]:
            msg = message_generator.generate_text_message(source=source)
            (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({"source": "telegram"}))

            assert "2 new message" in result[0].text

    def test_handles_corrupted_file(self, inbox_dir: Path, message_generator):
        """Test that corrupted files are skipped gracefully."""
        # Create valid message
        msg = message_generator.generate_text_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        # Create corrupted file
        (inbox_dir / "corrupted.json").write_text("not valid json {{{")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            # Should return the valid message without error
            assert "1 new message" in result[0].text

    def test_voice_message_indicator(self, inbox_dir: Path, message_generator):
        """Test that voice messages are indicated."""
        msg = message_generator.generate_voice_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))

            assert "Voice message needs transcription" in result[0].text


class TestSendReply:
    """Tests for send_reply tool."""

    @pytest.fixture
    def outbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get outbox directory."""
        return temp_messages_dir / "outbox"

    def test_creates_reply_file(self, outbox_dir: Path):
        """Test that reply file is created in outbox."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                    "text": "Hello, this is a reply!",
                    "source": "telegram",
                })
            )

            assert "Reply queued" in result[0].text

            # Check file was created
            files = list(outbox_dir.glob("*.json"))
            assert len(files) == 1

            content = json.loads(files[0].read_text())
            assert content["chat_id"] == 123456
            assert content["text"] == "Hello, this is a reply!"
            assert content["source"] == "telegram"

    def test_requires_chat_id(self, outbox_dir: Path):
        """Test that chat_id is required."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "text": "Hello!",
                })
            )

            assert "Error" in result[0].text
            assert "required" in result[0].text

    def test_requires_text(self, outbox_dir: Path):
        """Test that text is required."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                })
            )

            assert "Error" in result[0].text

    def test_handles_unicode_text(self, outbox_dir: Path):
        """Test that Unicode text is handled correctly."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            unicode_text = "Hello! \U0001f600 \u4e2d\u6587 \u0420\u0443\u0441\u0441\u043a\u0438\u0439"
            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                    "text": unicode_text,
                })
            )

            assert "Reply queued" in result[0].text

            files = list(outbox_dir.glob("*.json"))
            content = json.loads(files[0].read_text())
            assert content["text"] == unicode_text

    def test_default_source_is_telegram(self, outbox_dir: Path):
        """Test that default source is telegram."""
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            result = asyncio.run(
                handle_send_reply({
                    "chat_id": 123456,
                    "text": "Hello!",
                })
            )

            files = list(outbox_dir.glob("*.json"))
            content = json.loads(files[0].read_text())
            assert content["source"] == "telegram"


class TestAutoThreading:
    """Tests for automatic reply threading (issue #330).

    When send_reply is called for a Telegram message without an explicit
    reply_to_message_id, it should auto-look up the telegram_message_id from
    any message currently in processing/ for that chat_id and thread against it.

    For the subagent reply case (the common path), the original message has
    already been moved from processing/ to processed/ by the time send_reply is
    called.  The lookup must fall back to scanning processed/ (newest-first) so
    that threading still works.
    """

    @pytest.fixture
    def dirs(self, temp_messages_dir: Path):
        outbox = temp_messages_dir / "outbox"
        processing = temp_messages_dir / "processing"
        processed = temp_messages_dir / "processed"
        sent = temp_messages_dir / "sent"
        outbox.mkdir(parents=True, exist_ok=True)
        processing.mkdir(parents=True, exist_ok=True)
        processed.mkdir(parents=True, exist_ok=True)
        sent.mkdir(parents=True, exist_ok=True)
        return outbox, processing, processed, sent

    def _make_processing_msg(self, processing_dir: Path, chat_id: int, tg_msg_id: int) -> None:
        """Write a fake processing message with a telegram_message_id."""
        msg = {
            "id": f"12345_msg",
            "source": "telegram",
            "chat_id": chat_id,
            "type": "text",
            "text": "Hello",
            "telegram_message_id": tg_msg_id,
            "timestamp": "2026-03-14T12:00:00.000000",
        }
        (processing_dir / "12345_msg.json").write_text(json.dumps(msg))

    def test_auto_threads_when_processing_message_exists(self, dirs):
        """Auto-threading was removed (PR #420): reply is standalone even when processing/ has a message.

        Previously send_reply would look up telegram_message_id from processing/ and set
        reply_to_message_id automatically.  That behaviour was removed to avoid threading
        under the wrong message when multiple messages are in-flight.  Without an explicit
        reply_to_message_id the reply is now sent standalone.
        """
        outbox, processing, processed, sent = dirs
        self._make_processing_msg(processing, chat_id=123456, tg_msg_id=9001)

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "Reply without auto-threading",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        assert len(files) == 1
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "reply_to_message_id should not be auto-set: auto-threading was removed in PR #420"
        )

    def test_explicit_reply_id_takes_precedence_over_auto(self, dirs):
        """An explicit reply_to_message_id overrides auto-threading."""
        outbox, processing, processed, sent = dirs
        self._make_processing_msg(processing, chat_id=123456, tg_msg_id=9001)

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "Explicit thread",
                "source": "telegram",
                "reply_to_message_id": 5555,
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content.get("reply_to_message_id") == 5555, (
            "Explicit reply_to_message_id should win over auto-threading"
        )

    def test_no_threading_when_no_processing_message(self, dirs):
        """When both processing/ and processed/ are empty, reply_to_message_id is absent."""
        outbox, processing, processed, sent = dirs
        # No message in processing/ or processed/

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "No thread",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "reply_to_message_id should be absent when no processing message exists"
        )

    def test_no_threading_for_wrong_chat_id(self, dirs):
        """Processing message for a different chat_id is not used for threading."""
        outbox, processing, processed, sent = dirs
        self._make_processing_msg(processing, chat_id=999999, tg_msg_id=9001)

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,  # different from 999999
                "text": "Different chat",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "Should not thread against a message for a different chat_id"
        )

    def test_no_auto_threading_for_slack_source(self, dirs):
        """Auto-threading only applies to telegram; Slack messages are unaffected."""
        outbox, processing, processed, sent = dirs
        # Put a processing message with tg_msg_id for this chat
        self._make_processing_msg(processing, chat_id=123456, tg_msg_id=9001)

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "Slack reply",
                "source": "slack",
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "reply_to_message_id should not be set for Slack replies"
        )

    def test_processing_message_without_tg_msg_id_is_skipped(self, dirs):
        """Processing message lacking telegram_message_id does not trigger threading."""
        outbox, processing, processed, sent = dirs
        # Message without telegram_message_id
        msg = {
            "id": "no_tg_id_msg",
            "source": "telegram",
            "chat_id": 123456,
            "type": "text",
            "text": "No tg msg id",
            "timestamp": "2026-03-14T12:00:00.000000",
        }
        (processing / "no_tg_id_msg.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "No tg id processing msg",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content

    # ------------------------------------------------------------------
    # Subagent reply case (the common path): message already in processed/
    # ------------------------------------------------------------------

    def _make_processed_msg(
        self, processed_dir: Path, chat_id: int, tg_msg_id: int, filename: str = "12345_msg.json"
    ) -> None:
        """Write a fake processed message with a telegram_message_id."""
        msg = {
            "id": filename.replace(".json", ""),
            "source": "telegram",
            "chat_id": chat_id,
            "type": "text",
            "text": "Hello",
            "telegram_message_id": tg_msg_id,
            "timestamp": "2026-03-14T12:00:00.000000",
        }
        (processed_dir / filename).write_text(json.dumps(msg))

    def test_auto_threads_from_processed_when_processing_empty(self, dirs):
        """Auto-threading fallback via processed/ was also removed (PR #361 then #420).

        Both the processing/ lookup and the processed/ fallback were removed.  Replies are
        sent standalone unless reply_to_message_id is supplied explicitly.
        """
        outbox, processing, processed, sent = dirs
        self._make_processed_msg(processed, chat_id=123456, tg_msg_id=9001)
        # processing/ is empty

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "Subagent reply",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        assert len(files) == 1
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "reply_to_message_id should not be auto-set: processed/ fallback was removed in PR #361"
        )

    def test_processing_takes_priority_over_processed(self, dirs):
        """Auto-threading removed: neither processing/ nor processed/ sets reply_to_message_id.

        Previously processing/ took priority over processed/ for auto-threading.  Both lookups
        were removed (PRs #361, #420).  With messages in both dirs, the reply is still standalone.
        """
        outbox, processing, processed, sent = dirs
        self._make_processing_msg(processing, chat_id=123456, tg_msg_id=9001)
        self._make_processed_msg(processed, chat_id=123456, tg_msg_id=7777)

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "Should be sent standalone",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "reply_to_message_id should not be set: auto-threading removed in PRs #361 and #420"
        )

    def test_processed_fallback_wrong_chat_id_not_used(self, dirs):
        """processed/ message for a different chat_id is not used for threading."""
        outbox, processing, processed, sent = dirs
        # processed/ has a message for a different chat
        self._make_processed_msg(processed, chat_id=999999, tg_msg_id=9001)

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "Wrong chat processed msg",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "Should not thread against a processed message for a different chat_id"
        )

    def test_processed_fallback_uses_most_recent(self, dirs):
        """Auto-threading removed: multiple processed messages still yield no reply_to_message_id.

        Previously the most recently modified processed/ message was used for threading.
        That fallback was removed in PR #361; neither processed/ message sets reply_to_message_id.
        """
        import time as _time

        outbox, processing, processed, sent = dirs

        self._make_processed_msg(processed, chat_id=123456, tg_msg_id=1111, filename="old_msg.json")
        _time.sleep(0.02)
        self._make_processed_msg(processed, chat_id=123456, tg_msg_id=2222, filename="new_msg.json")

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
            SENT_DIR=sent,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 123456,
                "text": "Should be sent standalone regardless of processed/ contents",
                "source": "telegram",
            }))

        files = list(outbox.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "reply_to_message_id" not in content, (
            "reply_to_message_id should not be set: processed/ fallback removed in PR #361"
        )


class TestMarkProcessed:
    """Tests for mark_processed tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        """Set up inbox and processed directories."""
        inbox = temp_messages_dir / "inbox"
        processed = temp_messages_dir / "processed"
        return inbox, processed

    def test_moves_file_to_processed(self, setup_dirs, message_generator):
        """Test that message file is moved to processed directory (with force)."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id, "force": True}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (processed / f"{msg_id}.json").exists()

    def test_finds_by_partial_id(self, setup_dirs, message_generator):
        """Test that message can be found by partial ID match."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        # Use just part of the ID
        partial_id = msg_id.split("_")[0]

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": partial_id, "force": True}))

            assert "processed" in result[0].text.lower()

    def test_not_found_returns_error(self, setup_dirs):
        """Test that non-existent message returns error."""
        inbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(
                handle_mark_processed({"message_id": "nonexistent_id"})
            )

            assert "not found" in result[0].text.lower()

    def test_requires_message_id(self, setup_dirs):
        """Test that message_id is required."""
        inbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({}))

            assert "Error" in result[0].text


    def test_blocks_human_message_without_reply(self, setup_dirs, message_generator):
        """Guard auto-sends fallback reply for real chat_ids; skips for test/fake chat_ids.

        For chat_ids <= 1_000_000 (test/fake IDs that Telegram would reject), the guard
        skips the auto-reply fallback but still marks the message as processed.  The old
        soft-warning behaviour ("No reply sent") was replaced by an auto-send-then-proceed
        pattern to prevent silent message drops in production.
        """
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message(
            source="telegram", chat_id=123456,  # fake/test chat_id — guard skips auto-reply
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            # For fake/test chat_ids the guard skips auto-reply but still marks processed
            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (processed / f"{msg_id}.json").exists()

    def test_allows_human_message_with_force(self, setup_dirs, message_generator):
        """Test that force=True bypasses the reply guard."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message(
            source="telegram", chat_id=123456,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id, "force": True}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()

    def test_allows_system_message_without_reply(self, setup_dirs, message_generator):
        """Test that system/internal messages are not guarded."""
        inbox, processed = setup_dirs

        msg = message_generator.generate_text_message(
            source="internal", chat_id=0,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()

    def test_allows_after_reply_sent(self, setup_dirs, message_generator):
        """Test that mark_processed works after send_reply was called."""
        inbox, processed = setup_dirs
        import time as _time

        msg = message_generator.generate_text_message(
            source="telegram", chat_id=999,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed, _track_reply

            # Simulate that a reply was sent to this chat_id
            _track_reply(999)

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()

    # -----------------------------------------------------------------------
    # Issue #1594 — "Noted." fallback removal
    # -----------------------------------------------------------------------

    def test_no_fallback_sent_for_real_chat_id_without_reply(
        self, setup_dirs, message_generator, inbox_server_dirs
    ):
        """mark_processed must NOT write any outbox message when no reply was sent.

        Before fix: the server auto-sent "Noted." to the outbox for any
        user-facing message with a real (> 1_000_000) chat_id that was
        processed without a prior send_reply.

        After fix: the message is silently marked processed and no outbox
        file is written.  The absence of a fallback is the correct behavior —
        a missing reply is a dispatcher bug, not something to paper over.
        """
        inbox, processed = setup_dirs
        outbox = inbox_server_dirs["outbox"]

        # Use a real (large) chat_id that previously triggered the fallback
        REAL_CHAT_ID = 8305714125
        msg = message_generator.generate_text_message(
            source="telegram", chat_id=REAL_CHAT_ID,
        )
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        outbox_before = set(outbox.iterdir())

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

        # Message must be moved to processed
        assert "processed" in result[0].text.lower()
        assert not (inbox / f"{msg_id}.json").exists()
        assert (processed / f"{msg_id}.json").exists()

        # No outbox files should have been created (no "Noted." or any fallback)
        outbox_after = set(outbox.iterdir())
        new_files = outbox_after - outbox_before
        assert new_files == set(), (
            f"Expected no outbox files to be written, but found: {new_files}"
        )

    def test_no_fallback_sent_for_reaction_without_reply(
        self, setup_dirs, message_generator, inbox_server_dirs
    ):
        """Reaction messages must not trigger any outbox write.

        Reactions were already excluded from the old fallback, but this test
        confirms the new code path (no fallback at all) also handles them correctly.
        """
        inbox, processed = setup_dirs
        outbox = inbox_server_dirs["outbox"]

        REAL_CHAT_ID = 8305714125
        msg = message_generator.generate_text_message(
            source="telegram", chat_id=REAL_CHAT_ID,
        )
        msg["type"] = "reaction"
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        outbox_before = set(outbox.iterdir())

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

        assert "processed" in result[0].text.lower()
        outbox_after = set(outbox.iterdir())
        assert outbox_after - outbox_before == set()

    def test_no_fallback_sent_for_callback_without_reply(
        self, setup_dirs, message_generator, inbox_server_dirs
    ):
        """Callback (button press) messages must not trigger any outbox write."""
        inbox, processed = setup_dirs
        outbox = inbox_server_dirs["outbox"]

        REAL_CHAT_ID = 8305714125
        msg = message_generator.generate_text_message(
            source="telegram", chat_id=REAL_CHAT_ID,
        )
        msg["type"] = "callback"
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        outbox_before = set(outbox.iterdir())

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

        assert "processed" in result[0].text.lower()
        outbox_after = set(outbox.iterdir())
        assert outbox_after - outbox_before == set()


class TestListSources:
    """Tests for list_sources tool."""

    def test_returns_sources_list(self):
        """Test that sources list is returned."""
        import asyncio
        from src.mcp.inbox_server import handle_list_sources

        result = asyncio.run(handle_list_sources({}))

        assert "Sources" in result[0].text
        assert "Telegram" in result[0].text

    def test_shows_enabled_status(self):
        """Test that enabled status is shown."""
        import asyncio
        from src.mcp.inbox_server import handle_list_sources

        result = asyncio.run(handle_list_sources({}))

        assert "Enabled" in result[0].text or "enabled" in result[0].text.lower()


class TestGetStats:
    """Tests for get_stats tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path, message_generator):
        """Set up directories with messages."""
        inbox = temp_messages_dir / "inbox"
        outbox = temp_messages_dir / "outbox"
        processed = temp_messages_dir / "processed"

        # Add some messages to each
        for i in range(3):
            msg = message_generator.generate_text_message()
            (inbox / f"inbox_{i}.json").write_text(json.dumps(msg))

        for i in range(2):
            reply = {"chat_id": 123, "text": "Reply"}
            (outbox / f"outbox_{i}.json").write_text(json.dumps(reply))

        for i in range(5):
            msg = message_generator.generate_text_message()
            (processed / f"processed_{i}.json").write_text(json.dumps(msg))

        return inbox, outbox, processed

    def test_returns_message_counts(self, setup_dirs):
        """Test that message counts are returned."""
        inbox, outbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_get_stats

            result = asyncio.run(handle_get_stats({}))

            assert "3" in result[0].text  # inbox count
            assert "2" in result[0].text  # outbox count
            assert "5" in result[0].text  # processed count

    def test_shows_source_breakdown(self, setup_dirs):
        """Test that source breakdown is shown."""
        inbox, outbox, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_get_stats

            result = asyncio.run(handle_get_stats({}))

            assert "Statistics" in result[0].text


class TestWriteResultDeduplication:
    """Tests for server-side dedup: write_result should not relay when send_reply
    was already called with the same text to the same chat."""

    @pytest.fixture(autouse=True)
    def reset_direct_sends(self, tmp_path):
        """Clear the sent-replies directory before each test."""
        sent_replies = tmp_path / "sent-replies"
        sent_replies.mkdir(exist_ok=True)
        self._sent_replies_dir = sent_replies
        yield
        # Cleanup
        for f in sent_replies.iterdir():
            f.unlink(missing_ok=True)

    @pytest.fixture
    def dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        outbox = temp_messages_dir / "outbox"
        sent = temp_messages_dir / "sent"
        sent.mkdir(exist_ok=True)
        return inbox, outbox, sent

    def test_sent_reply_promoted_after_send_reply(self, dirs):
        """write_result with sent_reply_to_user=False is promoted to True when an identical
        message was already delivered via send_reply to the same chat."""
        inbox, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=self._sent_replies_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply, handle_write_result

            text = "Done! PR #42 is open."

            # Subagent calls send_reply first
            asyncio.run(handle_send_reply({
                "chat_id": 111,
                "text": text,
                "source": "telegram",
            }))

            # Then calls write_result omitting sent_reply_to_user (buggy pattern — would relay duplicate)
            result = asyncio.run(handle_write_result({
                "task_id": "issue-42",
                "chat_id": 111,
                "text": text,
                "source": "telegram",
                # sent_reply_to_user omitted — server should detect dedup and set True
            }))

            assert len(result) == 1
            assert "Result queued" in result[0].text

            # Verify sent_reply_to_user was promoted to True by server-side dedup
            inbox_files = list(inbox.glob("*.json"))
            assert len(inbox_files) == 1
            msg = json.loads(inbox_files[0].read_text())
            assert msg["sent_reply_to_user"] is True, (
                "sent_reply_to_user should be promoted to True when send_reply was already called"
            )

    def test_relay_not_suppressed_for_different_text(self, dirs):
        """write_result is NOT suppressed when the text differs from what was sent via send_reply."""
        inbox, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=self._sent_replies_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply, handle_write_result

            # send_reply with one text
            asyncio.run(handle_send_reply({
                "chat_id": 222,
                "text": "Interim update: working on it.",
                "source": "telegram",
            }))

            # write_result with different text and sent_reply_to_user=False
            asyncio.run(handle_write_result({
                "task_id": "issue-99",
                "chat_id": 222,
                "text": "Final result: all done.",
                "source": "telegram",
                "sent_reply_to_user": False,
            }))

            inbox_files = list(inbox.glob("*.json"))
            assert len(inbox_files) == 1
            msg = json.loads(inbox_files[0].read_text())
            assert msg["sent_reply_to_user"] is False, (
                "sent_reply_to_user should remain False when texts differ"
            )

    def test_relay_not_suppressed_for_different_chat(self, dirs):
        """write_result is NOT suppressed when the chat_id differs."""
        inbox, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=self._sent_replies_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_send_reply, handle_write_result

            text = "Task complete."

            # send_reply to chat 333
            asyncio.run(handle_send_reply({
                "chat_id": 333,
                "text": text,
                "source": "telegram",
            }))

            # write_result to a different chat 444 with sent_reply_to_user=False
            asyncio.run(handle_write_result({
                "task_id": "issue-77",
                "chat_id": 444,
                "text": text,
                "source": "telegram",
                "sent_reply_to_user": False,
            }))

            inbox_files = list(inbox.glob("*.json"))
            assert len(inbox_files) == 1
            msg = json.loads(inbox_files[0].read_text())
            assert msg["sent_reply_to_user"] is False, (
                "sent_reply_to_user should remain False when chat_id differs"
            )

    def test_explicit_sent_reply_true(self, dirs):
        """write_result with explicit sent_reply_to_user=True produces subagent_notification."""
        inbox, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=self._sent_replies_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "issue-55",
                "chat_id": 555,
                "text": "Already delivered directly.",
                "source": "telegram",
                "sent_reply_to_user": True,
            }))

            inbox_files = list(inbox.glob("*.json"))
            assert len(inbox_files) == 1
            msg = json.loads(inbox_files[0].read_text())
            assert msg["sent_reply_to_user"] is True
            assert msg["type"] == "subagent_notification"

    def test_legacy_forward_false_compat(self, dirs):
        """Legacy callers using forward=False are treated as sent_reply_to_user=True (inverse semantics)."""
        inbox, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=self._sent_replies_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "issue-legacy",
                "chat_id": 666,
                "text": "Legacy forward=False call.",
                "source": "telegram",
                "forward": False,  # old API: forward=False meant "don't relay" → sent_reply_to_user=True
            }))

            inbox_files = list(inbox.glob("*.json"))
            assert len(inbox_files) == 1
            msg = json.loads(inbox_files[0].read_text())
            assert msg["sent_reply_to_user"] is True
            assert msg["type"] == "subagent_notification"

    def test_legacy_forward_true_compat(self, dirs):
        """Legacy callers using forward=True are treated as sent_reply_to_user=False (inverse semantics)."""
        inbox, outbox, sent = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=self._sent_replies_dir,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "issue-legacy-fwd",
                "chat_id": 777,
                "text": "Legacy forward=True call.",
                "source": "telegram",
                "forward": True,  # old API: forward=True meant "relay" → sent_reply_to_user=False
            }))

            inbox_files = list(inbox.glob("*.json"))
            assert len(inbox_files) == 1
            msg = json.loads(inbox_files[0].read_text())
            assert msg["sent_reply_to_user"] is False
            assert msg["type"] == "subagent_result"


# ---------------------------------------------------------------------------
# Tests for _enqueue_recovery_notification summary_marker coupling
# ---------------------------------------------------------------------------

class TestEnqueueRecoveryNotification:
    """Tests for the summary_marker boundary logic in _enqueue_recovery_notification.

    The summary_marker "Recovered content:\\n\\n" is the coupling point between
    _write_synthetic_inbox_message (in require-write-result.py) and
    _enqueue_recovery_notification (in inbox_server.py). These tests verify
    that the marker correctly identifies the boundary, that content after it
    is used as the notification summary, and that the no-content fallback
    works when the marker is absent.
    """

    def _call_enqueue(self, inbox_dir: Path, msg: dict, owner_chat_id: int = 99999) -> dict:
        """Call _enqueue_recovery_notification and return the written notification."""
        from src.mcp.inbox_server import _enqueue_recovery_notification

        with patch("src.mcp.inbox_server._get_owner_chat_id_and_source", return_value=(owner_chat_id, "telegram")), \
             patch.multiple("src.mcp.inbox_server", INBOX_DIR=inbox_dir):
            _enqueue_recovery_notification(msg)

        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1, "expected exactly one notification file"
        return json.loads(files[0].read_text())

    def test_summary_marker_extracts_salvaged_content(self, tmp_path):
        """Content after 'Recovered content:\\n\\n' appears in the notification summary."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        salvaged = "The agent was working on issue #42 and had drafted a solution."
        msg = {
            "task_id": "agent-42",
            "text": (
                "Agent exited without calling write_result. "
                "Content recovered from transcript after 5 hook fires."
                "\n\nRecovered content:\n\n"
                + salvaged
            ),
        }
        notification = self._call_enqueue(inbox_dir, msg)

        assert salvaged in notification["text"]
        assert "Last known activity:" in notification["text"]

    def test_summary_marker_boundary_excludes_preamble(self, tmp_path):
        """The preamble (recovery note before the marker) does not appear as the summary."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        preamble = "Agent exited without calling write_result."
        salvaged = "Actual salvaged content from the transcript."
        msg = {
            "task_id": "agent-boundary",
            "text": f"{preamble}\n\nRecovered content:\n\n{salvaged}",
        }
        notification = self._call_enqueue(inbox_dir, msg)

        # The summary line should contain the salvaged part, not the preamble verbatim.
        assert "Last known activity:" in notification["text"]
        assert salvaged in notification["text"]
        # Preamble text should not be duplicated inside the "Last known activity:" line.
        summary_portion = notification["text"].split("Last known activity:")[1]
        assert preamble not in summary_portion

    def test_summary_marker_truncates_long_content(self, tmp_path):
        """Salvaged content longer than 300 characters is truncated with an ellipsis."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        long_content = "x" * 400
        msg = {
            "task_id": "agent-long",
            "text": "Preamble.\n\nRecovered content:\n\n" + long_content,
        }
        notification = self._call_enqueue(inbox_dir, msg)

        assert "Last known activity:" in notification["text"]
        assert "…" in notification["text"]
        # The full 400-char content should not appear verbatim.
        assert long_content not in notification["text"]

    def test_no_summary_marker_uses_fallback(self, tmp_path):
        """When text does not contain the summary marker, fallback message is used."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        msg = {
            "task_id": "agent-no-content",
            "text": "Agent exited without calling write_result.\n\n(No recoverable transcript content found.)",
        }
        notification = self._call_enqueue(inbox_dir, msg)

        assert "No recoverable transcript content was found." in notification["text"]
        assert "Last known activity:" not in notification["text"]

    def test_empty_text_uses_fallback(self, tmp_path):
        """Empty text field (no transcript content at all) uses the fallback message."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        msg = {"task_id": "agent-empty", "text": ""}
        notification = self._call_enqueue(inbox_dir, msg)

        assert "No recoverable transcript content was found." in notification["text"]

    def test_notification_uses_owner_chat_id(self, tmp_path):
        """Recovery notification is addressed to the owner, not to chat_id=0."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        owner_id = 123456789
        msg = {"task_id": "agent-chat", "text": ""}
        notification = self._call_enqueue(inbox_dir, msg, owner_chat_id=owner_id)

        assert notification["chat_id"] == owner_id
        assert notification["type"] == "subagent_notification"

    def test_owner_chat_id_none_skips_notification(self, tmp_path):
        """When owner chat_id cannot be resolved, no notification file is written."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        from src.mcp.inbox_server import _enqueue_recovery_notification

        with patch("src.mcp.inbox_server._get_owner_chat_id_and_source", return_value=(None, "telegram")), \
             patch.multiple("src.mcp.inbox_server", INBOX_DIR=inbox_dir):
            _enqueue_recovery_notification({"task_id": "x", "text": ""})

        assert list(inbox_dir.glob("*.json")) == []
