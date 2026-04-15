"""
Unit tests for the reply_text field in write_result (issue #1490).

When a subagent provides reply_text, the dispatcher should relay reply_text to
the user instead of text, keeping text as dispatcher-only internal context.
When reply_text is absent, text is used (backward-compat).

Test cases:
  1. reply_text present → stored in inbox message under key "reply_text"
  2. reply_text absent → "reply_text" key not in inbox message (backward-compat)
  3. reply_text present + sent_reply_to_user=True → reply_text NOT stored
     (subagent already delivered; no relay needed)
  4. reply_text empty string → treated as absent (not stored)
  5. reply_text present → text still stored as-is (dispatcher summary unchanged)
"""

from __future__ import annotations

import asyncio
import json
import sys
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


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestReplyTextField:
    """reply_text is stored in the inbox message when provided."""

    def test_reply_text_stored_when_provided(self, tmp_path):
        """When reply_text is provided, it appears in the inbox message."""
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

        msg = _read_inbox_message(inbox)
        assert "reply_text" in msg, "reply_text must appear in inbox message when provided"
        assert msg["reply_text"] == "Filed: https://github.com/SiderealPress/lobster/issues/42"

    def test_reply_text_absent_when_not_provided(self, tmp_path):
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
                "task_id": "test-no-reply-text",
                "chat_id": 12345,
                "text": "Done. PR is open at https://github.com/SiderealPress/lobster/pull/99",
                "source": "telegram",
            }))

        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg, "reply_text must NOT appear when not provided (backward-compat)"

    def test_text_always_stored_regardless_of_reply_text(self, tmp_path):
        """text is always stored in the inbox message even when reply_text is present."""
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
                "task_id": "test-text-preserved",
                "chat_id": 12345,
                "text": "Full dispatcher context: filed issue #42, applied label enhancement, set milestone v2.0",
                "reply_text": "Filed #42",
                "source": "telegram",
            }))

        msg = _read_inbox_message(inbox)
        assert msg["text"] == "Full dispatcher context: filed issue #42, applied label enhancement, set milestone v2.0"
        assert msg["reply_text"] == "Filed #42"

    def test_reply_text_not_stored_when_sent_reply_to_user_true(self, tmp_path):
        """When sent_reply_to_user=True, reply_text is NOT stored (subagent already delivered)."""
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

        msg = _read_inbox_message(inbox)
        # When sent_reply_to_user=True, reply_text should NOT be stored
        # (the relay path is suppressed; reply_text would never be used)
        assert "reply_text" not in msg, (
            "reply_text must NOT be stored when sent_reply_to_user=True — "
            "the subagent already sent the reply directly"
        )

    def test_reply_text_empty_string_treated_as_absent(self, tmp_path):
        """An empty reply_text is treated as absent — not stored in inbox message."""
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

        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg, "Empty reply_text must be treated as absent"

    def test_reply_text_whitespace_only_treated_as_absent(self, tmp_path):
        """A whitespace-only reply_text is treated as absent — not stored."""
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

        msg = _read_inbox_message(inbox)
        assert "reply_text" not in msg, "Whitespace-only reply_text must be treated as absent"

    def test_message_type_is_subagent_result_not_notification(self, tmp_path):
        """reply_text with sent_reply_to_user=False still creates subagent_result type."""
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
                "task_id": "test-type-check",
                "chat_id": 12345,
                "text": "Full context for dispatcher.",
                "reply_text": "Short reply for user.",
                "source": "telegram",
                "sent_reply_to_user": False,
            }))

        msg = _read_inbox_message(inbox)
        assert msg["type"] == "subagent_result", (
            f"Message type must be subagent_result when sent_reply_to_user=False, got: {msg['type']}"
        )
        assert "reply_text" in msg
