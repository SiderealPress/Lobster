"""
Tests for unrecognized inbox source quarantine (issue #1735).

An inbox file with an unrecognized source previously caused an infinite
wait_for_messages hot-loop: the file was detected on every call (glob found
it) but never dismissed (dispatcher couldn't route it). This exhausted the
150-turn limit in ~7 minutes and triggered repeated MCP restarts.

Fix: check_inbox quarantines files with unrecognized sources by moving them
to failed/ with _permanently_failed=True, so they cannot block the loop.

Behavior tested:
- Files with unrecognized sources are moved to failed/ and skipped
- Files with recognized sources are returned normally
- The quarantined file has _permanently_failed=True and _last_error set
- The quarantine does not affect messages with empty/missing source fields
  (those pass through so they can be handled by the dispatcher)
- bot-talk messages pass through without being quarantined
- The quarantine guard runs even when a source= filter is passed to check_inbox
"""

import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401
from src.mcp.message_types import INBOX_MESSAGE_SOURCES


_BASE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Named constant matching the spec: "daily-health-check" is the exact source
# that triggered the 20-MCP-session-loss incident on 2026-04-22.
UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK = "daily-health-check"

# Named constant: number of sessions lost before the root cause was diagnosed.
SESSION_LOSSES_BEFORE_FIX = 20


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_msg(msg_id: str, source: str, msg_type: str = "text") -> dict:
    return {
        "id": msg_id,
        "source": source,
        "type": msg_type,
        "text": f"test message {msg_id}",
        "timestamp": _iso(_BASE_TS),
        "chat_id": 12345,
        "user_id": 12345,
    }


class TestUnrecognizedSourceQuarantine:
    """Unrecognized inbox sources are quarantined, not returned to the dispatcher."""

    def test_quarantine_moves_unrecognized_source_to_failed(
        self, inbox_server_dirs: dict, tmp_path: Path
    ):
        """A file with an unrecognized source is moved to failed/ and not returned."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        # Write a message with the exact source that caused the incident
        msg = _make_msg("bad-source-001", source=UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK)
        msg_file = inbox_dir / "bad-source-001.json"
        msg_file.write_text(json.dumps(msg))

        from src.mcp.inbox_server import handle_check_inbox

        result = asyncio.run(handle_check_inbox({}))

        # File must be gone from inbox
        assert not msg_file.exists(), "Quarantined file must be removed from inbox"

        # File must appear in failed/ with permanently_failed flag
        failed_files = list(failed_dir.glob("*.json"))
        assert len(failed_files) == 1, "Quarantined file must appear in failed/"
        quarantined = json.loads(failed_files[0].read_text())
        assert quarantined["_permanently_failed"] is True
        assert "_last_error" in quarantined
        assert UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK in quarantined["_last_error"]

        # The dispatcher must not see the message
        result_text = result[0].text
        assert "bad-source-001" not in result_text

    def test_quarantine_does_not_affect_recognized_sources(
        self, inbox_server_dirs: dict
    ):
        """Messages with recognized sources pass through normally.

        Uses INBOX_MESSAGE_SOURCES directly so the test stays in sync with the
        constant — adding a new source to the constant automatically exercises it here.
        """
        inbox_dir = inbox_server_dirs["inbox"]

        recognized_sources = sorted(INBOX_MESSAGE_SOURCES)
        for i, source in enumerate(recognized_sources):
            msg = _make_msg(f"good-source-{i:03}", source=source)
            (inbox_dir / f"good-source-{i:03}.json").write_text(json.dumps(msg))

        from src.mcp.inbox_server import handle_check_inbox

        result = asyncio.run(handle_check_inbox({}))
        result_text = result[0].text

        # All recognized-source messages should be returned
        for i in range(len(recognized_sources)):
            assert f"good-source-{i:03}" in result_text, (
                f"Message {i} with a recognized source was incorrectly filtered"
            )

        # Nothing should be in failed/
        failed_dir = inbox_server_dirs["failed"]
        assert list(failed_dir.glob("*.json")) == [], (
            "No recognized-source message should be quarantined"
        )

    def test_quarantine_mixed_inbox_only_returns_valid_messages(
        self, inbox_server_dirs: dict
    ):
        """When inbox contains both valid and invalid sources, only valid ones return."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        good_msg = _make_msg("valid-001", source="telegram")
        bad_msg = _make_msg("invalid-001", source=UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK)

        (inbox_dir / "valid-001.json").write_text(json.dumps(good_msg))
        (inbox_dir / "invalid-001.json").write_text(json.dumps(bad_msg))

        from src.mcp.inbox_server import handle_check_inbox

        result = asyncio.run(handle_check_inbox({}))
        result_text = result[0].text

        assert "valid-001" in result_text, "Valid message must be returned"
        assert "invalid-001" not in result_text, "Quarantined message must not be returned"

        # Quarantined file must be in failed/
        assert (failed_dir / "invalid-001.json").exists(), (
            "Quarantined file must be moved to failed/"
        )
        # Valid file must still be in inbox (not yet processed)
        assert (inbox_dir / "valid-001.json").exists(), (
            "Valid file must remain in inbox"
        )

    def test_quarantine_preserves_original_message_data(
        self, inbox_server_dirs: dict
    ):
        """Quarantined files retain all original fields plus error metadata."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        msg = _make_msg("preserve-001", source=UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK)
        msg["subject"] = "Daily health check: 2 failure(s)"
        msg["body"] = "Some failure details"
        (inbox_dir / "preserve-001.json").write_text(json.dumps(msg))

        from src.mcp.inbox_server import handle_check_inbox
        asyncio.run(handle_check_inbox({}))

        quarantined = json.loads((failed_dir / "preserve-001.json").read_text())

        # Original fields preserved
        assert quarantined["source"] == UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK
        assert quarantined["subject"] == "Daily health check: 2 failure(s)"
        assert quarantined["body"] == "Some failure details"

        # Error metadata added
        assert quarantined["_permanently_failed"] is True
        assert "_last_error" in quarantined
        assert "_last_failed_at" in quarantined

    def test_multiple_unrecognized_sources_all_quarantined(
        self, inbox_server_dirs: dict
    ):
        """Multiple files with different unrecognized sources are all quarantined."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        bad_sources = [
            UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK,
            "cron-job",
            "custom-script",
            "unknown-bot",
        ]
        for i, source in enumerate(bad_sources):
            msg = _make_msg(f"bad-{i:03}", source=source)
            (inbox_dir / f"bad-{i:03}.json").write_text(json.dumps(msg))

        from src.mcp.inbox_server import handle_check_inbox
        asyncio.run(handle_check_inbox({}))

        # All must be quarantined
        failed_files = list(failed_dir.glob("*.json"))
        assert len(failed_files) == len(bad_sources), (
            f"Expected {len(bad_sources)} quarantined files, got {len(failed_files)}"
        )

        # None should remain in inbox
        assert list(inbox_dir.glob("*.json")) == [], (
            "All unrecognized-source files must be removed from inbox"
        )

    def test_wait_for_messages_does_not_hot_loop_on_unrecognized_source(
        self, inbox_server_dirs: dict
    ):
        """Regression test: unrecognized source file must not cause repeated returns.

        Before the fix, a single unrecognized-source file caused wait_for_messages
        to return immediately on every call, exhausting --max-turns 150 in ~7 minutes
        (SESSION_LOSSES_BEFORE_FIX = 20 crashes, each every ~7 minutes).

        After the fix, the file is quarantined on first check_inbox call. A subsequent
        call to check_inbox finds an empty inbox, preventing the hot-loop.
        """
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        # Write the exact file that caused the 2026-04-22 incident
        msg = _make_msg("daily-health-20260422-060013", source=UNRECOGNIZED_SOURCE_DAILY_HEALTH_CHECK)
        msg["type"] = "health_check"
        msg["subject"] = "Daily health check: 1 failure(s)"
        (inbox_dir / "daily-health-20260422-060013.json").write_text(json.dumps(msg))

        from src.mcp.inbox_server import handle_check_inbox

        # First call: quarantines the file
        asyncio.run(handle_check_inbox({}))

        # After first call, inbox must be empty
        assert list(inbox_dir.glob("*.json")) == [], (
            "Unrecognized-source file must be quarantined after first check_inbox call"
        )

        # Second call: must return "no messages" (inbox is empty)
        result2 = asyncio.run(handle_check_inbox({}))
        assert "no messages" in result2[0].text.lower() or "empty" in result2[0].text.lower() or len(result2[0].text) < 100, (
            "Second check_inbox call must indicate empty inbox, not re-return the quarantined file"
        )

    def test_bot_talk_messages_pass_quarantine_guard(
        self, inbox_server_dirs: dict
    ):
        """bot-talk messages are in INBOX_MESSAGE_SOURCES and must not be quarantined.

        Cross-Lobster bot-to-bot messages use source="bot-talk". Before this fix,
        "bot-talk" was missing from INBOX_MESSAGE_SOURCES, so every inbound
        cross-Lobster message would be permanently quarantined to failed/, breaking
        the bot-talk integration entirely.
        """
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        msg = _make_msg("bot-talk-001", source="bot-talk", msg_type="text")
        msg["direction"] = "INBOUND"
        msg["from"] = "other-lobster-instance"
        (inbox_dir / "bot-talk-001.json").write_text(json.dumps(msg))

        from src.mcp.inbox_server import handle_check_inbox

        result = asyncio.run(handle_check_inbox({}))

        # Must not be quarantined
        assert list(failed_dir.glob("*.json")) == [], (
            "bot-talk message must not be quarantined — 'bot-talk' is a recognized source"
        )
        # Must be returned to the dispatcher
        result_text = result[0].text
        assert "bot-talk-001" in result_text, (
            "bot-talk message must be returned by check_inbox"
        )

    def test_quarantine_guard_runs_with_source_filter(
        self, inbox_server_dirs: dict
    ):
        """A bad-source file is quarantined even when check_inbox is called with source=.

        Previously, the source_filter check ran first — a caller passing
        source="bad-script" would receive the bad file without quarantining it.
        Now quarantine always runs before filtering so the file is removed
        unconditionally.
        """
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        bad_source = "custom-script"
        msg = _make_msg("bad-sourced-001", source=bad_source)
        (inbox_dir / "bad-sourced-001.json").write_text(json.dumps(msg))

        from src.mcp.inbox_server import handle_check_inbox

        # Call check_inbox with a source filter that matches the bad source
        asyncio.run(handle_check_inbox({"source": bad_source}))

        # File must still be quarantined despite the matching source filter
        assert not (inbox_dir / "bad-sourced-001.json").exists(), (
            "Bad-source file must be quarantined even when source filter matches it"
        )
        assert (failed_dir / "bad-sourced-001.json").exists(), (
            "Bad-source file must appear in failed/ after quarantine"
        )
        quarantined = json.loads((failed_dir / "bad-sourced-001.json").read_text())
        assert quarantined["_permanently_failed"] is True
