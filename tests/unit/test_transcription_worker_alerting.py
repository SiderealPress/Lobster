"""
Tests for transcription worker dead-letter alerting.

Covers the notify_dispatcher_dead_letter function and its integration
with move_to_dead_letter: verifies that a subagent_observation JSON
is written to the inbox dir with the correct shape when a message is
dead-lettered.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg_data(msg_id: str = "test-msg-123") -> dict:
    return {"id": msg_id, "type": "voice", "chat_id": 99999}


def _load_inbox_observations(inbox_dir: Path) -> list[dict]:
    """Return all subagent_observation JSON files from the inbox dir."""
    observations = []
    for f in inbox_dir.glob("*.json"):
        data = json.loads(f.read_text())
        if data.get("type") == "subagent_observation":
            observations.append(data)
    return observations


# ---------------------------------------------------------------------------
# _read_admin_chat_id
# ---------------------------------------------------------------------------

class TestReadAdminChatId:
    """Pure unit tests for _read_admin_chat_id config parsing."""

    def test_reads_first_allowed_user(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("TELEGRAM_ALLOWED_USERS=12345678,99999\n")

        with patch("src.transcription.worker._CONFIG_DIR", config_dir):
            from src.transcription.worker import _read_admin_chat_id
            result = _read_admin_chat_id()
        assert result == 12345678

    def test_returns_none_when_config_missing(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # No config.env written

        with patch("src.transcription.worker._CONFIG_DIR", config_dir):
            from src.transcription.worker import _read_admin_chat_id
            result = _read_admin_chat_id()
        assert result is None

    def test_returns_none_when_value_non_numeric(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("TELEGRAM_ALLOWED_USERS=notanumber\n")

        with patch("src.transcription.worker._CONFIG_DIR", config_dir):
            from src.transcription.worker import _read_admin_chat_id
            result = _read_admin_chat_id()
        assert result is None

    def test_handles_quoted_value(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text('TELEGRAM_ALLOWED_USERS="55556666"\n')

        with patch("src.transcription.worker._CONFIG_DIR", config_dir):
            from src.transcription.worker import _read_admin_chat_id
            result = _read_admin_chat_id()
        assert result == 55556666


# ---------------------------------------------------------------------------
# notify_dispatcher_dead_letter
# ---------------------------------------------------------------------------

class TestNotifyDispatcherDeadLetter:
    """Tests for notify_dispatcher_dead_letter."""

    def test_writes_observation_to_inbox(self, tmp_path):
        """A subagent_observation JSON file is dropped in the inbox dir."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("TELEGRAM_ALLOWED_USERS=11112222\n")

        msg_data = _make_msg_data()
        reason = "All 3 whisper attempts failed"

        with patch("src.transcription.worker.INBOX_DIR", inbox_dir), \
             patch("src.transcription.worker._CONFIG_DIR", config_dir), \
             patch.dict(os.environ, {}, clear=False):
            # Remove LOBSTER_ADMIN_CHAT_ID so it falls back to config.env
            os.environ.pop("LOBSTER_ADMIN_CHAT_ID", None)
            from src.transcription.worker import notify_dispatcher_dead_letter
            notify_dispatcher_dead_letter(msg_data, reason)

        observations = _load_inbox_observations(inbox_dir)
        assert len(observations) == 1
        obs = observations[0]
        assert obs["type"] == "subagent_observation"
        assert obs["category"] == "system_error"
        assert obs["chat_id"] == 11112222
        assert reason in obs["text"]
        assert obs["task_id"].startswith("transcription-dead-letter-")

    def test_uses_env_var_over_config(self, tmp_path):
        """LOBSTER_ADMIN_CHAT_ID env var overrides config.env."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("TELEGRAM_ALLOWED_USERS=11112222\n")

        msg_data = _make_msg_data()
        reason = "ffmpeg conversion failed"

        with patch("src.transcription.worker.INBOX_DIR", inbox_dir), \
             patch("src.transcription.worker._CONFIG_DIR", config_dir), \
             patch.dict(os.environ, {"LOBSTER_ADMIN_CHAT_ID": "99998888"}):
            from src.transcription.worker import notify_dispatcher_dead_letter
            notify_dispatcher_dead_letter(msg_data, reason)

        observations = _load_inbox_observations(inbox_dir)
        assert len(observations) == 1
        assert observations[0]["chat_id"] == 99998888

    def test_silent_when_no_chat_id(self, tmp_path):
        """When no admin chat_id is resolvable, no file is written and no exception raised."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # No config.env — _read_admin_chat_id returns None

        msg_data = _make_msg_data()

        with patch("src.transcription.worker.INBOX_DIR", inbox_dir), \
             patch("src.transcription.worker._CONFIG_DIR", config_dir), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOBSTER_ADMIN_CHAT_ID", None)
            from src.transcription.worker import notify_dispatcher_dead_letter
            notify_dispatcher_dead_letter(msg_data, "some reason")  # must not raise

        assert _load_inbox_observations(inbox_dir) == []

    def test_observation_includes_pending_file_name(self, tmp_path):
        """When _pending_file is set in msg_data, the alert text includes the filename."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("TELEGRAM_ALLOWED_USERS=11112222\n")

        msg_data = _make_msg_data()
        msg_data["_pending_file"] = "1700000000000_abc123.json"
        reason = "Audio file not found"

        with patch("src.transcription.worker.INBOX_DIR", inbox_dir), \
             patch("src.transcription.worker._CONFIG_DIR", config_dir), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOBSTER_ADMIN_CHAT_ID", None)
            from src.transcription.worker import notify_dispatcher_dead_letter
            notify_dispatcher_dead_letter(msg_data, reason)

        observations = _load_inbox_observations(inbox_dir)
        assert len(observations) == 1
        assert "1700000000000_abc123.json" in observations[0]["text"]

    def test_observation_json_is_valid(self, tmp_path):
        """The written observation has all required fields for the dispatcher."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("TELEGRAM_ALLOWED_USERS=11112222\n")

        msg_data = _make_msg_data("abc-def-789")
        reason = "Whisper timed out after 120s"

        with patch("src.transcription.worker.INBOX_DIR", inbox_dir), \
             patch("src.transcription.worker._CONFIG_DIR", config_dir), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOBSTER_ADMIN_CHAT_ID", None)
            from src.transcription.worker import notify_dispatcher_dead_letter
            notify_dispatcher_dead_letter(msg_data, reason)

        obs = _load_inbox_observations(inbox_dir)[0]
        # All dispatcher-required fields must be present
        required_fields = {"id", "type", "source", "chat_id", "text", "category", "timestamp"}
        assert required_fields.issubset(obs.keys()), f"Missing fields: {required_fields - obs.keys()}"
        assert obs["source"] == "telegram"


# ---------------------------------------------------------------------------
# move_to_dead_letter integration
# ---------------------------------------------------------------------------

class TestMoveToDeadLetterIntegration:
    """Integration tests: move_to_dead_letter calls notify_dispatcher_dead_letter."""

    def test_move_to_dead_letter_queues_alert(self, tmp_path):
        """move_to_dead_letter writes to dead-letter AND drops an observation in inbox."""
        pending_dir = tmp_path / "pending-transcription"
        pending_dir.mkdir()
        dead_letter_dir = tmp_path / "dead-letter"
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").write_text("TELEGRAM_ALLOWED_USERS=77778888\n")

        # Create a fake pending file
        pending_file = pending_dir / "1234567890_voicemsg.json"
        msg_data = {"id": "voice-msg-001", "type": "voice"}
        pending_file.write_text(json.dumps(msg_data))

        with patch("src.transcription.worker.DEAD_LETTER_DIR", dead_letter_dir), \
             patch("src.transcription.worker.INBOX_DIR", inbox_dir), \
             patch("src.transcription.worker._CONFIG_DIR", config_dir), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOBSTER_ADMIN_CHAT_ID", None)
            from src.transcription.worker import move_to_dead_letter
            move_to_dead_letter(pending_file, msg_data, "All retries exhausted")

        # File should be in dead-letter
        assert (dead_letter_dir / pending_file.name).exists()
        # Observation should be in inbox
        observations = _load_inbox_observations(inbox_dir)
        assert len(observations) == 1
        obs = observations[0]
        assert obs["category"] == "system_error"
        assert obs["chat_id"] == 77778888
        assert "All retries exhausted" in obs["text"]
        assert "1234567890_voicemsg.json" in obs["text"]
