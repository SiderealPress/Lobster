"""Tests for channel_config module — pure functions and ChannelConfig class."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.channel_config import (
    ChannelConfig,
    get_channel_triggers,
    parse_config,
    resolve_channel,
    should_route_to_llm,
    VALID_MODES,
    _BUILTIN_DEFAULTS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config() -> dict:
    return {
        "defaults": {
            "mode": "monitor",
            "log_raw": True,
        },
        "channels": [
            {"id": "C001", "name": "general", "mode": "monitor"},
            {"id": "C002", "name": "ops", "mode": "full", "llm_routing": True},
            {"id": "C003", "name": "alerts", "mode": "respond"},
            {
                "id": "C004",
                "name": "archive",
                "mode": "ignore",
                "triggers": [{"rule": "keyword-alert"}],
            },
        ],
    }


@pytest.fixture
def config_file(tmp_path: Path, sample_config: dict) -> Path:
    path = tmp_path / "channels.yaml"
    path.write_text(yaml.dump(sample_config))
    return path


# ---------------------------------------------------------------------------
# Pure function tests: parse_config
# ---------------------------------------------------------------------------


class TestParseConfig:
    def test_empty_config(self):
        defaults, channels = parse_config({})
        assert defaults == _BUILTIN_DEFAULTS
        assert channels == {}

    def test_defaults_merged(self):
        raw = {"defaults": {"mode": "respond", "custom_key": 42}}
        defaults, channels = parse_config(raw)
        assert defaults["mode"] == "respond"
        assert defaults["custom_key"] == 42
        # Builtin defaults still present
        assert defaults["log_raw"] is True

    def test_channels_parsed(self, sample_config):
        defaults, channels = parse_config(sample_config)
        assert len(channels) == 4
        assert channels["C001"]["mode"] == "monitor"
        assert channels["C002"]["mode"] == "full"

    def test_channel_inherits_defaults(self, sample_config):
        defaults, channels = parse_config(sample_config)
        # C001 should have log_raw from file defaults
        assert channels["C001"]["log_raw"] is True

    def test_invalid_mode_falls_back(self):
        raw = {"channels": [{"id": "C999", "mode": "banana"}]}
        _, channels = parse_config(raw)
        assert channels["C999"]["mode"] == "monitor"

    def test_channel_without_id_skipped(self):
        raw = {"channels": [{"name": "no-id", "mode": "full"}]}
        _, channels = parse_config(raw)
        assert channels == {}

    def test_none_channels_list(self):
        raw = {"channels": None}
        _, channels = parse_config(raw)
        assert channels == {}


# ---------------------------------------------------------------------------
# Pure function tests: resolve_channel
# ---------------------------------------------------------------------------


class TestResolveChannel:
    def test_known_channel(self, sample_config):
        defaults, channels = parse_config(sample_config)
        cfg = resolve_channel("C002", defaults, channels)
        assert cfg["mode"] == "full"
        assert cfg["name"] == "ops"

    def test_unknown_channel_returns_defaults(self, sample_config):
        defaults, channels = parse_config(sample_config)
        cfg = resolve_channel("CUNKNOWN", defaults, channels)
        assert cfg["mode"] == "monitor"


# ---------------------------------------------------------------------------
# Pure function tests: should_route_to_llm
# ---------------------------------------------------------------------------


class TestShouldRouteToLlm:
    """Test all four modes with various event/mention combinations."""

    def test_monitor_never_routes(self):
        cfg = {"mode": "monitor"}
        assert not should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=False, is_dm=False)
        assert not should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=True, is_dm=False)
        assert not should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=False, is_dm=True)

    def test_ignore_never_routes(self):
        cfg = {"mode": "ignore"}
        assert not should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=True, is_dm=True)

    def test_respond_routes_mention(self):
        cfg = {"mode": "respond"}
        assert should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=True, is_dm=False)

    def test_respond_routes_dm(self):
        cfg = {"mode": "respond"}
        assert should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=False, is_dm=True)

    def test_respond_does_not_route_plain_message(self):
        cfg = {"mode": "respond"}
        assert not should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=False, is_dm=False)

    def test_full_always_routes(self):
        cfg = {"mode": "full"}
        assert should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=False, is_dm=False)
        assert should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=True, is_dm=False)

    def test_unknown_mode_does_not_route(self):
        cfg = {"mode": "unknown_mode"}
        assert not should_route_to_llm(channel_cfg=cfg, event_type="message", is_mention=True, is_dm=True)


# ---------------------------------------------------------------------------
# Pure function tests: get_channel_triggers
# ---------------------------------------------------------------------------


class TestGetChannelTriggers:
    def test_no_triggers(self):
        assert get_channel_triggers({"mode": "monitor"}) == []

    def test_triggers_extracted(self):
        cfg = {
            "mode": "full",
            "triggers": [{"rule": "keyword-alert"}, {"rule": "deploy-watch"}],
        }
        assert get_channel_triggers(cfg) == ["keyword-alert", "deploy-watch"]

    def test_ignore_mode_suppresses_triggers(self):
        cfg = {
            "mode": "ignore",
            "triggers": [{"rule": "keyword-alert"}],
        }
        assert get_channel_triggers(cfg) == []

    def test_string_triggers(self):
        cfg = {"mode": "respond", "triggers": ["rule-a", "rule-b"]}
        assert get_channel_triggers(cfg) == ["rule-a", "rule-b"]

    def test_empty_rule_skipped(self):
        cfg = {"mode": "full", "triggers": [{"rule": ""}, {"rule": "valid"}]}
        assert get_channel_triggers(cfg) == ["valid"]


# ---------------------------------------------------------------------------
# ChannelConfig class tests (stateful wrapper)
# ---------------------------------------------------------------------------


class TestChannelConfig:
    def test_loads_from_file(self, config_file):
        cc = ChannelConfig(config_path=str(config_file))
        assert cc.get_channel_mode("C002") == "full"
        assert cc.get_channel_mode("CUNKNOWN") == "monitor"

    def test_missing_file_uses_defaults(self, tmp_path):
        cc = ChannelConfig(config_path=str(tmp_path / "nonexistent.yaml"))
        assert cc.get_channel_mode("C001") == "monitor"

    def test_should_route_delegates(self, config_file):
        cc = ChannelConfig(config_path=str(config_file))
        # C002 is mode=full → always route
        assert cc.should_route_to_llm("C002", is_mention=False)
        # C001 is mode=monitor → never route
        assert not cc.should_route_to_llm("C001", is_mention=True)
        # C003 is mode=respond → route on mention
        assert cc.should_route_to_llm("C003", is_mention=True)
        assert not cc.should_route_to_llm("C003", is_mention=False)

    def test_get_triggers(self, config_file):
        cc = ChannelConfig(config_path=str(config_file))
        # C004 is ignore mode — triggers suppressed
        assert cc.get_channel_triggers("C004") == []
        # C001 has no triggers
        assert cc.get_channel_triggers("C001") == []

    def test_reload_picks_up_changes(self, config_file):
        cc = ChannelConfig(config_path=str(config_file))
        assert cc.get_channel_mode("C001") == "monitor"

        # Change C001 to full
        new_config = {
            "channels": [{"id": "C001", "name": "general", "mode": "full"}]
        }
        config_file.write_text(yaml.dump(new_config))
        cc.reload()
        assert cc.get_channel_mode("C001") == "full"

    def test_corrupt_file_keeps_previous(self, config_file):
        cc = ChannelConfig(config_path=str(config_file))
        assert cc.get_channel_mode("C002") == "full"

        # Write garbage
        config_file.write_text("!!invalid: yaml: [[[")
        cc.reload()
        # Should keep previous config
        assert cc.get_channel_mode("C002") == "full"

    def test_dm_routing_with_respond_mode(self, config_file):
        cc = ChannelConfig(config_path=str(config_file))
        # C003 is respond mode — DMs should route
        assert cc.should_route_to_llm("C003", is_dm=True)
