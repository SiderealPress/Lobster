"""
Tests for malformed JSON inbox file quarantine (issue #1813).

A JSON file written to inbox/ by a malfunctioning job (e.g. daily-health-check.sh
constructing JSON via shell concatenation) could contain invalid JSON. When
check_inbox tried to re-read such a file on the second pass it received a
JSONDecodeError, fell through to ``except Exception: continue``, and left the
file in inbox/ untouched.

Because wait_for_messages detects inbox files via glob and calls check_inbox
immediately when files are present, this caused a tight loop: WFM returned
immediately on every call for the entire day — causing 26 MCP restarts on
2026-04-25 before the file was manually removed.

Fix: when json.load raises JSONDecodeError on the second pass, the file is
immediately quarantined to failed/ with _permanently_failed=True, so it cannot
block the wait_for_messages loop.

Behavior tested:
- An unparseable inbox file is moved to failed/ and not returned to the dispatcher
- A quarantine metadata record is written with _permanently_failed and _last_error
- A valid message alongside a malformed file is returned normally
- A second call to check_inbox after quarantine finds an empty inbox (no tight-loop)
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))


_BASE_TS = datetime(2026, 4, 25, 6, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _valid_msg(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "source": "telegram",
        "type": "text",
        "text": f"hello from {msg_id}",
        "timestamp": _iso(_BASE_TS),
        "chat_id": 12345,
        "user_id": 12345,
    }


class TestMalformedJsonQuarantine:
    """Unparseable inbox JSON files are quarantined to failed/, not left to tight-loop."""

    def test_malformed_json_moved_to_failed(self, inbox_server_dirs: dict):
        """An inbox file with invalid JSON is quarantined and not returned."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        # Write the kind of truncated/malformed JSON that daily-health-check.sh produced
        bad_file = inbox_dir / "health-check-malformed-001.json"
        bad_file.write_text('{"source": "telegram", "type": "text", "text": "trunc')

        from src.mcp.inbox_server import handle_check_inbox

        result = asyncio.run(handle_check_inbox({}))

        # File must be gone from inbox
        assert not bad_file.exists(), "Malformed file must be removed from inbox"

        # A quarantine record must appear in failed/
        failed_files = list(failed_dir.glob("*.json"))
        assert len(failed_files) == 1, "Quarantined file must appear in failed/"

        quarantined = json.loads(failed_files[0].read_text())
        assert quarantined["_permanently_failed"] is True
        assert "_last_error" in quarantined
        assert "json" in quarantined["_last_error"].lower() or "parse" in quarantined["_last_error"].lower()

        # Dispatcher must not see the message
        result_text = result[0].text
        assert "health-check-malformed-001" not in result_text

    def test_malformed_json_alongside_valid_message(self, inbox_server_dirs: dict):
        """Valid messages are returned normally when a malformed file is quarantined."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        # Write one valid and one malformed file
        valid_file = inbox_dir / "valid-msg-001.json"
        valid_file.write_text(json.dumps(_valid_msg("valid-msg-001")))

        bad_file = inbox_dir / "malformed-msg-001.json"
        bad_file.write_text("{broken json :::}")

        from src.mcp.inbox_server import handle_check_inbox

        result = asyncio.run(handle_check_inbox({}))
        result_text = result[0].text

        # Valid message must be returned
        assert "valid-msg-001" in result_text, "Valid message must be returned"

        # Malformed file must be quarantined
        assert not bad_file.exists(), "Malformed file must be removed from inbox"
        assert (failed_dir / "malformed-msg-001.json").exists(), (
            "Malformed file must appear in failed/"
        )

        # Malformed file must not appear in output
        assert "malformed-msg-001" not in result_text

    def test_no_tight_loop_after_quarantine(self, inbox_server_dirs: dict):
        """Regression: quarantine on first call leaves inbox empty for second call.

        Before the fix, the file remained in inbox/ on every check_inbox call,
        causing wait_for_messages to return immediately in a tight loop.
        """
        inbox_dir = inbox_server_dirs["inbox"]

        bad_file = inbox_dir / "bad-json-loop-001.json"
        bad_file.write_text("not json at all")

        from src.mcp.inbox_server import handle_check_inbox

        # First call: should quarantine the file
        asyncio.run(handle_check_inbox({}))

        # Inbox must be empty after first call
        assert list(inbox_dir.glob("*.json")) == [], (
            "Malformed file must be quarantined after first check_inbox — "
            "leaving it in inbox would cause a WFM tight-loop"
        )

        # Second call: must indicate no messages (not re-return the bad file)
        result2 = asyncio.run(handle_check_inbox({}))
        result_text2 = result2[0].text.lower()
        assert "no new messages" in result_text2 or "no messages" in result_text2 or "📭" in result_text2, (
            "Second check_inbox must return empty-inbox response, not the quarantined file"
        )

    def test_quarantine_metadata_record(self, inbox_server_dirs: dict):
        """Quarantined record has the required metadata fields."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        fname = "bad-json-meta-001.json"
        (inbox_dir / fname).write_text("[this is not a json object]... oops")

        from src.mcp.inbox_server import handle_check_inbox

        asyncio.run(handle_check_inbox({}))

        quarantined = json.loads((failed_dir / fname).read_text())
        assert quarantined.get("_permanently_failed") is True, "_permanently_failed must be True"
        assert "_last_error" in quarantined, "_last_error must be set"
        assert "_last_failed_at" in quarantined, "_last_failed_at must be set"
        assert "_original_filename" in quarantined, "_original_filename must be set"
        assert quarantined["_original_filename"] == fname

    def test_completely_empty_file_quarantined(self, inbox_server_dirs: dict):
        """An empty .json file (zero bytes) is treated as unparseable and quarantined."""
        inbox_dir = inbox_server_dirs["inbox"]
        failed_dir = inbox_server_dirs["failed"]

        empty_file = inbox_dir / "empty-file-001.json"
        empty_file.write_text("")

        from src.mcp.inbox_server import handle_check_inbox

        asyncio.run(handle_check_inbox({}))

        # Must be removed from inbox (either quarantined or otherwise handled)
        assert not empty_file.exists(), "Empty file must not remain in inbox"
