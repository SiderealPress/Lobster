"""
Tests for write_result user_summary and dispatcher_detail fields (issue #1975).

The write_result tool gains two new optional fields:

- user_summary  — User-facing reply the dispatcher relays WITHOUT loading content
                  into its own context. Stored in the inbox message so the dispatcher
                  can forward it. When present, the dispatcher relays user_summary
                  instead of text or reply_text.
- dispatcher_detail — Extended context for the dispatcher, available on demand but
                       NOT auto-loaded. Stored separately so the dispatcher can
                       fetch it explicitly when it needs depth, without paying the
                       token cost on every result.

Behavioral contract (from issue #1975):
1. Dispatcher reads result/text — always, auto-loaded (existing behavior unchanged).
2. Dispatcher relays user_summary to the user without reading it — zero token cost.
3. Dispatcher ignores dispatcher_detail unless it needs to dig in — on-demand only.

The relay priority order is: user_summary > reply_text > text (backward-compat).

Behaviors tested:
- user_summary is stored in the inbox message when provided and non-empty
- user_summary is absent from the inbox message when not provided or falsy
- user_summary is NOT stored when sent_reply_to_user=True (user already has reply)
- user_summary is NOT stored when chat_id == 0 (dispatcher-internal tasks)
- dispatcher_detail is stored in the inbox message when provided and non-empty
- dispatcher_detail is absent from the inbox message when not provided or falsy
- dispatcher_detail is stored even when sent_reply_to_user=True (still useful context)
- dispatcher_detail is stored even when chat_id == 0 (dispatcher can still use it)
- text field is always stored regardless of user_summary or dispatcher_detail
- backward compatibility: callers that omit both new fields see identical behavior
- user_summary takes relay priority over reply_text when both are present
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

USER_SUMMARY_NOT_STORED_SENTINEL = "user_summary MUST NOT be in inbox message"
DISPATCHER_DETAIL_NOT_STORED_SENTINEL = "dispatcher_detail MUST NOT be in inbox message"


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
# Tests: user_summary stored when provided
# ---------------------------------------------------------------------------

class TestUserSummaryStoredInInboxMessage:
    """user_summary is stored in the inbox message when provided and non-empty."""

    def test_user_summary_stored_in_message(self, tmp_path):
        """user_summary field appears in inbox message when provided."""
        msg = _run_write_result(tmp_path, {
            "task_id": "my-task",
            "chat_id": 12345,
            "text": "Terse dispatcher summary.",
            "user_summary": "PR #42 is open and ready for review.",
        })
        assert msg.get("user_summary") == "PR #42 is open and ready for review."

    def test_text_always_stored_when_user_summary_provided(self, tmp_path):
        """text field is always present even when user_summary is also provided."""
        msg = _run_write_result(tmp_path, {
            "task_id": "my-task",
            "chat_id": 12345,
            "text": "Internal dispatcher summary.",
            "user_summary": "Short user-facing reply.",
        })
        assert msg.get("text") == "Internal dispatcher summary."

    def test_user_summary_preserved_verbatim(self, tmp_path):
        """user_summary is stored exactly as provided — no normalization."""
        user_msg = "Done! Here is the summary:\n- item one\n- item two"
        msg = _run_write_result(tmp_path, {
            "task_id": "task-abc",
            "chat_id": 99,
            "text": "Summary.",
            "user_summary": user_msg,
        })
        assert msg["user_summary"] == user_msg


# ---------------------------------------------------------------------------
# Tests: user_summary absent when not provided or falsy
# ---------------------------------------------------------------------------

class TestUserSummaryAbsentWhenNotProvided:
    """user_summary must not appear in inbox message when absent or falsy."""

    def test_user_summary_absent_when_not_provided(self, tmp_path):
        """No user_summary key in inbox message when caller omits it."""
        msg = _run_write_result(tmp_path, {
            "task_id": "no-user-summary-task",
            "chat_id": 12345,
            "text": "Direct user-facing text.",
        })
        assert "user_summary" not in msg

    def test_user_summary_absent_when_empty_string(self, tmp_path):
        """Empty string user_summary is treated as absent — not stored in message."""
        msg = _run_write_result(tmp_path, {
            "task_id": "empty-user-summary-task",
            "chat_id": 12345,
            "text": "Summary.",
            "user_summary": "",
        })
        assert "user_summary" not in msg

    def test_user_summary_absent_when_whitespace_only(self, tmp_path):
        """Whitespace-only user_summary is treated as absent — not stored."""
        msg = _run_write_result(tmp_path, {
            "task_id": "ws-user-summary-task",
            "chat_id": 12345,
            "text": "Summary.",
            "user_summary": "   ",
        })
        assert "user_summary" not in msg


# ---------------------------------------------------------------------------
# Tests: user_summary suppressed when irrelevant
# ---------------------------------------------------------------------------

class TestUserSummarySuppressedWhenIrrelevant:
    """user_summary must not be stored when the user already has a reply or
    when the message is dispatcher-internal (chat_id == 0)."""

    def test_user_summary_not_stored_when_sent_reply_to_user_true(self, tmp_path):
        """When sent_reply_to_user=True, user already got a reply.
        user_summary in the inbox message would be confusing — omit it."""
        msg = _run_write_result(tmp_path, {
            "task_id": "already-replied-task",
            "chat_id": 12345,
            "text": "Dispatcher summary.",
            "user_summary": "This was already sent directly.",
            "sent_reply_to_user": True,
        })
        assert "user_summary" not in msg

    def test_user_summary_not_stored_when_chat_id_is_zero(self, tmp_path):
        """chat_id == 0 is a dispatcher-internal task.
        No user relay happens, so user_summary is meaningless — omit it."""
        msg = _run_write_result(tmp_path, {
            "task_id": "dispatcher-internal-task",
            "chat_id": 0,
            "text": "Internal summary.",
            "user_summary": "This would never be sent to anyone.",
        })
        assert "user_summary" not in msg

    def test_user_summary_not_stored_when_chat_id_is_string_zero(self, tmp_path):
        """chat_id == '0' (string sentinel) is also dispatcher-internal.
        The guard must handle both int 0 and string '0'."""
        msg = _run_write_result(tmp_path, {
            "task_id": "dispatcher-internal-string-task",
            "chat_id": "0",
            "text": "Internal summary.",
            "user_summary": "This would never be sent to anyone.",
        })
        assert "user_summary" not in msg


# ---------------------------------------------------------------------------
# Tests: dispatcher_detail stored when provided
# ---------------------------------------------------------------------------

class TestDispatcherDetailStoredInInboxMessage:
    """dispatcher_detail is stored in the inbox message when provided and non-empty."""

    def test_dispatcher_detail_stored_in_message(self, tmp_path):
        """dispatcher_detail field appears in inbox message when provided."""
        detail = "Full diff: 42 files changed, +800/-300 lines. Key changes: ..."
        msg = _run_write_result(tmp_path, {
            "task_id": "detail-task",
            "chat_id": 12345,
            "text": "Brief summary.",
            "dispatcher_detail": detail,
        })
        assert msg.get("dispatcher_detail") == detail

    def test_dispatcher_detail_preserved_verbatim(self, tmp_path):
        """dispatcher_detail is stored exactly as provided."""
        detail = "Very long analysis:\n" + "x" * 5000
        msg = _run_write_result(tmp_path, {
            "task_id": "long-detail-task",
            "chat_id": 12345,
            "text": "Summary.",
            "dispatcher_detail": detail,
        })
        assert msg["dispatcher_detail"] == detail

    def test_dispatcher_detail_stored_when_sent_reply_to_user_true(self, tmp_path):
        """dispatcher_detail IS stored even when sent_reply_to_user=True.
        The dispatcher can still use it for context even if the user got a direct reply."""
        msg = _run_write_result(tmp_path, {
            "task_id": "notified-detail-task",
            "chat_id": 12345,
            "text": "Summary.",
            "dispatcher_detail": "Deep context for dispatcher.",
            "sent_reply_to_user": True,
        })
        assert msg.get("dispatcher_detail") == "Deep context for dispatcher."

    def test_dispatcher_detail_stored_for_dispatcher_internal_tasks(self, tmp_path):
        """dispatcher_detail IS stored even when chat_id == 0 (internal task).
        The dispatcher can use it even in internal/system tasks."""
        msg = _run_write_result(tmp_path, {
            "task_id": "internal-detail-task",
            "chat_id": 0,
            "text": "Internal summary.",
            "dispatcher_detail": "Extended detail for dispatcher-only use.",
        })
        assert msg.get("dispatcher_detail") == "Extended detail for dispatcher-only use."


# ---------------------------------------------------------------------------
# Tests: dispatcher_detail absent when not provided or falsy
# ---------------------------------------------------------------------------

class TestDispatcherDetailAbsentWhenNotProvided:
    """dispatcher_detail must not appear in inbox message when absent or falsy."""

    def test_dispatcher_detail_absent_when_not_provided(self, tmp_path):
        """No dispatcher_detail key in inbox message when caller omits it."""
        msg = _run_write_result(tmp_path, {
            "task_id": "no-detail-task",
            "chat_id": 12345,
            "text": "Summary.",
        })
        assert "dispatcher_detail" not in msg

    def test_dispatcher_detail_absent_when_empty_string(self, tmp_path):
        """Empty string dispatcher_detail is treated as absent — not stored."""
        msg = _run_write_result(tmp_path, {
            "task_id": "empty-detail-task",
            "chat_id": 12345,
            "text": "Summary.",
            "dispatcher_detail": "",
        })
        assert "dispatcher_detail" not in msg

    def test_dispatcher_detail_absent_when_whitespace_only(self, tmp_path):
        """Whitespace-only dispatcher_detail is treated as absent — not stored."""
        msg = _run_write_result(tmp_path, {
            "task_id": "ws-detail-task",
            "chat_id": 12345,
            "text": "Summary.",
            "dispatcher_detail": "   ",
        })
        assert "dispatcher_detail" not in msg


# ---------------------------------------------------------------------------
# Tests: relay priority ordering
# ---------------------------------------------------------------------------

class TestRelayPriority:
    """user_summary takes relay priority over reply_text when both are present.

    Priority order: user_summary > reply_text > text

    The dispatcher uses the first present field as the user-facing relay.
    This allows subagents to provide both a legacy reply_text and a new user_summary
    without breaking existing callers.
    """

    def test_user_summary_present_without_reply_text(self, tmp_path):
        """When only user_summary is present, it appears in the message."""
        msg = _run_write_result(tmp_path, {
            "task_id": "priority-task",
            "chat_id": 12345,
            "text": "Internal summary.",
            "user_summary": "User-facing: user_summary wins.",
        })
        assert msg.get("user_summary") == "User-facing: user_summary wins."
        assert "reply_text" not in msg

    def test_both_user_summary_and_reply_text_present(self, tmp_path):
        """When both user_summary and reply_text are present, both are stored.
        The dispatcher uses user_summary (higher priority) for the relay."""
        msg = _run_write_result(tmp_path, {
            "task_id": "both-fields-task",
            "chat_id": 12345,
            "text": "Internal summary.",
            "user_summary": "User-facing via user_summary.",
            "reply_text": "User-facing via reply_text (lower priority).",
        })
        assert msg.get("user_summary") == "User-facing via user_summary."
        assert msg.get("reply_text") == "User-facing via reply_text (lower priority)."

    def test_only_reply_text_present(self, tmp_path):
        """When only reply_text is present (no user_summary), behavior is unchanged."""
        msg = _run_write_result(tmp_path, {
            "task_id": "reply-text-only-task",
            "chat_id": 12345,
            "text": "Internal summary.",
            "reply_text": "User-facing via reply_text.",
        })
        assert "user_summary" not in msg
        assert msg.get("reply_text") == "User-facing via reply_text."


# ---------------------------------------------------------------------------
# Tests: all three fields together
# ---------------------------------------------------------------------------

class TestAllThreeFieldsTogether:
    """Verify behavior when text, user_summary, and dispatcher_detail are all provided."""

    def test_all_three_fields_stored(self, tmp_path):
        """All three fields appear in the inbox message when provided."""
        msg = _run_write_result(tmp_path, {
            "task_id": "full-task",
            "chat_id": 12345,
            "text": "Brief dispatcher summary.",
            "user_summary": "Short user-facing message.",
            "dispatcher_detail": "Extended analysis for dispatcher: ..." + "x" * 200,
        })
        assert msg["text"] == "Brief dispatcher summary."
        assert msg["user_summary"] == "Short user-facing message."
        assert "Extended analysis" in msg["dispatcher_detail"]

    def test_user_summary_suppressed_dispatcher_detail_kept_when_sent_reply_true(self, tmp_path):
        """When sent_reply_to_user=True: user_summary is dropped, dispatcher_detail is kept.

        user_summary is user-relay-only — irrelevant when user already has a reply.
        dispatcher_detail is dispatcher-only context — always useful regardless of relay status.
        """
        msg = _run_write_result(tmp_path, {
            "task_id": "mixed-flags-task",
            "chat_id": 12345,
            "text": "Brief summary.",
            "user_summary": "Would be relayed if user had no reply.",
            "dispatcher_detail": "Dispatcher context, always stored.",
            "sent_reply_to_user": True,
        })
        assert "user_summary" not in msg
        assert msg.get("dispatcher_detail") == "Dispatcher context, always stored."


# ---------------------------------------------------------------------------
# Tests: backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Callers that do not pass user_summary or dispatcher_detail see unchanged behavior."""

    def test_existing_fields_unchanged_without_new_fields(self, tmp_path):
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
        assert "user_summary" not in msg
        assert "dispatcher_detail" not in msg

    def test_reply_text_behavior_unchanged_without_new_fields(self, tmp_path):
        """Existing reply_text behavior is unaffected when new fields are absent."""
        msg = _run_write_result(tmp_path, {
            "task_id": "compat-reply-task",
            "chat_id": 12345,
            "text": "Internal summary.",
            "reply_text": "User-facing reply.",
        })
        assert msg.get("reply_text") == "User-facing reply."
        assert "user_summary" not in msg
        assert "dispatcher_detail" not in msg
