"""
Unit tests for hooks/require-reply-to-message-id.py

Tests cover:
- Non-send_reply tool calls pass through (exit 0)
- Telegram send_reply with reply_to_message_id > 0 passes (exit 0)
- Telegram send_reply missing reply_to_message_id is blocked (exit 2)
- Telegram send_reply with reply_to_message_id=0 is blocked (exit 2)
- Non-Telegram source passes through (exit 0)
- chat_id=0 (proactive/system send) passes through (exit 0)
- Absent source defaults to telegram enforcement (exit 2 when no reply_to_message_id)
- Malformed input passes through (exit 0 fail-open)
- Block message contains actionable text in stderr

Issue: #1168
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "require-reply-to-message-id.py"


def _run(tool_input: dict, tool_name: str = "mcp__lobster-inbox__send_reply") -> tuple[int, str, str]:
    """Run the hook with the given input; return (exit_code, stdout, stderr)."""
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


class TestRequireReplyToMessageId:

    def test_non_send_reply_tool_is_allowed(self):
        """Non-send_reply tool calls are not checked."""
        rc, _, _ = _run({"chat_id": 123456}, tool_name="mcp__lobster-inbox__mark_processed")
        assert rc == 0

    def test_telegram_with_valid_reply_to_message_id_allowed(self):
        """Telegram send_reply with a positive reply_to_message_id passes."""
        rc, _, _ = _run({
            "source": "telegram",
            "chat_id": 123456,
            "text": "Hello!",
            "reply_to_message_id": 99,
        })
        assert rc == 0

    def test_telegram_missing_reply_to_message_id_blocked(self):
        """Telegram send_reply without reply_to_message_id is blocked."""
        rc, _, stderr = _run({
            "source": "telegram",
            "chat_id": 123456,
            "text": "Hello!",
        })
        assert rc == 2
        assert "reply_to_message_id" in stderr

    def test_telegram_zero_reply_to_message_id_blocked(self):
        """Telegram send_reply with reply_to_message_id=0 is blocked."""
        rc, _, stderr = _run({
            "source": "telegram",
            "chat_id": 123456,
            "text": "Hello!",
            "reply_to_message_id": 0,
        })
        assert rc == 2
        assert "reply_to_message_id" in stderr

    def test_absent_source_defaults_to_telegram_enforcement(self):
        """When source is absent, it defaults to telegram — enforce reply_to_message_id."""
        rc, _, stderr = _run({
            "chat_id": 123456,
            "text": "Hello!",
        })
        assert rc == 2
        assert "reply_to_message_id" in stderr

    def test_slack_source_bypasses_enforcement(self):
        """Slack send_reply without reply_to_message_id is allowed."""
        rc, _, _ = _run({
            "source": "slack",
            "chat_id": 123456,
            "text": "Hello!",
        })
        assert rc == 0

    def test_system_source_bypasses_enforcement(self):
        """System messages are not Telegram — allowed without reply_to_message_id."""
        rc, _, _ = _run({
            "source": "system",
            "chat_id": 123456,
            "text": "System alert",
        })
        assert rc == 0

    def test_proactive_send_chat_id_zero_is_exempt(self):
        """chat_id=0 means no originating message — proactive send is allowed."""
        rc, _, _ = _run({
            "source": "telegram",
            "chat_id": 0,
            "text": "Good morning!",
        })
        assert rc == 0

    def test_malformed_input_fails_open(self):
        """Unparseable JSON input allows the call rather than blocking it."""
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="not-valid-json{{{",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_telegram_uppercase_source_is_normalised(self):
        """Source comparison is case-insensitive."""
        rc, _, _ = _run({
            "source": "Telegram",
            "chat_id": 123456,
            "text": "Hello!",
            "reply_to_message_id": 42,
        })
        assert rc == 0

    def test_telegram_missing_id_blocked_stderr_has_guidance(self):
        """Stderr message explains how to exempt proactive sends."""
        rc, _, stderr = _run({
            "source": "telegram",
            "chat_id": 123456,
            "text": "Hello!",
        })
        assert rc == 2
        assert "chat_id=0" in stderr
