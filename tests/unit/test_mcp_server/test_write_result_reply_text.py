"""
Unit tests for the reply_text field in write_result (issue #1490, fixed by #1543).

Design after fix:
  reply_text is EPHEMERAL — when present and sent_reply_to_user=False, write_result
  relays it immediately by writing an outbox file (same path as send_reply), then
  sets sent_reply_to_user=True so the dispatcher does not relay again.
  reply_text is NEVER stored in the inbox message; the dispatcher only sees `text`.

Test cases:
  1. reply_text present + sent_reply_to_user=False
     → outbox file written with reply_text content
     → inbox message does NOT contain reply_text
     → inbox message has sent_reply_to_user=True
     → inbox message type is subagent_notification
  2. reply_text absent → no outbox file; inbox message behaves normally (subagent_result)
  3. reply_text present + sent_reply_to_user=True
     → no outbox file written (user already has the reply)
     → inbox message does NOT contain reply_text
  4. reply_text empty string → treated as absent (no outbox file)
  5. reply_text whitespace-only → treated as absent (no outbox file)
  6. text is always stored in inbox message unchanged
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parents[4]
for _p in [str(_ROOT / "src" / "mcp"), str(_ROOT / "src" / "agents"),
           str(_ROOT / "src"), str(_ROOT / "src" / "utils")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import src.mcp.inbox_server  # noqa: F401 — pre-load so patch.multiple resolves it


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dirs(tmp_path: Path):
    """Return (inbox, outbox, sent, sent_replies) directories under tmp_path."""
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent"
    sent_replies = tmp_path / "sent-replies"
    for d in (inbox, outbox, sent, sent_replies):
        d.mkdir(parents=True, exist_ok=True)
    return inbox, outbox, sent, sent_replies


def _mock_session_store() -> MagicMock:
    store = MagicMock()
    store.session_end.return_value = None
    store.set_notified.return_value = None
    return store


def _read_inbox_message(inbox: Path) -> dict:
    files = list(inbox.glob("*.json"))
    assert len(files) == 1, f"Expected 1 inbox file, got {len(files)}"
    return json.loads(files[0].read_text())


def _read_outbox_messages(outbox: Path) -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted(outbox.glob("*.json"))]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestReplyTextRelayedImmediately:
    """reply_text is relayed via outbox immediately and never stored in the inbox message."""

    def test_reply_text_written_to_outbox_when_provided(self, tmp_path):
        """When reply_text is provided and not yet sent, an outbox file is written with its content."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-reply-text",
                "chat_id": 12345,
                "text": "Filed issue #42 in SiderealPress/lobster. Label: enhancement. URL: https://github.com/SiderealPress/lobster/issues/42",
                "reply_text": "Filed: https://github.com/SiderealPress/lobster/issues/42",
                "source": "telegram",
            }))

        outbox_msgs = _read_outbox_messages(outbox)
        assert len(outbox_msgs) == 1, "Exactly one outbox file must be written for reply_text relay"
        assert outbox_msgs[0]["text"] == "Filed: https://github.com/SiderealPress/lobster/issues/42"
        assert outbox_msgs[0]["chat_id"] == 12345

    def test_reply_text_not_in_inbox_message(self, tmp_path):
        """reply_text must NOT appear in the inbox message — dispatcher only sees text."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-reply-text-leak",
                "chat_id": 12345,
                "text": "Full dispatcher context: filed issue #42, applied label enhancement, set milestone v2.0",
                "reply_text": "Filed #42",
                "source": "telegram",
            }))

        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg, (
            "reply_text MUST NOT appear in the inbox message — "
            "it is ephemeral, relayed immediately, then discarded"
        )

    def test_sent_reply_to_user_true_after_reply_text_relay(self, tmp_path):
        """After relaying reply_text, the inbox message has sent_reply_to_user=True."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-sent-flag",
                "chat_id": 12345,
                "text": "Dispatcher-only context.",
                "reply_text": "Reply for user.",
                "source": "telegram",
                "sent_reply_to_user": False,
            }))

        msg = _read_inbox_message(inbox)
        assert msg["sent_reply_to_user"] is True, (
            "sent_reply_to_user must be True after reply_text relay — "
            "dispatcher must not relay again"
        )

    def test_message_type_is_subagent_notification_after_reply_text_relay(self, tmp_path):
        """After relay, message type is subagent_notification (user already has the reply)."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-type-after-relay",
                "chat_id": 12345,
                "text": "Full context for dispatcher.",
                "reply_text": "Short reply for user.",
                "source": "telegram",
                "sent_reply_to_user": False,
            }))

        msg = _read_inbox_message(inbox)
        assert msg["type"] == "subagent_notification", (
            f"Message type must be subagent_notification after reply_text relay, got: {msg['type']}"
        )


class TestReplyTextAbsent:
    """When reply_text is absent, normal backward-compatible behavior applies."""

    def test_no_outbox_file_when_reply_text_absent(self, tmp_path):
        """When reply_text is not provided, no outbox file is written by write_result."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-no-reply-text",
                "chat_id": 12345,
                "text": "Done. PR is open at https://github.com/SiderealPress/lobster/pull/99",
                "source": "telegram",
            }))

        outbox_msgs = _read_outbox_messages(outbox)
        assert len(outbox_msgs) == 0, "No outbox file should be written when reply_text is absent"

    def test_reply_text_not_in_inbox_when_absent(self, tmp_path):
        """When reply_text is not provided, it does not appear in the inbox message."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-no-reply-text-key",
                "chat_id": 12345,
                "text": "Done.",
                "source": "telegram",
            }))

        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg

    def test_message_type_is_subagent_result_when_no_reply_text(self, tmp_path):
        """Without reply_text, type is subagent_result (dispatcher relays text normally)."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-type-no-reply-text",
                "chat_id": 12345,
                "text": "Done.",
                "source": "telegram",
                "sent_reply_to_user": False,
            }))

        msg = _read_inbox_message(inbox)
        assert msg["type"] == "subagent_result"


class TestReplyTextWithSentReplyToUserTrue:
    """When sent_reply_to_user=True, reply_text is ignored (user already has the reply)."""

    def test_no_outbox_file_when_already_sent(self, tmp_path):
        """When sent_reply_to_user=True, reply_text is not relayed (no outbox write)."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-already-sent",
                "chat_id": 12345,
                "text": "Summary for dispatcher: completed filing of issue #42.",
                "reply_text": "Filed #42",
                "source": "telegram",
                "sent_reply_to_user": True,
            }))

        outbox_msgs = _read_outbox_messages(outbox)
        assert len(outbox_msgs) == 0, (
            "No outbox file must be written when sent_reply_to_user=True — "
            "the subagent already delivered the reply"
        )

    def test_reply_text_not_in_inbox_when_already_sent(self, tmp_path):
        """When sent_reply_to_user=True, reply_text is NOT stored in the inbox message."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-already-sent-no-leak",
                "chat_id": 12345,
                "text": "Summary for dispatcher.",
                "reply_text": "Filed #42",
                "source": "telegram",
                "sent_reply_to_user": True,
            }))

        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg


class TestReplyTextEdgeCases:
    """Edge cases for reply_text handling."""

    def test_reply_text_empty_string_treated_as_absent(self, tmp_path):
        """An empty reply_text is treated as absent — no outbox file written."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-empty-reply-text",
                "chat_id": 12345,
                "text": "Done.",
                "reply_text": "",
                "source": "telegram",
            }))

        outbox_msgs = _read_outbox_messages(outbox)
        assert len(outbox_msgs) == 0, "Empty reply_text must be treated as absent"
        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg

    def test_reply_text_whitespace_only_treated_as_absent(self, tmp_path):
        """A whitespace-only reply_text is treated as absent — no outbox file written."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-whitespace-reply-text",
                "chat_id": 12345,
                "text": "Done.",
                "reply_text": "   ",
                "source": "telegram",
            }))

        outbox_msgs = _read_outbox_messages(outbox)
        assert len(outbox_msgs) == 0, "Whitespace-only reply_text must be treated as absent"
        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg

    def test_text_always_stored_in_inbox_unchanged(self, tmp_path):
        """text is always stored in the inbox message unchanged, regardless of reply_text."""
        inbox, outbox, sent, sent_replies = _make_dirs(tmp_path)
        mock_store = _mock_session_store()
        expected_text = "Full dispatcher context: filed issue #42, applied label enhancement, set milestone v2.0"

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            SENT_REPLIES_DIR=sent_replies,
            _session_store=mock_store,
        ):
            from src.mcp.inbox_server import handle_write_result

            asyncio.run(handle_write_result({
                "task_id": "test-text-preserved",
                "chat_id": 12345,
                "text": expected_text,
                "reply_text": "Filed #42",
                "source": "telegram",
            }))

        msg = _read_inbox_message(inbox)
        assert msg["text"] == expected_text, "text field must be stored unchanged in inbox message"
