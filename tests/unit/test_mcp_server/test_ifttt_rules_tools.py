"""
Tests for MCP Server IFTTT Behavioral Rules Tools

Covers: list_rules, add_rule, delete_rule, get_rule
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


def _write_rules_yaml(path: Path, rules: list[dict]) -> None:
    """Write a rules YAML file to the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump({"version": 1, "rules": rules}, default_flow_style=False),
        encoding="utf-8",
    )


def _read_rules_yaml(path: Path) -> list[dict]:
    """Read rules from a YAML file."""
    from src.utils.ifttt_rules import load_rules
    return load_rules(path)


def _default_rules_path(tmp_path: Path) -> Path:
    return tmp_path / "ifttt-rules.yaml"


# =============================================================================
# list_rules
# =============================================================================


class TestListRules:
    def test_returns_all_rules_by_default(self, tmp_path):
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {"id": "r1", "condition": "IF x", "action_ref": "mem_1", "enabled": True},
            {"id": "r2", "condition": "IF y", "action_ref": "mem_2", "enabled": False},
        ])

        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=_read_rules_yaml(rules_path)):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({}))

        text = result[0].text
        assert "r1" in text
        assert "r2" in text
        assert "2 total" in text

    def test_enabled_only_filters_disabled(self, tmp_path):
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {"id": "r1", "condition": "IF x", "action_ref": "mem_1", "enabled": True},
            {"id": "r2", "condition": "IF y", "action_ref": "mem_2", "enabled": False},
        ])

        loaded = _read_rules_yaml(rules_path)
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=loaded):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({"enabled_only": True}))

        text = result[0].text
        assert "r1" in text
        assert "r2" not in text

    def test_empty_store_returns_no_rules_message(self, tmp_path):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({}))

        assert "No" in result[0].text
        assert "rules" in result[0].text.lower()

    def test_shows_condition_and_action_ref(self, tmp_path):
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {
                "id": "check-calendar",
                "condition": "user mentions a meeting",
                "action_ref": "mem_abc123",
                "enabled": True,
            }
        ])
        loaded = _read_rules_yaml(rules_path)
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=loaded):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({}))

        text = result[0].text
        assert "user mentions a meeting" in text
        assert "mem_abc123" in text

    def test_shows_disabled_label_for_disabled_rules(self, tmp_path):
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {"id": "r-off", "condition": "IF off", "action_ref": "mem_off", "enabled": False},
        ])
        loaded = _read_rules_yaml(rules_path)
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=loaded):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({}))

        assert "[disabled]" in result[0].text


# =============================================================================
# add_rule
# =============================================================================


class TestAddRule:
    def test_adds_rule_and_returns_id(self, tmp_path):
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [])

        saved_rules = []

        def fake_load():
            return []

        def fake_save(rules, **kwargs):
            saved_rules.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", side_effect=fake_load),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
        ):
            from src.mcp.inbox_server import handle_add_rule
            result = asyncio.run(handle_add_rule({
                "condition": "user asks about weather",
                "action_ref": "mem_weather_001",
            }))

        text = result[0].text
        assert "Rule added:" in text
        assert len(saved_rules) == 1
        assert saved_rules[0]["condition"] == "user asks about weather"
        assert saved_rules[0]["action_ref"] == "mem_weather_001"
        assert saved_rules[0]["enabled"] is True

    def test_requires_condition(self):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_add_rule
            result = asyncio.run(handle_add_rule({"action_ref": "mem_x"}))

        assert "Error" in result[0].text
        assert "condition" in result[0].text.lower()

    def test_requires_action_ref(self):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_add_rule
            result = asyncio.run(handle_add_rule({"condition": "IF x"}))

        assert "Error" in result[0].text
        assert "action_ref" in result[0].text.lower()

    def test_generated_id_is_slug_derived_from_condition(self):
        saved_rules = []

        def fake_load():
            return []

        def fake_save(rules, **kwargs):
            saved_rules.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", side_effect=fake_load),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
        ):
            from src.mcp.inbox_server import handle_add_rule
            asyncio.run(handle_add_rule({
                "condition": "User mentions a project deadline",
                "action_ref": "mem_deadline",
            }))

        assert saved_rules
        rule_id = saved_rules[0]["id"]
        assert "user" in rule_id or "mention" in rule_id or "project" in rule_id

    def test_id_includes_uuid_suffix_for_uniqueness(self):
        """Two rules with the same condition get different IDs due to UUID suffix."""
        ids = []

        def fake_load():
            return []

        def fake_save(rules, **kwargs):
            if rules:
                ids.append(rules[-1]["id"])

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", side_effect=fake_load),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
        ):
            from src.mcp.inbox_server import handle_add_rule
            asyncio.run(handle_add_rule({"condition": "same condition", "action_ref": "mem_a"}))
            asyncio.run(handle_add_rule({"condition": "same condition", "action_ref": "mem_b"}))

        assert len(ids) == 2
        assert ids[0] != ids[1]


# =============================================================================
# delete_rule
# =============================================================================


class TestDeleteRule:
    def test_deletes_existing_rule_returns_true(self, tmp_path):
        existing = [{"id": "r1", "condition": "IF x", "action_ref": "mem_1", "enabled": True}]
        saved = []

        def fake_save(rules, **kwargs):
            saved.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
        ):
            from src.mcp.inbox_server import handle_delete_rule
            result = asyncio.run(handle_delete_rule({"rule_id": "r1"}))

        assert "true" in result[0].text
        assert saved == []  # rule was removed

    def test_missing_rule_returns_false(self):
        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]),
            patch("src.mcp.inbox_server._ifttt_save_rules") as mock_save,
        ):
            from src.mcp.inbox_server import handle_delete_rule
            result = asyncio.run(handle_delete_rule({"rule_id": "nonexistent"}))

        assert "false" in result[0].text
        mock_save.assert_not_called()

    def test_requires_rule_id(self):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_delete_rule
            result = asyncio.run(handle_delete_rule({}))

        assert "Error" in result[0].text

    def test_only_deletes_target_rule(self, tmp_path):
        existing = [
            {"id": "r1", "condition": "IF x", "action_ref": "mem_1", "enabled": True},
            {"id": "r2", "condition": "IF y", "action_ref": "mem_2", "enabled": True},
        ]
        saved = []

        def fake_save(rules, **kwargs):
            saved.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
        ):
            from src.mcp.inbox_server import handle_delete_rule
            asyncio.run(handle_delete_rule({"rule_id": "r1"}))

        assert len(saved) == 1
        assert saved[0]["id"] == "r2"


# =============================================================================
# get_rule
# =============================================================================


class TestGetRule:
    def test_returns_rule_fields(self):
        existing = [
            {
                "id": "check-cal",
                "condition": "user mentions meeting",
                "action_ref": "mem_xyz",
                "enabled": True,
            }
        ]
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=existing):
            from src.mcp.inbox_server import handle_get_rule
            result = asyncio.run(handle_get_rule({"rule_id": "check-cal"}))

        text = result[0].text
        assert "check-cal" in text
        assert "user mentions meeting" in text
        assert "mem_xyz" in text
        assert "True" in text

    def test_missing_rule_returns_null(self):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_get_rule
            result = asyncio.run(handle_get_rule({"rule_id": "nonexistent"}))

        assert "null" in result[0].text

    def test_requires_rule_id(self):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_get_rule
            result = asyncio.run(handle_get_rule({}))

        assert "Error" in result[0].text

    def test_returns_disabled_rule_when_present(self):
        existing = [
            {
                "id": "paused-rule",
                "condition": "IF sleeping",
                "action_ref": "mem_zzz",
                "enabled": False,
            }
        ]
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=existing):
            from src.mcp.inbox_server import handle_get_rule
            result = asyncio.run(handle_get_rule({"rule_id": "paused-rule"}))

        text = result[0].text
        assert "paused-rule" in text
        assert "False" in text
