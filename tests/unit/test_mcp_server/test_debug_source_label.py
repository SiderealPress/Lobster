"""
Tests for debug source label injection in send_reply (#1789).

When LOBSTER_DEBUG=true, send_reply prepends a label to the outgoing text
indicating whether the reply originated from the dispatcher (no task_id) or
a subagent (task_id provided). In production (LOBSTER_DEBUG != 'true'), text
is delivered unchanged.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/mcp is on sys.path (mirrors the pattern used throughout this package).
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401 — pre-load for patch.multiple resolution


# ---------------------------------------------------------------------------
# Label format constants (mirror the implementation so tests document intent)
# ---------------------------------------------------------------------------

DISPATCHER_LABEL = "[dispatcher]"
AGENT_LABEL_PREFIX = "[agent:"


@pytest.fixture
def dirs(tmp_path):
    """Create the directories needed by handle_send_reply."""
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    for d in (outbox, sent, processing, processed):
        d.mkdir(parents=True, exist_ok=True)
    return outbox, sent, processing, processed


def _read_outbox_text(outbox: Path) -> str:
    """Read the text field from the single outbox file."""
    files = list(outbox.glob("*.json"))
    assert len(files) == 1, f"Expected 1 outbox file, found {len(files)}"
    return json.loads(files[0].read_text())["text"]


class TestDebugSourceLabel:
    """Debug source label is prepended when LOBSTER_DEBUG=true."""

    def test_dispatcher_reply_gets_dispatcher_label(self, dirs):
        """A send_reply call without task_id is from the dispatcher — label is [dispatcher]."""
        outbox, sent, processing, processed = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ), patch.dict(os.environ, {"LOBSTER_DEBUG": "true"}):
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 12345,
                "text": "Hello from dispatcher",
                "source": "telegram",
            }))

        text = _read_outbox_text(outbox)
        assert text.startswith(DISPATCHER_LABEL), (
            f"Expected text to start with '{DISPATCHER_LABEL}', got: {text!r}"
        )
        assert "Hello from dispatcher" in text

    def test_subagent_reply_gets_agent_label(self, dirs):
        """A send_reply call with task_id is from a subagent — label is [agent:<task_id>]."""
        outbox, sent, processing, processed = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ), patch.dict(os.environ, {"LOBSTER_DEBUG": "true"}):
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 12345,
                "text": "Hello from subagent",
                "source": "telegram",
                "task_id": "code-review-42",
            }))

        text = _read_outbox_text(outbox)
        assert text.startswith(AGENT_LABEL_PREFIX), (
            f"Expected text to start with '{AGENT_LABEL_PREFIX}', got: {text!r}"
        )
        assert "code-review-42" in text
        assert "Hello from subagent" in text

    def test_long_task_id_is_truncated_in_label(self, dirs):
        """task_id values longer than 20 chars are truncated to keep labels readable."""
        outbox, sent, processing, processed = dirs
        long_task_id = "sprint-issue-99-very-long-task-identifier"

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ), patch.dict(os.environ, {"LOBSTER_DEBUG": "true"}):
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 12345,
                "text": "Hello",
                "source": "telegram",
                "task_id": long_task_id,
            }))

        text = _read_outbox_text(outbox)
        # Label must not contain the full 40-char task_id — it should be cut off
        assert long_task_id not in text.split("\n")[0], (
            "Label line should not contain the full long task_id"
        )
        # But the first 20 chars should be present
        assert long_task_id[:20] in text


class TestDebugSourceLabelOff:
    """No label is injected when LOBSTER_DEBUG is not 'true'."""

    def test_no_label_when_debug_false(self, dirs):
        """Text is unchanged when LOBSTER_DEBUG=false."""
        outbox, sent, processing, processed = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ), patch.dict(os.environ, {"LOBSTER_DEBUG": "false"}):
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 12345,
                "text": "Clean production message",
                "source": "telegram",
            }))

        text = _read_outbox_text(outbox)
        assert text == "Clean production message", (
            f"Text should be unchanged in non-debug mode, got: {text!r}"
        )

    def test_no_label_when_debug_env_absent(self, dirs):
        """Text is unchanged when LOBSTER_DEBUG env var is not set."""
        outbox, sent, processing, processed = dirs

        env = {k: v for k, v in os.environ.items() if k != "LOBSTER_DEBUG"}
        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ), patch.dict(os.environ, env, clear=True):
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 12345,
                "text": "No debug env var",
                "source": "telegram",
            }))

        text = _read_outbox_text(outbox)
        assert text == "No debug env var"

    def test_no_label_when_debug_has_task_id_but_debug_off(self, dirs):
        """task_id present but debug=false — no label injected."""
        outbox, sent, processing, processed = dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ), patch.dict(os.environ, {"LOBSTER_DEBUG": "false"}):
            from src.mcp.inbox_server import handle_send_reply

            asyncio.run(handle_send_reply({
                "chat_id": 12345,
                "text": "Subagent message in prod",
                "source": "telegram",
                "task_id": "some-task",
            }))

        text = _read_outbox_text(outbox)
        assert text == "Subagent message in prod"
