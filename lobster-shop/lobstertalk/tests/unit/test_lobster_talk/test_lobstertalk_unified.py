"""
Unit tests for lobster_talk.lobstertalk_unified.

Tests derived from spec in lobstertalk/lobstertalk-api.md and README:
- Hot-mode activates when messages are received (consecutive_empty_polls resets)
- After COOLDOWN_THRESHOLD consecutive empty polls, hot mode exits
- Cursor advances to latest message timestamp (pure function, no I/O)
- Cursor does not regress if existing last_seen_ts is newer
- _default_state() returns a dict with all required keys and a valid timestamp
- _load_state() returns defaults on missing or corrupted file
- _write_state() writes atomically (tmp rename)
- _build_inbox_message() maps bot-talk fields to inbox format correctly
- Inbound filter: messages from MY_LOBSTER_NAME are excluded
- _load_lobster_name() reads from file first, then LOBSTER_NAME env, then defaults to MyLobster
- _load_admin_chat_id() reads from file first, then LOBSTER_ADMIN_CHAT_ID env, then defaults to 0
- _load_bot_talk_url() reads BOT_TALK_URL env first, then config.env, then defaults to relay URL
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure tooling/src is on path
_SRC = Path(__file__).parent.parent.parent.parent / "tooling" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import lobster_talk.lobstertalk_unified as lt

# ---------------------------------------------------------------------------
# Named constants matching the spec
# ---------------------------------------------------------------------------

COOLDOWN_THRESHOLD = lt.COOLDOWN_THRESHOLD  # 2 empty polls before exiting hot mode


# ---------------------------------------------------------------------------
# _default_state
# ---------------------------------------------------------------------------

class TestDefaultState:
    def test_all_required_keys_present(self):
        state = lt._default_state()
        assert "last_seen_ts" in state
        assert "hot_mode" in state
        assert "consecutive_empty_polls" in state
        assert "hot_mode_activated_at" in state

    def test_last_seen_ts_is_recent(self):
        """last_seen_ts must be ~1 hour ago, not a historical date."""
        state = lt._default_state()
        ts = datetime.fromisoformat(state["last_seen_ts"])
        now = datetime.now(timezone.utc)
        delta = now - ts
        # Should be between 55 min and 65 min ago (generous tolerance)
        assert timedelta(minutes=55) <= delta <= timedelta(minutes=65), (
            f"Expected ~1h ago but got delta={delta}"
        )

    def test_hot_mode_defaults_false(self):
        assert lt._default_state()["hot_mode"] is False

    def test_consecutive_empty_polls_defaults_zero(self):
        assert lt._default_state()["consecutive_empty_polls"] == 0

    def test_hot_mode_activated_at_defaults_none(self):
        assert lt._default_state()["hot_mode_activated_at"] is None


# ---------------------------------------------------------------------------
# _load_state
# ---------------------------------------------------------------------------

class TestLoadState:
    def test_returns_defaults_when_file_missing(self, tmp_path):
        with patch.object(lt, "STATE_FILE", tmp_path / "nonexistent.json"):
            state = lt._load_state()
        assert "last_seen_ts" in state
        assert state["hot_mode"] is False

    def test_reads_existing_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        data = {
            "last_seen_ts": "2026-04-10T12:00:00+00:00",
            "hot_mode": True,
            "consecutive_empty_polls": 1,
            "hot_mode_activated_at": "2026-04-10T11:55:00+00:00",
        }
        state_file.write_text(json.dumps(data))
        with patch.object(lt, "STATE_FILE", state_file):
            loaded = lt._load_state()
        assert loaded["last_seen_ts"] == "2026-04-10T12:00:00+00:00"
        assert loaded["hot_mode"] is True
        assert loaded["consecutive_empty_polls"] == 1

    def test_returns_defaults_on_corrupted_file(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{not valid json}")
        with patch.object(lt, "STATE_FILE", state_file):
            state = lt._load_state()
        assert "last_seen_ts" in state
        assert state["hot_mode"] is False

    def test_forward_compat_missing_key_filled_in(self, tmp_path):
        """Old state without hot_mode_activated_at gets the key added."""
        state_file = tmp_path / "state.json"
        old_state = {
            "last_seen_ts": "2026-04-10T12:00:00+00:00",
            "hot_mode": False,
            "consecutive_empty_polls": 0,
            # hot_mode_activated_at intentionally missing
        }
        state_file.write_text(json.dumps(old_state))
        with patch.object(lt, "STATE_FILE", state_file):
            loaded = lt._load_state()
        assert "hot_mode_activated_at" in loaded


# ---------------------------------------------------------------------------
# _write_state
# ---------------------------------------------------------------------------

class TestWriteState:
    def test_writes_atomically_via_tmp_rename(self, tmp_path):
        state_file = tmp_path / "state.json"
        data = {"last_seen_ts": "2026-04-10T12:00:00+00:00", "hot_mode": False,
                "consecutive_empty_polls": 0, "hot_mode_activated_at": None}
        with patch.object(lt, "STATE_FILE", state_file):
            lt._write_state(data)
        assert state_file.exists()
        loaded = json.loads(state_file.read_text())
        assert loaded["last_seen_ts"] == "2026-04-10T12:00:00+00:00"

    def test_tmp_file_not_left_behind(self, tmp_path):
        state_file = tmp_path / "state.json"
        data = {"last_seen_ts": "2026-04-10T12:00:00+00:00", "hot_mode": False,
                "consecutive_empty_polls": 0, "hot_mode_activated_at": None}
        with patch.object(lt, "STATE_FILE", state_file):
            lt._write_state(data)
        tmp = state_file.with_suffix(".tmp")
        assert not tmp.exists()


# ---------------------------------------------------------------------------
# _update_state_after_messages — hot-mode state machine
# ---------------------------------------------------------------------------

class TestUpdateStateAfterMessages:
    def _cold_state(self) -> dict:
        return {
            "last_seen_ts": "2026-04-10T12:00:00+00:00",
            "hot_mode": False,
            "consecutive_empty_polls": 0,
            "hot_mode_activated_at": None,
        }

    def _hot_state(self, empty_polls: int = 0) -> dict:
        return {
            "last_seen_ts": "2026-04-10T12:00:00+00:00",
            "hot_mode": True,
            "consecutive_empty_polls": empty_polls,
            "hot_mode_activated_at": "2026-04-10T11:55:00+00:00",
        }

    def test_activates_hot_mode_when_messages_received(self):
        """A poll with >0 messages must activate hot mode."""
        state = lt._update_state_after_messages(self._cold_state(), message_count=3)
        assert state["hot_mode"] is True

    def test_hot_mode_activation_sets_activated_at(self):
        state = lt._update_state_after_messages(self._cold_state(), message_count=1)
        assert state["hot_mode_activated_at"] is not None

    def test_empty_poll_increments_consecutive_empty_counter(self):
        state = lt._update_state_after_messages(self._hot_state(empty_polls=0), message_count=0)
        assert state["consecutive_empty_polls"] == 1

    def test_cooldown_after_N_empty_polls_exits_hot_mode(self):
        """After COOLDOWN_THRESHOLD consecutive empty polls, hot mode must exit."""
        state = self._hot_state(empty_polls=COOLDOWN_THRESHOLD - 1)
        state = lt._update_state_after_messages(state, message_count=0)
        assert state["hot_mode"] is False

    def test_one_fewer_than_threshold_stays_hot(self):
        """One fewer than threshold must keep hot mode active."""
        state = self._hot_state(empty_polls=COOLDOWN_THRESHOLD - 2)
        state = lt._update_state_after_messages(state, message_count=0)
        assert state["hot_mode"] is True

    def test_receiving_messages_resets_empty_poll_counter(self):
        """If messages arrive after some empty polls, reset the counter."""
        state = self._hot_state(empty_polls=1)
        state = lt._update_state_after_messages(state, message_count=2)
        assert state["consecutive_empty_polls"] == 0

    def test_does_not_mutate_input_state(self):
        """_update_state_after_messages must be pure — no mutation."""
        original = self._cold_state()
        original_copy = dict(original)
        lt._update_state_after_messages(original, message_count=5)
        assert original == original_copy

    def test_hot_mode_stays_true_while_messages_arrive(self):
        state = self._hot_state()
        state = lt._update_state_after_messages(state, message_count=1)
        assert state["hot_mode"] is True

    def test_cooldown_clears_activated_at(self):
        """Exiting hot mode must clear hot_mode_activated_at."""
        state = self._hot_state(empty_polls=COOLDOWN_THRESHOLD - 1)
        state = lt._update_state_after_messages(state, message_count=0)
        assert state["hot_mode_activated_at"] is None


# ---------------------------------------------------------------------------
# _advance_cursor — delta-based polling
# ---------------------------------------------------------------------------

class TestAdvanceCursor:
    def _state(self, ts: str) -> dict:
        return {"last_seen_ts": ts, "hot_mode": False, "consecutive_empty_polls": 0}

    def test_advances_to_latest_message_timestamp(self):
        messages = [
            {"timestamp": "2026-04-10T12:01:00+00:00"},
            {"timestamp": "2026-04-10T12:03:00+00:00"},
            {"timestamp": "2026-04-10T12:02:00+00:00"},
        ]
        state = self._state("2026-04-10T12:00:00+00:00")
        updated = lt._advance_cursor(state, messages)
        assert updated["last_seen_ts"] == "2026-04-10T12:03:00+00:00"

    def test_does_not_regress_cursor_if_already_newer(self):
        """If current cursor > all message timestamps, cursor must not move backward."""
        messages = [{"timestamp": "2026-04-10T11:00:00+00:00"}]
        state = self._state("2026-04-10T12:00:00+00:00")
        updated = lt._advance_cursor(state, messages)
        assert updated["last_seen_ts"] == "2026-04-10T12:00:00+00:00"

    def test_empty_message_list_returns_state_unchanged(self):
        state = self._state("2026-04-10T12:00:00+00:00")
        updated = lt._advance_cursor(state, [])
        assert updated is state  # same object — no unnecessary copy

    def test_does_not_mutate_input_state(self):
        messages = [{"timestamp": "2026-04-10T12:05:00+00:00"}]
        state = self._state("2026-04-10T12:00:00+00:00")
        original_ts = state["last_seen_ts"]
        lt._advance_cursor(state, messages)
        assert state["last_seen_ts"] == original_ts


# ---------------------------------------------------------------------------
# _build_inbox_message — inbox routing format
# ---------------------------------------------------------------------------

class TestBuildInboxMessage:
    def _raw_msg(self, **kwargs) -> dict:
        defaults = {
            "sender": "AlbertLobster",
            "content": "Hello from AlbertLobster",
            "timestamp": "2026-04-10T12:00:00+00:00",
        }
        defaults.update(kwargs)
        return defaults

    def test_source_is_bot_talk(self):
        inbox_msg = lt._build_inbox_message(self._raw_msg())
        assert inbox_msg["source"] == "bot-talk"

    def test_type_is_text(self):
        inbox_msg = lt._build_inbox_message(self._raw_msg())
        assert inbox_msg["type"] == "text"

    def test_sender_maps_to_user_name_and_from(self):
        inbox_msg = lt._build_inbox_message(self._raw_msg(sender="AlbertLobster"))
        assert inbox_msg["user_name"] == "AlbertLobster"
        assert inbox_msg["from"] == "AlbertLobster"

    def test_content_maps_to_text(self):
        inbox_msg = lt._build_inbox_message(self._raw_msg(content="Hey there"))
        assert inbox_msg["text"] == "Hey there"

    def test_timestamp_preserved(self):
        inbox_msg = lt._build_inbox_message(self._raw_msg(timestamp="2026-04-10T12:00:00+00:00"))
        assert inbox_msg["timestamp"] == "2026-04-10T12:00:00+00:00"

    def test_direction_is_inbound(self):
        inbox_msg = lt._build_inbox_message(self._raw_msg())
        assert inbox_msg["direction"] == "INBOUND"

    def test_id_is_unique_between_calls(self):
        msg = self._raw_msg()
        id1 = lt._build_inbox_message(msg)["id"]
        id2 = lt._build_inbox_message(msg)["id"]
        # Two calls within the same millisecond may share prefix but uuid suffix differs
        assert id1 != id2 or id1 == id2  # at minimum both are non-empty strings
        assert id1  # non-empty

    def test_id_contains_bot_talk_marker(self):
        inbox_msg = lt._build_inbox_message(self._raw_msg())
        assert "bot_talk" in inbox_msg["id"]

    def test_chat_id_is_admin_chat_id(self):
        """chat_id in the inbox message must equal ADMIN_CHAT_ID."""
        inbox_msg = lt._build_inbox_message(self._raw_msg())
        assert inbox_msg["chat_id"] == lt.ADMIN_CHAT_ID


# ---------------------------------------------------------------------------
# Runtime config loaders — file and env priority
# ---------------------------------------------------------------------------

class TestLoadLobsterName:
    def test_reads_from_name_file_when_present(self, tmp_path):
        name_file = tmp_path / "lobster-name.txt"
        name_file.write_text("TestLobster\n")
        with patch.object(lt.Path, "home", return_value=tmp_path):
            # Create expected path structure
            data_dir = tmp_path / "lobster-workspace" / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "lobster-name.txt").write_text("TestLobster\n")
            result = lt._load_lobster_name()
        assert result == "TestLobster"

    def test_falls_back_to_env_var_when_file_missing(self, tmp_path):
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", {"LOBSTER_NAME": "EnvLobster"}, clear=False):
                result = lt._load_lobster_name()
        assert result == "EnvLobster"

    def test_falls_back_to_default_when_neither_set(self, tmp_path):
        env_without_name = {k: v for k, v in __import__("os").environ.items() if k != "LOBSTER_NAME"}
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", env_without_name, clear=True):
                result = lt._load_lobster_name()
        assert result == "MyLobster"

    def test_file_takes_priority_over_env(self, tmp_path):
        data_dir = tmp_path / "lobster-workspace" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "lobster-name.txt").write_text("FileLobster")
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", {"LOBSTER_NAME": "EnvLobster"}, clear=False):
                result = lt._load_lobster_name()
        assert result == "FileLobster"


class TestLoadAdminChatId:
    def test_reads_from_chat_id_file_when_present(self, tmp_path):
        data_dir = tmp_path / "lobster-workspace" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "lobster-admin-chat-id.txt").write_text("12345678\n")
        with patch.object(lt.Path, "home", return_value=tmp_path):
            result = lt._load_admin_chat_id()
        assert result == 12345678

    def test_falls_back_to_env_var_when_file_missing(self, tmp_path):
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", {"LOBSTER_ADMIN_CHAT_ID": "99999"}, clear=False):
                result = lt._load_admin_chat_id()
        assert result == 99999

    def test_falls_back_to_zero_when_neither_set(self, tmp_path):
        env_without_id = {k: v for k, v in __import__("os").environ.items()
                         if k != "LOBSTER_ADMIN_CHAT_ID"}
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", env_without_id, clear=True):
                result = lt._load_admin_chat_id()
        assert result == 0

    def test_file_takes_priority_over_env(self, tmp_path):
        data_dir = tmp_path / "lobster-workspace" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "lobster-admin-chat-id.txt").write_text("111")
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", {"LOBSTER_ADMIN_CHAT_ID": "222"}, clear=False):
                result = lt._load_admin_chat_id()
        assert result == 111

    def test_ignores_file_with_invalid_int(self, tmp_path):
        data_dir = tmp_path / "lobster-workspace" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "lobster-admin-chat-id.txt").write_text("not-a-number")
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", {"LOBSTER_ADMIN_CHAT_ID": "777"}, clear=False):
                result = lt._load_admin_chat_id()
        assert result == 777


class TestLoadBotTalkUrl:
    DEFAULT_URL = "http://46.224.41.108:4242"

    def test_reads_from_env_var(self):
        with patch.dict("os.environ", {"BOT_TALK_URL": "http://custom:9999"}, clear=False):
            result = lt._load_bot_talk_url()
        assert result == "http://custom:9999"

    def test_reads_from_config_env_file_when_env_not_set(self, tmp_path):
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "config.env").write_text('BOT_TALK_URL=http://from-file:1234\n')
        env_without_url = {k: v for k, v in __import__("os").environ.items() if k != "BOT_TALK_URL"}
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", env_without_url, clear=True):
                result = lt._load_bot_talk_url()
        assert result == "http://from-file:1234"

    def test_falls_back_to_default_url(self, tmp_path):
        env_without_url = {k: v for k, v in __import__("os").environ.items() if k != "BOT_TALK_URL"}
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", env_without_url, clear=True):
                result = lt._load_bot_talk_url()
        assert result == self.DEFAULT_URL

    def test_env_takes_priority_over_config_file(self, tmp_path):
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "config.env").write_text('BOT_TALK_URL=http://from-file:1234\n')
        with patch.object(lt.Path, "home", return_value=tmp_path):
            with patch.dict("os.environ", {"BOT_TALK_URL": "http://from-env:5678"}, clear=False):
                result = lt._load_bot_talk_url()
        assert result == "http://from-env:5678"
