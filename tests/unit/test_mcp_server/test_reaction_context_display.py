"""
Tests for reaction message display in check_inbox (issue #1583 Approach A).

Verifies that when a reaction event arrives in the inbox, the dispatcher sees
the reacted_to_text field surfaced in the formatted output so it can identify
which message was reacted to without relying on memory or context lookup.

Before this fix: "👍 reaction from User"
After this fix:  "👍 reaction from User (on: 'Let me merge #1486...')"
"""

import asyncio
import json
from pathlib import Path

import pytest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REACTED_TO_TEXT_SAMPLE = "Let me merge #1486 — looks good to me"
REACTION_EMOJI = "👍"


def _make_reaction_message(
    msg_id: str = "1234567890_reaction_99",
    emoji: str = REACTION_EMOJI,
    reacted_to_text: str = REACTED_TO_TEXT_SAMPLE,
    user_name: str = "TestUser",
    chat_id: int = 123456,
    telegram_message_id: int = 99,
) -> dict:
    """Build a reaction inbox message dict as lobster_bot.py produces it."""
    return {
        "id": msg_id,
        "source": "telegram",
        "type": "reaction",
        "chat_id": chat_id,
        "user_name": user_name,
        "telegram_message_id": telegram_message_id,
        "emoji": emoji,
        "reacted_to_text": reacted_to_text,
        "text": f"[Reaction: {emoji} on message {telegram_message_id}]",
        "timestamp": "2026-04-15T12:00:00Z",
    }


def _run_check_inbox(inbox_dir: Path) -> str:
    """Run handle_check_inbox with inbox_dir patched and return formatted text."""
    with patch.multiple("src.mcp.inbox_server", INBOX_DIR=inbox_dir):
        from src.mcp.inbox_server import handle_check_inbox

        result = asyncio.run(handle_check_inbox({}))
        return result[0].text


# ---------------------------------------------------------------------------
# Tests — reaction context surfaced when reacted_to_text present
# ---------------------------------------------------------------------------


class TestReactionContextDisplay:
    """Dispatcher sees reacted_to_text in formatted check_inbox output."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    def test_reacted_to_text_appears_in_output_when_present(self, inbox_dir: Path):
        """When reacted_to_text is non-empty, it appears in the formatted output."""
        msg = _make_reaction_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        output = _run_check_inbox(inbox_dir)

        assert REACTED_TO_TEXT_SAMPLE in output

    def test_reaction_output_includes_emoji(self, inbox_dir: Path):
        """The emoji itself is included in the formatted header line."""
        msg = _make_reaction_message(emoji="✅")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        output = _run_check_inbox(inbox_dir)

        assert "✅" in output

    def test_reaction_output_includes_user_name(self, inbox_dir: Path):
        """The sender's name is included in the formatted header line."""
        msg = _make_reaction_message(user_name="TestUser")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        output = _run_check_inbox(inbox_dir)

        assert "TestUser" in output

    def test_reaction_context_uses_on_prefix(self, inbox_dir: Path):
        """The reacted_to_text is rendered with an 'on:' prefix for readability."""
        msg = _make_reaction_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        output = _run_check_inbox(inbox_dir)

        assert "on:" in output

    # ---------------------------------------------------------------------------
    # Tests — graceful fallback when reacted_to_text is absent or empty
    # ---------------------------------------------------------------------------

    def test_no_crash_when_reacted_to_text_empty(self, inbox_dir: Path):
        """When reacted_to_text is empty string, the context snippet is not shown."""
        msg = _make_reaction_message(reacted_to_text="")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        output = _run_check_inbox(inbox_dir)

        # Should still mention the emoji and user
        assert REACTION_EMOJI in output
        assert "TestUser" in output
        # The context parenthetical "(on: '...')" must NOT appear when text is empty.
        # We check for the full pattern rather than "on:" alone to avoid false positives
        # from the raw text field "[Reaction: emoji on message N]".
        assert "(on:" not in output

    def test_no_crash_when_reacted_to_text_missing(self, inbox_dir: Path):
        """When reacted_to_text field is absent from JSON, output renders gracefully."""
        msg = _make_reaction_message()
        del msg["reacted_to_text"]
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        output = _run_check_inbox(inbox_dir)

        assert REACTION_EMOJI in output
        assert "TestUser" in output
        # Same: context parenthetical must be absent when the field is missing.
        assert "(on:" not in output
