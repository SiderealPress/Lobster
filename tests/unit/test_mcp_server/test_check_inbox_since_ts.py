"""
Tests for check_inbox since_ts parameter (issue #607 compact_catchup feature).

Verifies that:
- since_ts filters messages by timestamp from both inbox/ and processed/
- Messages older than since_ts are excluded
- Excluded subtypes (self_check, compact-reminder, compact_catchup,
  subagent_notification) are stripped from since_ts results
- Absence of since_ts preserves original inbox-only behaviour
- _parse_iso_timestamp handles various ISO 8601 formats
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


def _iso(dt: datetime) -> str:
    """Format datetime to ISO 8601 UTC string as stored in messages."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _make_msg(msg_id: str, ts: datetime, subtype: str = None, msg_type: str = "text", source: str = "telegram") -> dict:
    msg = {
        "id": msg_id,
        "source": source,
        "chat_id": 12345,
        "user_id": 12345,
        "username": "testuser",
        "user_name": "Test",
        "type": msg_type,
        "text": f"message {msg_id}",
        "timestamp": _iso(ts),
    }
    if subtype:
        msg["subtype"] = subtype
    return msg


_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_BEFORE = _BASE - timedelta(hours=1)   # 11:00
_WINDOW_START = _BASE                   # 12:00 — since_ts
_AFTER = _BASE + timedelta(hours=1)    # 13:00


class TestParseIsoTimestamp:
    """Tests for _parse_iso_timestamp helper."""

    def test_z_suffix(self):
        from src.mcp.inbox_server import _parse_iso_timestamp
        ts = _parse_iso_timestamp("2026-01-01T12:00:00Z")
        assert ts is not None
        assert abs(ts - _WINDOW_START.timestamp()) < 1

    def test_utc_offset(self):
        from src.mcp.inbox_server import _parse_iso_timestamp
        ts = _parse_iso_timestamp("2026-01-01T12:00:00+00:00")
        assert ts is not None
        assert abs(ts - _WINDOW_START.timestamp()) < 1

    def test_microseconds(self):
        from src.mcp.inbox_server import _parse_iso_timestamp
        ts = _parse_iso_timestamp("2026-01-01T12:00:00.000000")
        assert ts is not None

    def test_none_on_empty_string(self):
        from src.mcp.inbox_server import _parse_iso_timestamp
        assert _parse_iso_timestamp("") is None

    def test_none_on_invalid_string(self):
        from src.mcp.inbox_server import _parse_iso_timestamp
        assert _parse_iso_timestamp("not-a-timestamp") is None


class TestCheckInboxSinceTs:
    """Tests for check_inbox with since_ts parameter."""

    @pytest.fixture
    def dirs(self, tmp_path):
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        inbox.mkdir()
        processed.mkdir()
        return {"inbox": inbox, "processed": processed}

    def _run(self, dirs, args):
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=dirs["inbox"],
            PROCESSED_DIR=dirs["processed"],
        ):
            from src.mcp.inbox_server import handle_check_inbox
            return asyncio.run(handle_check_inbox(args))

    def test_since_ts_returns_only_messages_at_or_after_window(self, dirs):
        """Messages before since_ts are excluded; messages at/after are included."""
        old_msg = _make_msg("old", _BEFORE)
        new_msg = _make_msg("new", _AFTER)

        (dirs["processed"] / "old.json").write_text(json.dumps(old_msg))
        (dirs["processed"] / "new.json").write_text(json.dumps(new_msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "new" in text
        assert "old" not in text or "No messages" in text

    def test_since_ts_scans_both_inbox_and_processed(self, dirs):
        """Messages in inbox/ AND processed/ are included when since_ts is set."""
        inbox_msg = _make_msg("inbox1", _AFTER)
        processed_msg = _make_msg("proc1", _AFTER)

        (dirs["inbox"] / "inbox1.json").write_text(json.dumps(inbox_msg))
        (dirs["processed"] / "proc1.json").write_text(json.dumps(processed_msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "inbox1" in text
        assert "proc1" in text

    def test_since_ts_excludes_compact_reminder_subtype(self, dirs):
        """compact-reminder messages are excluded even if within the time window."""
        msg = _make_msg("c1", _AFTER, subtype="compact-reminder", source="system")
        (dirs["processed"] / "c1.json").write_text(json.dumps(msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "c1" not in text or "No messages" in text

    def test_since_ts_excludes_self_check_subtype(self, dirs):
        """self_check messages are excluded even if within the time window."""
        msg = _make_msg("sc1", _AFTER, subtype="self_check", source="system")
        (dirs["processed"] / "sc1.json").write_text(json.dumps(msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "sc1" not in text or "No messages" in text

    def test_since_ts_excludes_compact_catchup_subtype(self, dirs):
        """compact_catchup messages are excluded."""
        msg = _make_msg("cu1", _AFTER, subtype="compact_catchup", source="system")
        (dirs["processed"] / "cu1.json").write_text(json.dumps(msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "cu1" not in text or "No messages" in text

    def test_since_ts_excludes_subagent_notification_subtype(self, dirs):
        """subagent_notification messages are excluded."""
        msg = _make_msg("sn1", _AFTER, subtype="subagent_notification", msg_type="subagent_notification")
        (dirs["processed"] / "sn1.json").write_text(json.dumps(msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "sn1" not in text or "No messages" in text

    def test_since_ts_includes_user_messages(self, dirs):
        """Regular user messages within the window are included."""
        msg = _make_msg("u1", _AFTER, source="telegram")
        (dirs["processed"] / "u1.json").write_text(json.dumps(msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "u1" in text or "1 message" in text.lower()

    def test_since_ts_empty_window_returns_no_messages(self, dirs):
        """No messages in window returns appropriate empty response."""
        # All messages are before the window
        old_msg = _make_msg("old1", _BEFORE)
        (dirs["processed"] / "old1.json").write_text(json.dumps(old_msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "No messages" in text

    def test_without_since_ts_only_reads_inbox(self, dirs):
        """Without since_ts, check_inbox only reads from inbox/ not processed/."""
        inbox_msg = _make_msg("i1", _AFTER)
        processed_msg = _make_msg("p1", _AFTER)

        (dirs["inbox"] / "i1.json").write_text(json.dumps(inbox_msg))
        (dirs["processed"] / "p1.json").write_text(json.dumps(processed_msg))

        result = self._run(dirs, {})
        text = result[0].text
        assert "i1" in text
        assert "p1" not in text  # processed not scanned without since_ts

    def test_since_ts_respects_limit(self, dirs):
        """limit parameter still caps results in since_ts mode."""
        for i in range(10):
            msg = _make_msg(f"m{i}", _AFTER)
            (dirs["processed"] / f"m{i}.json").write_text(json.dumps(msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z", "limit": 3})
        text = result[0].text
        # The output header says "3 new message(s)" — assert exactly 3 were returned
        assert "3 new message" in text

    def test_since_ts_includes_boundary_message(self, dirs):
        """A message exactly at since_ts should be included (>= boundary)."""
        msg = _make_msg("exact", _WINDOW_START)
        (dirs["processed"] / "exact.json").write_text(json.dumps(msg))

        result = self._run(dirs, {"since_ts": "2026-01-01T12:00:00Z"})
        text = result[0].text
        assert "exact" in text or "1 message" in text.lower()
