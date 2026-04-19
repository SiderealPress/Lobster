"""
Tests for write_result reply_text field (issue #1490).

The reply_text field allows subagents to split the dispatcher summary (text)
from the user-facing relay content (a file path). When reply_text is provided,
the inbox message carries it so the dispatcher can read the file and relay
its contents instead of text.

Behaviors tested:
- reply_text path is stored in the inbox message when provided
- reply_text is absent from the inbox message when not provided
- text is always stored in the inbox message regardless of reply_text
- empty/whitespace reply_text is treated as absent
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
    """Return (inbox, outbox, sent, sent_replies) directories under tmp_path."""
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
# Tests: reply_text field is stored when provided
# ---------------------------------------------------------------------------

class TestReplyTextStoredInInboxMessage:
    """reply_text file path is stored in the inbox message when provided."""

    def test_reply_text_path_stored_in_message(self, tmp_path):
        """reply_text field appears in inbox message when provided."""
        reply_path = "/tmp/lobster-workspace/reports/my-task-reply.md"
        msg = _run_write_result(tmp_path, {
            "task_id": "my-task",
            "chat_id": 12345,
            "text": "Terse dispatcher summary.",
            "reply_text": reply_path,
        })
        assert msg.get("reply_text") == reply_path

    def test_text_always_stored_regardless_of_reply_text(self, tmp_path):
        """text field is always present even when reply_text is provided."""
        msg = _run_write_result(tmp_path, {
            "task_id": "my-task",
            "chat_id": 12345,
            "text": "Internal summary for dispatcher.",
            "reply_text": "/tmp/some-reply.md",
        })
        assert msg.get("text") == "Internal summary for dispatcher."

    def test_reply_text_path_is_preserved_verbatim(self, tmp_path):
        """reply_text path is stored exactly as provided — no normalization."""
        path = "~/lobster-workspace/reports/task-abc-reply.md"
        msg = _run_write_result(tmp_path, {
            "task_id": "task-abc",
            "chat_id": 99,
            "text": "Summary.",
            "reply_text": path,
        })
        assert msg["reply_text"] == path


# ---------------------------------------------------------------------------
# Tests: reply_text absent when not provided
# ---------------------------------------------------------------------------

class TestReplyTextAbsentWhenNotProvided:
    """reply_text must not appear in inbox message when not passed by the subagent."""

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
        """Whitespace-only reply_text is treated as absent — not stored in message."""
        msg = _run_write_result(tmp_path, {
            "task_id": "ws-reply-text-task",
            "chat_id": 12345,
            "text": "Summary.",
            "reply_text": "   ",
        })
        assert "reply_text" not in msg


# ---------------------------------------------------------------------------
# Tests: reply_text suppressed for system tasks (chat_id 0 or "0")
# ---------------------------------------------------------------------------

class TestReplyTextSuppressedForSystemTasks:
    """reply_text must NOT be stored when chat_id is the system sentinel (0 or '0').

    System tasks route results back to the dispatcher, not to a real user.
    Storing a reply_text file path in that context would be misleading — there
    is no user to relay to.
    """

    def test_reply_text_suppressed_when_chat_id_integer_zero(self, tmp_path):
        """reply_text is not stored when chat_id is integer 0 (system task)."""
        msg = _run_write_result(tmp_path, {
            "task_id": "system-task",
            "chat_id": 0,
            "text": "Internal dispatcher briefing.",
            "reply_text": "/tmp/some-reply.md",
        })
        assert "reply_text" not in msg

    def test_reply_text_suppressed_when_chat_id_string_zero(self, tmp_path):
        """reply_text is not stored when chat_id is string '0' (system task via string)."""
        msg = _run_write_result(tmp_path, {
            "task_id": "system-task-str",
            "chat_id": "0",
            "text": "Internal dispatcher briefing.",
            "reply_text": "/tmp/some-reply.md",
        })
        assert "reply_text" not in msg

    def test_reply_text_stored_for_nonzero_chat_id(self, tmp_path):
        """reply_text IS stored for real user chat_ids (non-zero)."""
        reply_path = "/tmp/lobster-workspace/reports/my-task-reply.md"
        msg = _run_write_result(tmp_path, {
            "task_id": "user-task",
            "chat_id": 12345,
            "text": "Terse summary.",
            "reply_text": reply_path,
        })
        assert msg.get("reply_text") == reply_path


# ---------------------------------------------------------------------------
# Tests: backward compatibility — existing callers unaffected
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Callers that do not pass reply_text see unchanged behavior."""

    def test_existing_fields_unchanged(self, tmp_path):
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
