"""
Tests for check_inbox offset/pagination and total-count (#1316).

Verifies that:
- offset parameter skips the correct number of messages
- total count is reported in the response header when messages are truncated
- pagination hint appears in the footer when more messages remain
- offset defaults to 0 (no skip)
- limit + offset combination pages through results correctly
- since_ts mode also respects offset and reports total
"""

import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401


_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_msg(msg_id: str, offset_minutes: int = 0) -> dict:
    ts = _BASE + timedelta(minutes=offset_minutes)
    return {
        "id": msg_id,
        "source": "telegram",
        "chat_id": 12345,
        "user_id": 12345,
        "username": "testuser",
        "user_name": "Test",
        "type": "text",
        "text": f"message {msg_id}",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f"),
    }


def _write_msgs(inbox_dir: Path, msgs: list[dict]) -> None:
    for msg in msgs:
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))


@pytest.fixture
def dirs(tmp_path):
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    inbox.mkdir()
    processed.mkdir()
    return {"inbox": inbox, "processed": processed}


def _run(dirs, args):
    with patch.multiple(
        "src.mcp.inbox_server",
        INBOX_DIR=dirs["inbox"],
        PROCESSED_DIR=dirs["processed"],
    ):
        from src.mcp.inbox_server import handle_check_inbox
        return asyncio.run(handle_check_inbox(args))


class TestCheckInboxPagination:
    """Tests for offset and total-count in regular inbox mode."""

    def test_default_offset_returns_first_page(self, dirs):
        """Without offset, returns first N messages (same as before)."""
        msgs = [_make_msg(f"m{i}", i) for i in range(5)]
        _write_msgs(dirs["inbox"], msgs)

        result = _run(dirs, {"limit": 3})
        text = result[0].text
        # Shows first 3 out of 5
        assert "3 new message" in text
        assert "1–3 of 5" in text

    def test_offset_skips_messages(self, dirs):
        """offset=2 skips the first 2 messages."""
        msgs = [_make_msg(f"m{i}", i) for i in range(5)]
        _write_msgs(dirs["inbox"], msgs)

        result = _run(dirs, {"limit": 2, "offset": 2})
        text = result[0].text
        # Shows messages 3-4 (indices 2-3)
        assert "2 new message" in text
        assert "3–4 of 5" in text

    def test_offset_beyond_total_returns_empty(self, dirs):
        """offset >= total with messages present returns empty."""
        msgs = [_make_msg(f"m{i}", i) for i in range(3)]
        _write_msgs(dirs["inbox"], msgs)

        result = _run(dirs, {"limit": 10, "offset": 5})
        text = result[0].text
        # Offset beyond total — no messages in the slice, but total was 3
        assert "0 new message" in text or "No new messages" in text

    def test_no_pagination_info_when_all_fit(self, dirs):
        """When all messages fit in limit, no pagination hint is shown."""
        msgs = [_make_msg(f"m{i}", i) for i in range(3)]
        _write_msgs(dirs["inbox"], msgs)

        result = _run(dirs, {"limit": 10})
        text = result[0].text
        # No truncation: no "of N" or "more message" hint needed
        assert "of 3" not in text
        assert "more message" not in text.lower()

    def test_footer_hint_when_truncated(self, dirs):
        """Footer shows next-page hint when messages were truncated."""
        msgs = [_make_msg(f"m{i}", i) for i in range(7)]
        _write_msgs(dirs["inbox"], msgs)

        result = _run(dirs, {"limit": 3})
        text = result[0].text
        # Should mention remaining count and offset to continue
        assert "more message" in text.lower()
        assert "offset=3" in text

    def test_second_page_no_more_hint(self, dirs):
        """When on the last page, no 'more messages' hint appears."""
        msgs = [_make_msg(f"m{i}", i) for i in range(5)]
        _write_msgs(dirs["inbox"], msgs)

        result = _run(dirs, {"limit": 3, "offset": 3})
        text = result[0].text
        # Last page: 2 messages, no more hint
        assert "more message" not in text.lower()

    def test_total_count_reported_in_header(self, dirs):
        """Total count appears in the header when messages are truncated."""
        msgs = [_make_msg(f"m{i}", i) for i in range(8)]
        _write_msgs(dirs["inbox"], msgs)

        result = _run(dirs, {"limit": 5})
        text = result[0].text
        assert "of 8" in text


class TestCheckInboxPaginationSinceTs:
    """Tests for offset and total-count in since_ts (historical scan) mode."""

    def test_since_ts_with_offset_skips_messages(self, dirs):
        """offset works in since_ts mode too."""
        for i in range(6):
            msg = _make_msg(f"m{i}", i + 10)  # all after base
            (dirs["processed"] / f"m{i}.json").write_text(json.dumps(msg))

        result = _run(dirs, {"since_ts": "2026-01-01T12:00:00Z", "limit": 2, "offset": 2})
        text = result[0].text
        assert "2 new message" in text
        assert "3–4 of 6" in text

    def test_since_ts_total_in_header(self, dirs):
        """Total appears in header for since_ts mode when truncated."""
        for i in range(5):
            msg = _make_msg(f"m{i}", i + 5)
            (dirs["processed"] / f"m{i}.json").write_text(json.dumps(msg))

        result = _run(dirs, {"since_ts": "2026-01-01T12:00:00Z", "limit": 2})
        text = result[0].text
        assert "of 5" in text
