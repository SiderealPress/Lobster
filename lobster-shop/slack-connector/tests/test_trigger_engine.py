"""Tests for trigger_engine module — pure functions and TriggerEngine class."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.trigger_engine import (
    TriggerEngine,
    build_template_vars,
    evaluate_all,
    evaluate_rule,
    interpolate_template,
    load_rules_from_dir,
    matches_channel,
    matches_command,
    matches_emoji,
    matches_event_type,
    matches_file_type,
    matches_keywords,
    matches_regex,
    matches_user,
    parse_rule,
    VALID_ACTION_TYPES,
    VALID_EVENT_TYPES,
    VALID_KEYWORD_MODES,
    _handle_lobster_task,
    _handle_send_reply,
    _handle_shell,
    _handle_telegram_notify,
    _handle_webhook,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_rule(**overrides) -> dict:
    """Build a valid rule dict with sensible defaults, applying overrides."""
    base = {
        "name": "test-rule",
        "description": "A test rule",
        "enabled": True,
        "source_path": "/tmp/test.toml",
        "event": "message",
        "channels": [],
        "users": [],
        "keywords": [],
        "keyword_mode": "any",
        "emoji": None,
        "command": None,
        "file_type": None,
        "regex": None,
        "compiled_regex": None,
        "action_type": "send_reply",
        "action": {"type": "send_reply", "message": "Hello"},
    }
    base.update(overrides)
    return base


def _make_event(**overrides) -> dict:
    """Build a minimal Slack message event with overrides."""
    base = {
        "type": "message",
        "channel": "C001",
        "user": "U001",
        "text": "hello world",
        "ts": "1234567890.123456",
        "thread_ts": "",
        "username": "alice",
        "channel_name": "general",
    }
    base.update(overrides)
    return base


SAMPLE_TOML = """\
[rule]
name = "test-keyword"
description = "Test keyword rule"
enabled = true

[trigger]
event = "message"
channels = ["C001", "C002"]
keywords = ["deploy", "outage"]
keyword_mode = "any"

[action]
type = "send_reply"
message = "Alert: {message_text} in #{channel_name}"
"""

DISABLED_TOML = """\
[rule]
name = "disabled-rule"
enabled = false

[trigger]
event = "message"

[action]
type = "send_reply"
message = "Should not fire"
"""

REGEX_TOML = """\
[rule]
name = "jira-tracker"
description = "Track JIRA ticket mentions"
enabled = true

[trigger]
event = "message"
regex = "JIRA-\\\\d+"

[action]
type = "telegram_notify"
message = "JIRA ticket mentioned: {message_text}"
"""


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    """Create a temp rules directory with sample TOML files."""
    d = tmp_path / "rules"
    d.mkdir()
    (d / "keyword.toml").write_text(SAMPLE_TOML)
    (d / "disabled.toml").write_text(DISABLED_TOML)
    return d


@pytest.fixture
def rules_dir_with_examples(rules_dir: Path) -> Path:
    """Rules dir with an examples/ subdirectory that should be excluded."""
    examples = rules_dir / "examples"
    examples.mkdir()
    (examples / "sample.toml").write_text(SAMPLE_TOML.replace("test-keyword", "example-rule"))
    return rules_dir


# ---------------------------------------------------------------------------
# Pure function tests: parse_rule
# ---------------------------------------------------------------------------


class TestParseRule:
    def test_valid_rule(self):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        raw = tomllib.loads(SAMPLE_TOML)
        rule = parse_rule(raw, source_path="/tmp/test.toml")
        assert rule is not None
        assert rule["name"] == "test-keyword"
        assert rule["event"] == "message"
        assert rule["channels"] == ["C001", "C002"]
        assert rule["keywords"] == ["deploy", "outage"]
        assert rule["keyword_mode"] == "any"
        assert rule["action_type"] == "send_reply"
        assert rule["enabled"] is True

    def test_missing_name_returns_none(self):
        raw = {"rule": {}, "trigger": {"event": "message"}, "action": {"type": "send_reply"}}
        assert parse_rule(raw) is None

    def test_invalid_event_returns_none(self):
        raw = {
            "rule": {"name": "bad"},
            "trigger": {"event": "invalid_event"},
            "action": {"type": "send_reply"},
        }
        assert parse_rule(raw) is None

    def test_invalid_action_type_returns_none(self):
        raw = {
            "rule": {"name": "bad"},
            "trigger": {"event": "message"},
            "action": {"type": "invalid_action"},
        }
        assert parse_rule(raw) is None

    def test_invalid_keyword_mode_defaults_to_any(self):
        raw = {
            "rule": {"name": "test"},
            "trigger": {"event": "message", "keyword_mode": "banana"},
            "action": {"type": "send_reply"},
        }
        rule = parse_rule(raw)
        assert rule is not None
        assert rule["keyword_mode"] == "any"

    def test_invalid_regex_returns_none(self):
        raw = {
            "rule": {"name": "test"},
            "trigger": {"event": "message", "regex": "[invalid("},
            "action": {"type": "send_reply"},
        }
        assert parse_rule(raw) is None

    def test_defaults_applied(self):
        raw = {
            "rule": {"name": "minimal"},
            "trigger": {},
            "action": {"type": "send_reply"},
        }
        rule = parse_rule(raw)
        assert rule is not None
        assert rule["event"] == "message"
        assert rule["channels"] == []
        assert rule["users"] == []
        assert rule["keywords"] == []
        assert rule["keyword_mode"] == "any"
        assert rule["enabled"] is True


# ---------------------------------------------------------------------------
# Pure function tests: load_rules_from_dir
# ---------------------------------------------------------------------------


class TestLoadRulesFromDir:
    def test_loads_rules(self, rules_dir):
        rules = load_rules_from_dir(rules_dir)
        assert "test-keyword" in rules
        assert "disabled-rule" in rules
        assert len(rules) == 2

    def test_nonexistent_dir(self, tmp_path):
        rules = load_rules_from_dir(tmp_path / "nonexistent")
        assert rules == {}

    def test_skips_invalid_files(self, rules_dir):
        (rules_dir / "bad.toml").write_text("this is not valid toml {{{}}")
        rules = load_rules_from_dir(rules_dir)
        # Should still load the valid rules
        assert "test-keyword" in rules

    def test_excludes_examples_dir(self, rules_dir_with_examples):
        rules = load_rules_from_dir(rules_dir_with_examples)
        assert "example-rule" not in rules
        assert "test-keyword" in rules


# ---------------------------------------------------------------------------
# Pure function tests: event matching
# ---------------------------------------------------------------------------


class TestMatchesEventType:
    def test_matching_type(self):
        rule = _make_rule(event="message")
        event = _make_event(type="message")
        assert matches_event_type(rule, event) is True

    def test_non_matching_type(self):
        rule = _make_rule(event="reaction_added")
        event = _make_event(type="message")
        assert matches_event_type(rule, event) is False

    def test_event_type_fallback_key(self):
        rule = _make_rule(event="message")
        event = {"event_type": "message"}
        assert matches_event_type(rule, event) is True


class TestMatchesChannel:
    def test_empty_channels_matches_all(self):
        rule = _make_rule(channels=[])
        event = _make_event(channel="C999")
        assert matches_channel(rule, event) is True

    def test_matching_channel(self):
        rule = _make_rule(channels=["C001", "C002"])
        assert matches_channel(rule, _make_event(channel="C001")) is True
        assert matches_channel(rule, _make_event(channel="C002")) is True

    def test_non_matching_channel(self):
        rule = _make_rule(channels=["C001"])
        assert matches_channel(rule, _make_event(channel="C999")) is False

    def test_channel_id_fallback_key(self):
        rule = _make_rule(channels=["C001"])
        event = {"channel_id": "C001"}
        assert matches_channel(rule, event) is True


class TestMatchesUser:
    def test_empty_users_matches_all(self):
        rule = _make_rule(users=[])
        assert matches_user(rule, _make_event(user="U999")) is True

    def test_matching_user(self):
        rule = _make_rule(users=["U001"])
        assert matches_user(rule, _make_event(user="U001")) is True

    def test_non_matching_user(self):
        rule = _make_rule(users=["U001"])
        assert matches_user(rule, _make_event(user="U999")) is False


class TestMatchesKeywords:
    def test_no_keywords_matches_all(self):
        rule = _make_rule(keywords=[])
        assert matches_keywords(rule, _make_event(text="anything")) is True

    def test_any_mode_single_match(self):
        rule = _make_rule(keywords=["deploy", "outage"], keyword_mode="any")
        assert matches_keywords(rule, _make_event(text="starting deploy now")) is True

    def test_any_mode_no_match(self):
        rule = _make_rule(keywords=["deploy", "outage"], keyword_mode="any")
        assert matches_keywords(rule, _make_event(text="hello world")) is False

    def test_all_mode_all_present(self):
        rule = _make_rule(keywords=["deploy", "staging"], keyword_mode="all")
        assert matches_keywords(rule, _make_event(text="deploy to staging now")) is True

    def test_all_mode_partial_match(self):
        rule = _make_rule(keywords=["deploy", "staging"], keyword_mode="all")
        assert matches_keywords(rule, _make_event(text="deploy to production")) is False

    def test_case_insensitive(self):
        rule = _make_rule(keywords=["Deploy"], keyword_mode="any")
        assert matches_keywords(rule, _make_event(text="DEPLOY NOW")) is True
        assert matches_keywords(rule, _make_event(text="deploy now")) is True

    def test_empty_text(self):
        rule = _make_rule(keywords=["deploy"], keyword_mode="any")
        assert matches_keywords(rule, _make_event(text="")) is False


class TestMatchesEmoji:
    def test_no_emoji_filter_matches_all(self):
        rule = _make_rule(emoji=None)
        assert matches_emoji(rule, _make_event()) is True

    def test_matching_emoji(self):
        rule = _make_rule(emoji="eyes")
        event = _make_event(reaction="eyes")
        assert matches_emoji(rule, event) is True

    def test_non_matching_emoji(self):
        rule = _make_rule(emoji="eyes")
        event = _make_event(reaction="thumbsup")
        assert matches_emoji(rule, event) is False


class TestMatchesCommand:
    def test_no_command_filter_matches_all(self):
        rule = _make_rule(command=None)
        assert matches_command(rule, _make_event()) is True

    def test_matching_command(self):
        rule = _make_rule(command="/deploy")
        event = _make_event(command="/deploy")
        assert matches_command(rule, event) is True

    def test_non_matching_command(self):
        rule = _make_rule(command="/deploy")
        event = _make_event(command="/status")
        assert matches_command(rule, event) is False


class TestMatchesFileType:
    def test_no_file_type_matches_all(self):
        rule = _make_rule(file_type=None)
        assert matches_file_type(rule, _make_event()) is True

    def test_matching_file_type_in_files_array(self):
        rule = _make_rule(file_type="pdf")
        event = _make_event(files=[{"filetype": "pdf", "name": "doc.pdf"}])
        assert matches_file_type(rule, event) is True

    def test_non_matching_file_type(self):
        rule = _make_rule(file_type="pdf")
        event = _make_event(files=[{"filetype": "png", "name": "image.png"}])
        assert matches_file_type(rule, event) is False


class TestMatchesRegex:
    def test_no_regex_matches_all(self):
        rule = _make_rule(compiled_regex=None)
        assert matches_regex(rule, _make_event()) is True

    def test_matching_regex(self):
        import re
        rule = _make_rule(compiled_regex=re.compile(r"JIRA-\d+", re.IGNORECASE))
        assert matches_regex(rule, _make_event(text="Fix for JIRA-123")) is True

    def test_non_matching_regex(self):
        import re
        rule = _make_rule(compiled_regex=re.compile(r"JIRA-\d+", re.IGNORECASE))
        assert matches_regex(rule, _make_event(text="No ticket here")) is False


# ---------------------------------------------------------------------------
# Pure function tests: evaluate_rule (composition)
# ---------------------------------------------------------------------------


class TestEvaluateRule:
    def test_enabled_rule_matches(self):
        rule = _make_rule(event="message", channels=["C001"], keywords=["hello"])
        event = _make_event(type="message", channel="C001", text="hello world")
        assert evaluate_rule(rule, event) is True

    def test_disabled_rule_never_matches(self):
        rule = _make_rule(enabled=False, event="message")
        event = _make_event(type="message")
        assert evaluate_rule(rule, event) is False

    def test_channel_mismatch_fails(self):
        rule = _make_rule(channels=["C999"])
        event = _make_event(channel="C001")
        assert evaluate_rule(rule, event) is False

    def test_keyword_mismatch_fails(self):
        rule = _make_rule(keywords=["deploy"])
        event = _make_event(text="hello world")
        assert evaluate_rule(rule, event) is False

    def test_all_filters_must_pass(self):
        """Rule with multiple filters requires ALL to match (conjunction)."""
        rule = _make_rule(
            channels=["C001"],
            users=["U001"],
            keywords=["deploy"],
        )
        # Matches all
        event = _make_event(channel="C001", user="U001", text="deploy now")
        assert evaluate_rule(rule, event) is True

        # Wrong user
        event = _make_event(channel="C001", user="U999", text="deploy now")
        assert evaluate_rule(rule, event) is False


class TestEvaluateAll:
    def test_returns_matching_rules(self):
        rules = {
            "r1": _make_rule(name="r1", keywords=["deploy"]),
            "r2": _make_rule(name="r2", keywords=["outage"]),
            "r3": _make_rule(name="r3", keywords=["hello"]),
        }
        event = _make_event(text="deploy is happening")
        matched = evaluate_all(rules, event)
        assert len(matched) == 1
        assert matched[0]["name"] == "r1"

    def test_multiple_matches(self):
        rules = {
            "r1": _make_rule(name="r1", keywords=[]),  # matches everything
            "r2": _make_rule(name="r2", keywords=[]),  # matches everything
        }
        event = _make_event(text="hello")
        matched = evaluate_all(rules, event)
        assert len(matched) == 2

    def test_disabled_rules_excluded(self):
        rules = {
            "r1": _make_rule(name="r1", enabled=False),
        }
        event = _make_event()
        assert evaluate_all(rules, event) == []


# ---------------------------------------------------------------------------
# Pure function tests: template interpolation
# ---------------------------------------------------------------------------


class TestBuildTemplateVars:
    def test_extracts_vars(self):
        event = _make_event(
            text="deploy now",
            channel="C001",
            user="U001",
            username="alice",
            channel_name="eng",
            ts="1234.5678",
            thread_ts="1234.0000",
        )
        v = build_template_vars(event)
        assert v["message_text"] == "deploy now"
        assert v["channel_id"] == "C001"
        assert v["channel_name"] == "eng"
        assert v["user_id"] == "U001"
        assert v["username"] == "alice"
        assert v["ts"] == "1234.5678"
        assert v["thread_ts"] == "1234.0000"

    def test_file_name_from_files(self):
        event = _make_event(files=[{"name": "report.pdf"}])
        v = build_template_vars(event)
        assert v["file_name"] == "report.pdf"

    def test_emoji_from_reaction(self):
        event = _make_event(reaction="thumbsup")
        v = build_template_vars(event)
        assert v["emoji"] == "thumbsup"

    def test_date_is_populated(self):
        event = _make_event()
        v = build_template_vars(event)
        assert len(v["date"]) == 10  # YYYY-MM-DD


class TestInterpolateTemplate:
    def test_basic_interpolation(self):
        result = interpolate_template(
            "Hello {username} in #{channel_name}",
            {"username": "alice", "channel_name": "general"},
        )
        assert result == "Hello alice in #general"

    def test_unknown_vars_left_intact(self):
        result = interpolate_template(
            "Hello {unknown_var}",
            {"username": "alice"},
        )
        assert result == "Hello {unknown_var}"

    def test_multiple_occurrences(self):
        result = interpolate_template(
            "{user} said {user}",
            {"user": "bob"},
        )
        assert result == "bob said bob"

    def test_empty_template(self):
        assert interpolate_template("", {"x": "y"}) == ""


# ---------------------------------------------------------------------------
# Action handler tests (with mocked side effects)
# ---------------------------------------------------------------------------


class TestHandleLobsterTask:
    def test_returns_interpolated_prompt(self):
        action = {
            "type": "lobster_task",
            "task_prompt": "Analyze message from {username}: {message_text}",
            "subagent_type": "general-purpose",
            "run_in_background": True,
        }
        template_vars = {"username": "alice", "message_text": "deploy now"}
        result = _handle_lobster_task(action, template_vars)
        assert result["status"] == "pending"
        assert "alice" in result["task_prompt"]
        assert "deploy now" in result["task_prompt"]
        assert result["subagent_type"] == "general-purpose"

    def test_missing_prompt_errors(self):
        result = _handle_lobster_task({"type": "lobster_task"}, {})
        assert result["status"] == "error"


class TestHandleSendReply:
    def test_returns_interpolated_message(self):
        action = {"type": "send_reply", "message": "Alert: {message_text}"}
        result = _handle_send_reply(action, {"message_text": "deploy", "channel_id": "C001"})
        assert result["status"] == "ready"
        assert result["message"] == "Alert: deploy"
        assert result["channel"] == "C001"

    def test_missing_message_errors(self):
        result = _handle_send_reply({"type": "send_reply"}, {})
        assert result["status"] == "error"

    def test_custom_channel(self):
        action = {"type": "send_reply", "message": "hi", "channel": "C999"}
        result = _handle_send_reply(action, {"channel_id": "C001"})
        assert result["channel"] == "C999"


class TestHandleTelegramNotify:
    def test_returns_interpolated_message(self):
        action = {"type": "telegram_notify", "message": "Alert: {message_text}"}
        with patch.dict(os.environ, {"LOBSTER_ADMIN_CHAT_ID": "12345"}):
            result = _handle_telegram_notify(action, {"message_text": "outage"})
        assert result["status"] == "ready"
        assert result["message"] == "Alert: outage"
        assert result["chat_id"] == "12345"

    def test_custom_chat_id(self):
        action = {"type": "telegram_notify", "message": "hi", "chat_id": "99999"}
        result = _handle_telegram_notify(action, {})
        assert result["chat_id"] == "99999"

    def test_missing_message_errors(self):
        result = _handle_telegram_notify({"type": "telegram_notify"}, {})
        assert result["status"] == "error"


class TestHandleShell:
    def test_successful_command(self):
        action = {"type": "shell", "command": "echo hello"}
        result = _handle_shell(action, {})
        assert result["status"] == "success"
        assert "hello" in result["stdout"]
        assert result["returncode"] == 0

    def test_failed_command(self):
        action = {"type": "shell", "command": "false"}
        result = _handle_shell(action, {})
        assert result["status"] == "error"
        assert result["returncode"] != 0

    def test_template_interpolation(self):
        action = {"type": "shell", "command": "echo {username}"}
        result = _handle_shell(action, {"username": "alice"})
        assert "alice" in result["stdout"]

    def test_missing_command_errors(self):
        result = _handle_shell({"type": "shell"}, {})
        assert result["status"] == "error"

    def test_timeout(self):
        action = {"type": "shell", "command": "sleep 60", "timeout": 1}
        result = _handle_shell(action, {})
        assert result["status"] == "error"
        assert "timed out" in result["error"].lower()


class TestHandleWebhook:
    def test_missing_url_errors(self):
        result = _handle_webhook({"type": "webhook"}, {})
        assert result["status"] == "error"

    def test_successful_post(self):
        """Test webhook with a mocked urlopen."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        action = {"type": "webhook", "url": "https://example.com/hook"}
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = _handle_webhook(action, {"message_text": "hello"})
        assert result["status"] == "success"
        assert result["response_code"] == 200


# ---------------------------------------------------------------------------
# TriggerEngine class tests (stateful wrapper)
# ---------------------------------------------------------------------------


class TestTriggerEngine:
    def test_loads_rules_on_init(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        assert engine.rule_count == 2

    def test_evaluate_returns_matches(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        event = _make_event(channel="C001", text="deploy is starting")
        matched = engine.evaluate(event)
        assert len(matched) == 1
        assert matched[0]["name"] == "test-keyword"

    def test_evaluate_excludes_disabled(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        event = _make_event(text="anything")
        matched = engine.evaluate(event)
        # disabled-rule should not match even though it has no keyword filter
        names = [r["name"] for r in matched]
        assert "disabled-rule" not in names

    def test_evaluate_no_match(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        event = _make_event(channel="C999", text="no keywords here")
        matched = engine.evaluate(event)
        assert matched == []

    def test_fire_action_send_reply(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        rule = engine.rules["test-keyword"]
        event = _make_event(text="deploy", username="alice", channel_name="eng")
        result = engine.fire_action(rule, event)
        assert result["status"] == "ready"
        assert "deploy" in result["message"]
        assert "eng" in result["message"]

    def test_fire_action_unknown_type(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        rule = _make_rule(action_type="nonexistent")
        result = engine.fire_action(rule, _make_event())
        assert result["status"] == "error"

    def test_reload_picks_up_new_rules(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        assert engine.rule_count == 2

        # Add a new rule
        new_toml = """\
[rule]
name = "new-rule"
enabled = true

[trigger]
event = "message"

[action]
type = "send_reply"
message = "new!"
"""
        (rules_dir / "new.toml").write_text(new_toml)
        engine.reload_rules()
        assert engine.rule_count == 3
        assert "new-rule" in engine.rules

    def test_reload_removes_deleted_rules(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        assert "test-keyword" in engine.rules

        (rules_dir / "keyword.toml").unlink()
        engine.reload_rules()
        assert "test-keyword" not in engine.rules

    def test_reload_updates_modified_rules(self, rules_dir):
        engine = TriggerEngine(rules_dir=str(rules_dir))
        rule = engine.rules["test-keyword"]
        assert rule["channels"] == ["C001", "C002"]

        # Modify the rule
        modified = SAMPLE_TOML.replace('channels = ["C001", "C002"]', 'channels = ["C999"]')
        (rules_dir / "keyword.toml").write_text(modified)
        engine.reload_rules()
        assert engine.rules["test-keyword"]["channels"] == ["C999"]

    def test_nonexistent_dir_loads_empty(self, tmp_path):
        engine = TriggerEngine(rules_dir=str(tmp_path / "nonexistent"))
        assert engine.rule_count == 0
        assert engine.evaluate(_make_event()) == []

    def test_enabled_false_disables_immediately(self, rules_dir):
        """Setting enabled=false in TOML prevents rule from firing."""
        engine = TriggerEngine(rules_dir=str(rules_dir))
        # Modify keyword rule to be disabled
        disabled = SAMPLE_TOML.replace("enabled = true", "enabled = false")
        (rules_dir / "keyword.toml").write_text(disabled)
        engine.reload_rules()

        event = _make_event(channel="C001", text="deploy now")
        assert engine.evaluate(event) == []


# ---------------------------------------------------------------------------
# Integration: end-to-end evaluate + fire
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_keyword_alert_fires(self, rules_dir):
        """Posting text with a keyword fires the matching rule."""
        engine = TriggerEngine(rules_dir=str(rules_dir))
        event = _make_event(
            channel="C001",
            text="deploy to production please",
            username="bob",
            channel_name="engineering",
        )
        matched = engine.evaluate(event)
        assert len(matched) == 1

        result = engine.fire_action(matched[0], event)
        assert result["status"] == "ready"
        assert "deploy to production please" in result["message"]
        assert "engineering" in result["message"]

    def test_all_keyword_mode(self, rules_dir):
        """keyword_mode='all' requires every keyword present."""
        all_toml = """\
[rule]
name = "all-keywords"
enabled = true

[trigger]
event = "message"
keywords = ["deploy", "staging"]
keyword_mode = "all"

[action]
type = "send_reply"
message = "Both keywords found"
"""
        (rules_dir / "all.toml").write_text(all_toml)
        engine = TriggerEngine(rules_dir=str(rules_dir))

        # Only one keyword — should not match
        event1 = _make_event(text="deploy to production")
        matched1 = [r for r in engine.evaluate(event1) if r["name"] == "all-keywords"]
        assert matched1 == []

        # Both keywords — should match
        event2 = _make_event(text="deploy to staging now")
        matched2 = [r for r in engine.evaluate(event2) if r["name"] == "all-keywords"]
        assert len(matched2) == 1

    def test_empty_channels_matches_all(self, tmp_path):
        """channels=[] should match messages from any channel."""
        d = tmp_path / "rules2"
        d.mkdir()
        toml = """\
[rule]
name = "global"
enabled = true

[trigger]
event = "message"
channels = []
keywords = ["lobster-test"]

[action]
type = "send_reply"
message = "Triggered!"
"""
        (d / "global.toml").write_text(toml)
        engine = TriggerEngine(rules_dir=str(d))

        for ch in ["C001", "C002", "C999"]:
            event = _make_event(channel=ch, text="lobster-test in channel")
            assert len(engine.evaluate(event)) == 1
