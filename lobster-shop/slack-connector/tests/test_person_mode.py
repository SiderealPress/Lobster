"""Tests for person-mode behavior in ingress_logger and onboarding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingress_logger import (
    SlackIngressLogger,
    is_self_message,
    should_log_in_mode,
    should_route_to_llm_for_mode,
)
from src.onboarding import (
    bot_instructions,
    person_instructions,
    instructions_for_mode,
    _read_config_env,
    _write_config_env,
)


# ---------------------------------------------------------------------------
# Pure function tests — is_self_message
# ---------------------------------------------------------------------------


class TestIsSelfMessage:
    def test_matching_user_is_self(self) -> None:
        event = {"user": "U_LOBSTER", "text": "hello"}
        assert is_self_message(event, "U_LOBSTER") is True

    def test_different_user_is_not_self(self) -> None:
        event = {"user": "U_OTHER", "text": "hello"}
        assert is_self_message(event, "U_LOBSTER") is False

    def test_empty_own_id_never_matches(self) -> None:
        event = {"user": "U_ANY", "text": "hello"}
        assert is_self_message(event, "") is False

    def test_missing_user_field(self) -> None:
        event = {"text": "hello"}
        assert is_self_message(event, "U_LOBSTER") is False


# ---------------------------------------------------------------------------
# Pure function tests — should_log_in_mode
# ---------------------------------------------------------------------------


class TestShouldLogInMode:
    def test_bot_mode_always_logs(self) -> None:
        assert should_log_in_mode(
            account_type="bot", is_mention=False, is_dm=False, is_self=True
        ) is True

    def test_person_mode_skips_self(self) -> None:
        assert should_log_in_mode(
            account_type="person", is_mention=False, is_dm=False, is_self=True
        ) is False

    def test_person_mode_logs_others(self) -> None:
        assert should_log_in_mode(
            account_type="person", is_mention=False, is_dm=False, is_self=False
        ) is True

    def test_person_mode_logs_mentions(self) -> None:
        assert should_log_in_mode(
            account_type="person", is_mention=True, is_dm=False, is_self=False
        ) is True


# ---------------------------------------------------------------------------
# Pure function tests — should_route_to_llm_for_mode
# ---------------------------------------------------------------------------


class TestShouldRouteToLlmForMode:
    # Person mode: routes all messages in respond channels
    def test_person_respond_routes_all(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="person",
            is_mention=False,
            is_dm=False,
            is_self=False,
            channel_mode="respond",
        ) is True

    # Bot mode: respond only routes mentions/DMs
    def test_bot_respond_requires_mention(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="bot",
            is_mention=False,
            is_dm=False,
            is_self=False,
            channel_mode="respond",
        ) is False

    def test_bot_respond_routes_mention(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="bot",
            is_mention=True,
            is_dm=False,
            is_self=False,
            channel_mode="respond",
        ) is True

    def test_bot_respond_routes_dm(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="bot",
            is_mention=False,
            is_dm=True,
            is_self=False,
            channel_mode="respond",
        ) is True

    # Self-messages never routed
    def test_self_never_routed(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="person",
            is_mention=False,
            is_dm=False,
            is_self=True,
            channel_mode="full",
        ) is False

    # Monitor/ignore never route
    def test_monitor_never_routes(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="person",
            is_mention=True,
            is_dm=False,
            is_self=False,
            channel_mode="monitor",
        ) is False

    def test_ignore_never_routes(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="bot",
            is_mention=True,
            is_dm=False,
            is_self=False,
            channel_mode="ignore",
        ) is False

    # Full always routes (unless self)
    def test_full_always_routes(self) -> None:
        assert should_route_to_llm_for_mode(
            account_type="bot",
            is_mention=False,
            is_dm=False,
            is_self=False,
            channel_mode="full",
        ) is True


# ---------------------------------------------------------------------------
# SlackIngressLogger person-mode integration tests
# ---------------------------------------------------------------------------


class TestIngressLoggerPersonMode:
    @pytest.fixture
    def logger_person(self, tmp_path: Path) -> SlackIngressLogger:
        lgr = SlackIngressLogger(
            log_root=tmp_path / "logs",
            dedup_db_path=tmp_path / "state" / "dedup.db",
            account_type="person",
            own_user_id="U_LOBSTER",
        )
        yield lgr
        lgr.close()

    @pytest.fixture
    def logger_bot(self, tmp_path: Path) -> SlackIngressLogger:
        lgr = SlackIngressLogger(
            log_root=tmp_path / "logs",
            dedup_db_path=tmp_path / "state" / "dedup.db",
            account_type="bot",
            own_user_id="U_LOBSTER",
        )
        yield lgr
        lgr.close()

    def test_person_mode_skips_self_message(
        self, logger_person: SlackIngressLogger, tmp_path: Path
    ) -> None:
        event = {"ts": "100.001", "user": "U_LOBSTER", "text": "my own message"}
        logger_person.log_message(event=event, channel_id="C01")

        log_files = list((tmp_path / "logs").rglob("*.jsonl"))
        assert len(log_files) == 0

    def test_person_mode_logs_other_messages(
        self, logger_person: SlackIngressLogger, tmp_path: Path
    ) -> None:
        event = {"ts": "100.002", "user": "U_OTHER", "text": "hello from someone"}
        logger_person.log_message(event=event, channel_id="C01")

        log_files = list((tmp_path / "logs").rglob("*.jsonl"))
        assert len(log_files) == 1
        record = json.loads(log_files[0].read_text().strip())
        assert record["text"] == "hello from someone"

    def test_bot_mode_logs_all_including_self(
        self, logger_bot: SlackIngressLogger, tmp_path: Path
    ) -> None:
        event = {"ts": "100.003", "user": "U_LOBSTER", "text": "bot's own message"}
        logger_bot.log_message(event=event, channel_id="C01")

        log_files = list((tmp_path / "logs").rglob("*.jsonl"))
        assert len(log_files) == 1


# ---------------------------------------------------------------------------
# Onboarding instruction tests (pure)
# ---------------------------------------------------------------------------


class TestOnboardingInstructions:
    def test_bot_instructions_contain_xoxb(self) -> None:
        text = bot_instructions()
        assert "xoxb-" in text
        assert "Bot Account Setup" in text

    def test_person_instructions_contain_xoxp(self) -> None:
        text = person_instructions()
        assert "xoxp-" in text
        assert "Person Account Setup" in text

    def test_person_instructions_warn_legacy(self) -> None:
        text = person_instructions()
        assert "DEPRECATED" in text or "deprecated" in text

    def test_person_instructions_list_scopes(self) -> None:
        text = person_instructions()
        assert "channels:history" in text
        assert "channels:write" in text

    def test_instructions_for_mode_dispatches(self) -> None:
        assert "Bot Account Setup" in instructions_for_mode("bot")
        assert "Person Account Setup" in instructions_for_mode("person")


# ---------------------------------------------------------------------------
# Config env read/write tests (side-effect boundary)
# ---------------------------------------------------------------------------


class TestConfigEnv:
    def test_read_missing_file(self, tmp_path: Path) -> None:
        result = _read_config_env(tmp_path / "nonexistent.env")
        assert result == {}

    def test_read_existing_file(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.env"
        config_path.write_text("KEY1=value1\nKEY2=value2\n")
        result = _read_config_env(config_path)
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_read_skips_comments(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.env"
        config_path.write_text("# comment\nKEY=val\n")
        result = _read_config_env(config_path)
        assert result == {"KEY": "val"}

    def test_read_strips_quotes(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.env"
        config_path.write_text('KEY="quoted"\n')
        result = _read_config_env(config_path)
        assert result == {"KEY": "quoted"}

    def test_write_creates_new_file(self, tmp_path: Path) -> None:
        config_path = tmp_path / "subdir" / "config.env"
        _write_config_env({"KEY1": "val1"}, config_path)
        assert config_path.exists()
        result = _read_config_env(config_path)
        assert result["KEY1"] == "val1"

    def test_write_updates_existing_key(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.env"
        config_path.write_text("KEY=old\nOTHER=keep\n")
        _write_config_env({"KEY": "new"}, config_path)
        result = _read_config_env(config_path)
        assert result["KEY"] == "new"
        assert result["OTHER"] == "keep"

    def test_write_preserves_comments(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.env"
        config_path.write_text("# important comment\nKEY=val\n")
        _write_config_env({"KEY": "updated"}, config_path)
        content = config_path.read_text()
        assert "# important comment" in content
