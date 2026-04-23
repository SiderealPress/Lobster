"""
Unit tests for lobster_talk.standalone.lobstertalk_poll.

Tests derived from the standalone script's documented behavior (exit codes,
env vars, state format, cursor advancement, inbox writing).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure tooling/src is on path
_SRC = Path(__file__).parent.parent.parent.parent / "tooling" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import lobster_talk.standalone.lobstertalk_poll as poll


# ---------------------------------------------------------------------------
# _default_state
# ---------------------------------------------------------------------------

class TestDefaultState:
    def test_contains_last_seen_ts(self):
        state = poll._default_state()
        assert "last_seen_ts" in state

    def test_last_seen_ts_is_approximately_one_hour_ago(self):
        state = poll._default_state()
        ts = datetime.fromisoformat(state["last_seen_ts"])
        delta = datetime.now(timezone.utc) - ts
        assert timedelta(minutes=55) <= delta <= timedelta(minutes=65)


# ---------------------------------------------------------------------------
# load_state / write_state
# ---------------------------------------------------------------------------

class TestLoadState:
    def test_returns_defaults_when_file_missing(self, tmp_path):
        state = poll.load_state(tmp_path / "nonexistent.json")
        assert "last_seen_ts" in state

    def test_reads_existing_state(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text(json.dumps({"last_seen_ts": "2026-04-10T10:00:00+00:00"}))
        state = poll.load_state(f)
        assert state["last_seen_ts"] == "2026-04-10T10:00:00+00:00"

    def test_returns_defaults_on_corrupted_file(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("{invalid json")
        state = poll.load_state(f)
        assert "last_seen_ts" in state


class TestWriteState:
    def test_writes_and_reads_back(self, tmp_path):
        f = tmp_path / "state.json"
        data = {"last_seen_ts": "2026-04-10T12:00:00+00:00"}
        poll.write_state(f, data)
        loaded = json.loads(f.read_text())
        assert loaded["last_seen_ts"] == "2026-04-10T12:00:00+00:00"

    def test_no_tmp_file_left_behind(self, tmp_path):
        f = tmp_path / "state.json"
        poll.write_state(f, {"last_seen_ts": "2026-04-10T12:00:00+00:00"})
        assert not f.with_suffix(".tmp").exists()


# ---------------------------------------------------------------------------
# advance_cursor — pure function
# ---------------------------------------------------------------------------

class TestAdvanceCursor:
    def test_advances_to_max_timestamp(self):
        state = {"last_seen_ts": "2026-04-10T12:00:00+00:00"}
        messages = [
            {"timestamp": "2026-04-10T12:02:00+00:00"},
            {"timestamp": "2026-04-10T12:05:00+00:00"},
            {"timestamp": "2026-04-10T12:01:00+00:00"},
        ]
        updated = poll.advance_cursor(state, messages)
        assert updated["last_seen_ts"] == "2026-04-10T12:05:00+00:00"

    def test_does_not_regress(self):
        state = {"last_seen_ts": "2026-04-10T12:00:00+00:00"}
        messages = [{"timestamp": "2026-04-10T11:00:00+00:00"}]
        updated = poll.advance_cursor(state, messages)
        assert updated["last_seen_ts"] == "2026-04-10T12:00:00+00:00"

    def test_empty_messages_returns_same_state(self):
        state = {"last_seen_ts": "2026-04-10T12:00:00+00:00"}
        updated = poll.advance_cursor(state, [])
        assert updated is state

    def test_does_not_mutate_input(self):
        state = {"last_seen_ts": "2026-04-10T12:00:00+00:00"}
        original_ts = state["last_seen_ts"]
        poll.advance_cursor(state, [{"timestamp": "2026-04-10T13:00:00+00:00"}])
        assert state["last_seen_ts"] == original_ts


# ---------------------------------------------------------------------------
# build_inbox_message — pure function
# ---------------------------------------------------------------------------

class TestBuildInboxMessage:
    def _raw(self, **kw) -> dict:
        return {
            "sender": "AlbertLobster",
            "content": "Hello",
            "timestamp": "2026-04-10T12:00:00+00:00",
            **kw,
        }

    def test_source_is_bot_talk(self):
        msg = poll.build_inbox_message(self._raw(), my_name="MyLobster")
        assert msg["source"] == "bot-talk"

    def test_type_is_text(self):
        msg = poll.build_inbox_message(self._raw(), my_name="MyLobster")
        assert msg["type"] == "text"

    def test_sender_maps_to_user_name_and_from(self):
        msg = poll.build_inbox_message(self._raw(sender="AlbertLobster"), my_name="MyLobster")
        assert msg["user_name"] == "AlbertLobster"
        assert msg["from"] == "AlbertLobster"

    def test_content_maps_to_text(self):
        msg = poll.build_inbox_message(self._raw(content="hey"), my_name="MyLobster")
        assert msg["text"] == "hey"

    def test_to_field_is_my_name(self):
        msg = poll.build_inbox_message(self._raw(), my_name="MyLobster")
        assert msg["to"] == "MyLobster"

    def test_direction_is_inbound(self):
        msg = poll.build_inbox_message(self._raw(), my_name="MyLobster")
        assert msg["direction"] == "INBOUND"

    def test_id_contains_bot_talk_marker(self):
        msg = poll.build_inbox_message(self._raw(), my_name="MyLobster")
        assert "bot_talk" in msg["id"]


# ---------------------------------------------------------------------------
# write_inbox_message
# ---------------------------------------------------------------------------

class TestWriteInboxMessage:
    def test_creates_file_in_inbox_dir(self, tmp_path):
        inbox = tmp_path / "inbox"
        raw = {"sender": "AlbertLobster", "content": "hi", "timestamp": "2026-04-10T12:00:00+00:00"}
        filename = poll.write_inbox_message(inbox, raw, "MyLobster")
        assert (inbox / filename).exists()

    def test_written_file_is_valid_json(self, tmp_path):
        inbox = tmp_path / "inbox"
        raw = {"sender": "AlbertLobster", "content": "hi", "timestamp": "2026-04-10T12:00:00+00:00"}
        filename = poll.write_inbox_message(inbox, raw, "MyLobster")
        content = json.loads((inbox / filename).read_text())
        assert content["source"] == "bot-talk"

    def test_no_tmp_file_left_behind(self, tmp_path):
        inbox = tmp_path / "inbox"
        raw = {"sender": "AlbertLobster", "content": "hi", "timestamp": "2026-04-10T12:00:00+00:00"}
        filename = poll.write_inbox_message(inbox, raw, "MyLobster")
        assert not (inbox / Path(filename).stem).with_suffix(".tmp").exists()
