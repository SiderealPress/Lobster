"""
Tests for write_result reply_text field (Closes #1621 clean rewrite).

reply_text lets subagents decouple the dispatcher-internal summary from what
the user actually sees. When reply_text is provided and the subagent has NOT
already sent a reply (sent_reply_to_user=False) and chat_id != 0, the
dispatcher receives reply_text in the inbox message so it can send that to the
user instead of text.

text always remains as the internal dispatcher summary.

Behaviors tested:
- reply_text is stored in the inbox message when provided and non-empty
- reply_text is absent from the inbox message when not provided
- empty / whitespace-only reply_text is treated as absent
- text is always stored regardless of reply_text
- reply_text is NOT stored when sent_reply_to_user=True (user already got a reply)
- reply_text is NOT stored when chat_id == 0 (dispatcher-internal tasks)
- backward compatibility: callers that omit reply_text see identical behavior
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup: ensure src/mcp and src/agents are importable
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
    """Return (inbox, outbox, sent, sent_replies, task_replied) under tmp_path."""
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent"
    sent_replies = tmp_path / "sent-replies"
    task_replied = tmp_path / "task-replied"
    for d in (inbox, outbox, sent, sent_replies, task_replied):
        d.mkdir(parents=True, exist_ok=True)
    return inbox, outbox, sent, sent_replies, task_replied


def _mock_session_store() -> MagicMock:
    store = MagicMock()
    store.session_end.return_value = None
    store.set_notified.return_value = None
    return store


def _run_write_result(tmp_path: Path, args: dict) -> dict:
    """Run handle_write_result and return the inbox message written."""
    inbox, outbox, sent, sent_replies, task_replied = _make_dirs(tmp_path)
    mock_store = _mock_session_store()

    with patch.multiple(
        "src.mcp.inbox_server",
        INBOX_DIR=inbox,
        OUTBOX_DIR=outbox,
        SENT_DIR=sent,
        SENT_REPLIES_DIR=sent_replies,
        TASK_REPLIED_DIR=task_replied,
        _session_store=mock_store,
        _db_persist_agent_event=None,
    ):
        from src.mcp.inbox_server import handle_write_result
        asyncio.run(handle_write_result(args))

    inbox_files = list(inbox.glob("*.json"))
    assert len(inbox_files) == 1, f"Expected 1 inbox file, got {len(inbox_files)}"
    return json.loads(inbox_files[0].read_text())


# ---------------------------------------------------------------------------
# Tests: reply_text stored when provided
# ---------------------------------------------------------------------------

class TestReplyTextStoredInInboxMessage:
    """reply_text is stored in the inbox message when provided and non-empty."""

    def test_reply_text_stored_in_message(self, tmp_path):
        """reply_text field appears in inbox message when provided."""
        msg = _run_write_result(tmp_path, {
            "task_id": "my-task",
            "chat_id": 12345,
            "text": "Terse dispatcher summary.",
            "reply_text": "PR #42 is open and ready for review.",
        })
        assert msg.get("reply_text") == "PR #42 is open and ready for review."

    def test_text_always_stored_when_reply_text_provided(self, tmp_path):
        """text field is always present even when reply_text is also provided."""
        msg = _run_write_result(tmp_path, {
            "task_id": "my-task",
            "chat_id": 12345,
            "text": "Internal dispatcher summary.",
            "reply_text": "Short user-facing reply.",
        })
        assert msg.get("text") == "Internal dispatcher summary."

    def test_reply_text_preserved_verbatim(self, tmp_path):
        """reply_text is stored exactly as provided — no normalization."""
        user_reply = "Done! Here is the summary:\n- item one\n- item two"
        msg = _run_write_result(tmp_path, {
            "task_id": "task-abc",
            "chat_id": 99,
            "text": "Summary.",
            "reply_text": user_reply,
        })
        assert msg["reply_text"] == user_reply


# ---------------------------------------------------------------------------
# Tests: reply_text absent when not provided or falsy
# ---------------------------------------------------------------------------

class TestReplyTextAbsentWhenNotProvided:
    """reply_text must not appear in inbox message when absent or falsy."""

    def test_reply_text_absent_when_not_provided(self, tmp_path):
        """No reply_text key in inbox message when caller omits it."""
        msg = _run_write_result(tmp_path, {
            "task_id": "no-reply-text-task",
            "chat_id": 12345,
            "text": "Direct user-facing text.",
        })
        assert "reply_text" not in msg

    def test_reply_text_absent_when_empty_string(self, tmp_path):
        """Empty string reply_text is treated as absent — not stored in message."""
        msg = _run_write_result(tmp_path, {
            "task_id": "empty-reply-text-task",
            "chat_id": 12345,
            "text": "Summary.",
            "reply_text": "",
        })
        assert "reply_text" not in msg

    def test_reply_text_absent_when_whitespace_only(self, tmp_path):
        """Whitespace-only reply_text is treated as absent — not stored."""
        msg = _run_write_result(tmp_path, {
            "task_id": "ws-reply-text-task",
            "chat_id": 12345,
            "text": "Summary.",
            "reply_text": "   ",
        })
        assert "reply_text" not in msg


# ---------------------------------------------------------------------------
# Tests: reply_text suppressed when irrelevant
# ---------------------------------------------------------------------------

class TestReplyTextSuppressedWhenIrrelevant:
    """reply_text must not be stored when the user already has a reply or
    when the message is dispatcher-internal (chat_id == 0)."""

    def test_reply_text_not_stored_when_sent_reply_to_user_true(self, tmp_path):
        """When sent_reply_to_user=True, user already got a reply.
        reply_text in the inbox message would be confusing — omit it."""
        msg = _run_write_result(tmp_path, {
            "task_id": "already-replied-task",
            "chat_id": 12345,
            "text": "Dispatcher summary.",
            "reply_text": "This was already sent directly.",
            "sent_reply_to_user": True,
        })
        assert "reply_text" not in msg

    def test_reply_text_not_stored_when_chat_id_is_zero(self, tmp_path):
        """chat_id == 0 is a dispatcher-internal task.
        No user relay happens, so reply_text is meaningless — omit it."""
        msg = _run_write_result(tmp_path, {
            "task_id": "dispatcher-internal-task",
            "chat_id": 0,
            "text": "Internal summary.",
            "reply_text": "This would never be sent to anyone.",
        })
        assert "reply_text" not in msg

    def test_reply_text_not_stored_when_chat_id_is_string_zero(self, tmp_path):
        """chat_id == '0' (string sentinel) is also dispatcher-internal.
        The guard must handle both int 0 and string '0' to prevent accidental relay."""
        msg = _run_write_result(tmp_path, {
            "task_id": "dispatcher-internal-string-task",
            "chat_id": "0",
            "text": "Internal summary.",
            "reply_text": "This would never be sent to anyone.",
        })
        assert "reply_text" not in msg


# ---------------------------------------------------------------------------
# Tests: backward compatibility — existing callers unaffected
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Callers that do not pass reply_text see unchanged behavior."""

    def test_existing_fields_unchanged_without_reply_text(self, tmp_path):
        """Core fields (text, task_id, chat_id, status, source) are unchanged."""
        msg = _run_write_result(tmp_path, {
            "task_id": "compat-task",
            "chat_id": 42,
            "text": "My result.",
            "source": "telegram",
            "status": "success",
        })
        assert msg["task_id"] == "compat-task"
        assert msg["chat_id"] == 42
        assert msg["text"] == "My result."
        assert msg["source"] == "telegram"
        assert msg["status"] == "success"
        assert "reply_text" not in msg

    def test_sent_reply_to_user_false_by_default(self, tmp_path):
        """sent_reply_to_user defaults to False when not provided."""
        msg = _run_write_result(tmp_path, {
            "task_id": "default-sent",
            "chat_id": 1,
            "text": "Text.",
        })
        assert msg["sent_reply_to_user"] is False
