"""Tests for Telegram-native onboarding additions to onboarding.py.

Tests cover:
- OnboardingState dataclass (pure)
- get/save/clear onboarding state (file I/O with tmp_path)
- list_workspace_channels (mocked Slack SDK)
- delete_telegram_message (mocked urllib)
- build_channels_config (pure)
- write_channels_config (file I/O with tmp_path)
- restart_ingress_service (mocked subprocess)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

import sys
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent),
)

from src.onboarding import (
    STEP_MODE_SELECT,
    STEP_BOT_TOKEN,
    STEP_CONFIRM,
    STEP_DONE,
    STEP_CANCELLED,
    CHANNEL_MODE_MONITOR,
    CHANNEL_MODE_MENTIONS,
    CHANNEL_MODE_FULL,
    CHANNEL_MODES,
    OnboardingState,
    get_onboarding_state,
    save_onboarding_state,
    clear_onboarding_state,
    list_workspace_channels,
    delete_telegram_message,
    build_channels_config,
    write_channels_config,
    restart_ingress_service,
)


# ============================================================================
# OnboardingState — pure dataclass
# ============================================================================

class TestOnboardingState:
    def test_defaults(self):
        state = OnboardingState(chat_id="123")
        assert state.chat_id == "123"
        assert state.step == STEP_MODE_SELECT
        assert state.mode == ""
        assert state.bot_token == ""
        assert state.app_token == ""
        assert state.person_token == ""
        assert state.workspace_name == ""
        assert state.available_channels == []
        assert state.selected_channels == []
        assert state.channel_modes == {}
        assert state.last_token_message_id is None

    def test_to_dict_round_trip(self):
        state = OnboardingState(
            chat_id="456",
            step=STEP_BOT_TOKEN,
            mode="bot",
            workspace_name="TestCorp",
            selected_channels=["C123", "C456"],
            channel_modes={"C123": CHANNEL_MODE_MONITOR},
            last_token_message_id=9001,
        )
        d = state.to_dict()
        restored = OnboardingState.from_dict(d)
        assert restored.chat_id == "456"
        assert restored.step == STEP_BOT_TOKEN
        assert restored.mode == "bot"
        assert restored.workspace_name == "TestCorp"
        assert restored.selected_channels == ["C123", "C456"]
        assert restored.channel_modes == {"C123": CHANNEL_MODE_MONITOR}
        assert restored.last_token_message_id == 9001

    def test_from_dict_missing_fields_use_defaults(self):
        state = OnboardingState.from_dict({"chat_id": "789"})
        assert state.step == STEP_MODE_SELECT
        assert state.mode == ""
        assert state.available_channels == []

    def test_mutable_fields_are_independent(self):
        """Separate instances should not share mutable defaults."""
        a = OnboardingState(chat_id="1")
        b = OnboardingState(chat_id="2")
        a.selected_channels.append("C001")
        assert b.selected_channels == []


# ============================================================================
# Onboarding state file I/O
# ============================================================================

class TestGetSaveClearOnboardingState:
    def test_save_and_get(self, tmp_path):
        state = OnboardingState(chat_id="100", step=STEP_CONFIRM, mode="bot")
        save_onboarding_state(state, state_dir=tmp_path)

        loaded = get_onboarding_state("100", state_dir=tmp_path)
        assert loaded.step == STEP_CONFIRM
        assert loaded.mode == "bot"

    def test_get_missing_returns_fresh(self, tmp_path):
        state = get_onboarding_state("nonexistent", state_dir=tmp_path)
        assert state.step == STEP_MODE_SELECT
        assert state.chat_id == "nonexistent"

    def test_clear_removes_file(self, tmp_path):
        state = OnboardingState(chat_id="200", step=STEP_DONE)
        save_onboarding_state(state, state_dir=tmp_path)
        assert (tmp_path / "onboarding_200.json").exists()

        clear_onboarding_state("200", state_dir=tmp_path)
        assert not (tmp_path / "onboarding_200.json").exists()

    def test_clear_nonexistent_is_safe(self, tmp_path):
        # Should not raise
        clear_onboarding_state("missing", state_dir=tmp_path)

    def test_save_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        state = OnboardingState(chat_id="300")
        save_onboarding_state(state, state_dir=nested)
        assert (nested / "onboarding_300.json").exists()

    def test_get_corrupted_file_returns_fresh(self, tmp_path):
        path = tmp_path / "onboarding_bad.json"
        path.write_text("not json!!!")
        state = get_onboarding_state("bad", state_dir=tmp_path)
        assert state.step == STEP_MODE_SELECT

    def test_state_file_is_valid_json(self, tmp_path):
        state = OnboardingState(
            chat_id="400",
            available_channels=[{"id": "C1", "name": "general"}],
        )
        save_onboarding_state(state, state_dir=tmp_path)
        data = json.loads((tmp_path / "onboarding_400.json").read_text())
        assert data["chat_id"] == "400"
        assert data["available_channels"] == [{"id": "C1", "name": "general"}]


# ============================================================================
# list_workspace_channels — mocked Slack SDK
# ============================================================================

class TestListWorkspaceChannels:
    def _make_slack_mock(self, pages: list[list[dict]]) -> dict:
        """Build a fake slack_sdk with paginated conversations.list."""
        cursors = [str(i) for i in range(1, len(pages))] + [""]

        def fake_conversations_list(**kwargs):
            page_idx = 0
            cursor_in = kwargs.get("cursor")
            if cursor_in:
                try:
                    page_idx = int(cursor_in)
                except ValueError:
                    page_idx = 0
            channels_on_page = pages[page_idx]
            next_cursor = cursors[page_idx]
            return {
                "ok": True,
                "channels": channels_on_page,
                "response_metadata": {"next_cursor": next_cursor},
            }

        mock_client = MagicMock()
        mock_client.conversations_list.side_effect = fake_conversations_list

        mock_sdk = MagicMock()
        mock_sdk.WebClient.return_value = mock_client

        mock_errors = MagicMock()
        mock_errors.SlackApiError = type("SlackApiError", (Exception,), {})

        return {"slack_sdk": mock_sdk, "slack_sdk.errors": mock_errors}

    def test_single_page(self):
        channels_data = [
            {"id": "C001", "name": "general", "is_member": True, "is_private": False},
            {"id": "C002", "name": "dev", "is_member": False, "is_private": False},
        ]
        modules = self._make_slack_mock([channels_data])
        with patch.dict("sys.modules", modules):
            result = list_workspace_channels("xoxb-test-123-456")
        assert len(result) == 2
        assert result[0]["id"] == "C001"
        assert result[0]["name"] == "general"
        assert result[0]["is_member"] is True
        assert result[1]["id"] == "C002"

    def test_pagination(self):
        page1 = [{"id": "C001", "name": "a", "is_member": True, "is_private": False}]
        page2 = [{"id": "C002", "name": "b", "is_member": False, "is_private": True}]
        modules = self._make_slack_mock([page1, page2])
        with patch.dict("sys.modules", modules):
            result = list_workspace_channels("xoxb-test-123-456")
        assert len(result) == 2
        assert {c["id"] for c in result} == {"C001", "C002"}

    def test_api_error_returns_empty(self):
        mock_client = MagicMock()
        SlackApiError = type(
            "SlackApiError",
            (Exception,),
            {"response": {"error": "not_authed"}},
        )
        mock_client.conversations_list.side_effect = SlackApiError("not_authed")

        mock_sdk = MagicMock()
        mock_sdk.WebClient.return_value = mock_client

        mock_errors = MagicMock()
        mock_errors.SlackApiError = SlackApiError

        with patch.dict("sys.modules", {"slack_sdk": mock_sdk, "slack_sdk.errors": mock_errors}):
            result = list_workspace_channels("xoxb-test-123-456")
        assert result == []

    def test_import_error_returns_empty(self):
        with patch.dict("sys.modules", {"slack_sdk": None}):
            result = list_workspace_channels("xoxb-test-123-456")
        assert result == []

    def test_injectable_fn(self):
        """Test dependency injection path."""
        def fake_fn(token):
            return [{"id": "C999", "name": "injected", "is_member": True, "is_private": False}]

        result = list_workspace_channels("xoxb-any", _conversations_list_fn=fake_fn)
        assert len(result) == 1
        assert result[0]["id"] == "C999"

    def test_ok_false_returns_empty(self):
        mock_client = MagicMock()
        mock_client.conversations_list.return_value = {
            "ok": False,
            "error": "missing_scope",
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }
        mock_sdk = MagicMock()
        mock_sdk.WebClient.return_value = mock_client
        mock_errors = MagicMock()
        mock_errors.SlackApiError = type("SlackApiError", (Exception,), {})

        with patch.dict("sys.modules", {"slack_sdk": mock_sdk, "slack_sdk.errors": mock_errors}):
            result = list_workspace_channels("xoxb-test-123-456")
        assert result == []


# ============================================================================
# delete_telegram_message — mocked urllib
# ============================================================================

class TestDeleteTelegramMessage:
    def test_injectable_fn_success(self):
        calls = []
        def fake_delete(chat_id, message_id):
            calls.append((chat_id, message_id))
            return True

        result = delete_telegram_message(123, 456, _delete_fn=fake_delete)
        assert result is True
        assert calls == [(123, 456)]

    def test_injectable_fn_failure(self):
        def fake_delete(chat_id, message_id):
            return False

        result = delete_telegram_message(123, 456, _delete_fn=fake_delete)
        assert result is False

    def test_no_token_returns_false(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove TELEGRAM_BOT_TOKEN if present
            import os
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            result = delete_telegram_message(123, 456, bot_token=None)
        assert result is False

    def test_network_error_returns_false(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            result = delete_telegram_message(
                123, 456,
                bot_token="fake_bot_token",
            )
        assert result is False

    def test_success_via_urllib(self):
        import io
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"ok": True}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = delete_telegram_message(
                999, 888,
                bot_token="fake_bot_token",
            )
        assert result is True


# ============================================================================
# build_channels_config — pure
# ============================================================================

class TestBuildChannelsConfig:
    def test_basic(self):
        selections = [
            {"id": "C001", "name": "general", "mode": CHANNEL_MODE_MONITOR},
            {"id": "C002", "name": "dev", "mode": CHANNEL_MODE_MENTIONS},
        ]
        config = build_channels_config(selections)
        assert "channels" in config
        assert "C001" in config["channels"]
        assert config["channels"]["C001"]["mode"] == CHANNEL_MODE_MONITOR
        assert config["channels"]["C001"]["name"] == "general"
        assert config["channels"]["C002"]["mode"] == CHANNEL_MODE_MENTIONS

    def test_defaults_log_flags(self):
        selections = [{"id": "C001", "name": "a", "mode": CHANNEL_MODE_MONITOR}]
        config = build_channels_config(selections)
        ch = config["channels"]["C001"]
        assert ch["log_messages"] is True
        assert ch["log_reactions"] is True
        assert ch["log_edits"] is True
        assert ch["log_deletes"] is False
        assert ch["log_files"] is True

    def test_invalid_mode_falls_back_to_monitor(self):
        selections = [{"id": "C001", "name": "a", "mode": "garbage"}]
        config = build_channels_config(selections)
        assert config["channels"]["C001"]["mode"] == CHANNEL_MODE_MONITOR

    def test_empty_id_skipped(self):
        selections = [
            {"id": "", "name": "empty-id", "mode": CHANNEL_MODE_MONITOR},
            {"id": "C001", "name": "real", "mode": CHANNEL_MODE_FULL},
        ]
        config = build_channels_config(selections)
        assert "" not in config["channels"]
        assert "C001" in config["channels"]

    def test_empty_selections(self):
        config = build_channels_config([])
        assert config == {"channels": {}}

    def test_full_mode(self):
        selections = [{"id": "C001", "name": "a", "mode": CHANNEL_MODE_FULL}]
        config = build_channels_config(selections)
        assert config["channels"]["C001"]["mode"] == CHANNEL_MODE_FULL

    def test_all_modes_valid(self):
        for mode in CHANNEL_MODES:
            selections = [{"id": "C001", "name": "a", "mode": mode}]
            config = build_channels_config(selections)
            assert config["channels"]["C001"]["mode"] == mode


# ============================================================================
# write_channels_config — file I/O
# ============================================================================

class TestWriteChannelsConfig:
    def test_writes_valid_yaml(self, tmp_path):
        config_path = tmp_path / "channels.yaml"
        selections = [
            {"id": "C001", "name": "general", "mode": CHANNEL_MODE_MONITOR},
        ]
        write_channels_config(selections, config_path=config_path)
        assert config_path.exists()
        data = yaml.safe_load(config_path.read_text())
        assert "channels" in data
        assert "C001" in data["channels"]

    def test_creates_parent_dirs(self, tmp_path):
        config_path = tmp_path / "deep" / "nested" / "channels.yaml"
        write_channels_config([], config_path=config_path)
        assert config_path.exists()

    def test_overwrites_existing(self, tmp_path):
        config_path = tmp_path / "channels.yaml"
        # First write
        write_channels_config(
            [{"id": "C001", "name": "old", "mode": CHANNEL_MODE_MONITOR}],
            config_path=config_path,
        )
        # Overwrite
        write_channels_config(
            [{"id": "C002", "name": "new", "mode": CHANNEL_MODE_FULL}],
            config_path=config_path,
        )
        data = yaml.safe_load(config_path.read_text())
        assert "C001" not in data["channels"]
        assert "C002" in data["channels"]


# ============================================================================
# restart_ingress_service — mocked subprocess
# ============================================================================

class TestRestartIngressService:
    def test_injectable_success(self):
        def fake_run(service_name):
            return True, f"Service {service_name} restarted."

        ok, msg = restart_ingress_service(_run_fn=fake_run)
        assert ok is True
        assert "restarted" in msg

    def test_injectable_failure(self):
        def fake_run(service_name):
            return False, "systemctl failed"

        ok, msg = restart_ingress_service(_run_fn=fake_run)
        assert ok is False
        assert "failed" in msg

    def test_systemctl_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ok, msg = restart_ingress_service(service_name="my-service")

        assert ok is True
        assert "my-service" in msg
        mock_run.assert_called_once_with(
            ["systemctl", "restart", "my-service"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_systemctl_nonzero_exit(self):
        mock_result = MagicMock()
        mock_result.returncode = 5
        mock_result.stderr = "Unit not found."

        with patch("subprocess.run", return_value=mock_result):
            ok, msg = restart_ingress_service()

        assert ok is False
        assert "5" in msg

    def test_systemctl_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ok, msg = restart_ingress_service()

        assert ok is False
        assert "not found" in msg.lower()

    def test_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=30)):
            ok, msg = restart_ingress_service()

        assert ok is False
        assert "timed out" in msg.lower()
