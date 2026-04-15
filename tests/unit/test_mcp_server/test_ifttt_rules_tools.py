"""
Tests for MCP Server IFTTT Behavioral Rules Tools

Covers: list_rules, add_rule, delete_rule, get_rule, update_rule
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _make_mock_memory(store_return_value: int = 42):
    """Build a mock _memory_provider with a store() method returning an int ID."""
    mock = MagicMock()
    mock.store.return_value = store_return_value
    return mock


# =============================================================================
# list_rules
# =============================================================================


class TestListRules:
    def test_returns_all_rules_by_default(self, tmp_path):
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {"id": "r1", "condition": "IF x", "action_ref": "1", "enabled": True},
            {"id": "r2", "condition": "IF y", "action_ref": "2", "enabled": False},
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
            {"id": "r1", "condition": "IF x", "action_ref": "1", "enabled": True},
            {"id": "r2", "condition": "IF y", "action_ref": "2", "enabled": False},
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
                "action_ref": "99",
                "enabled": True,
            }
        ])
        loaded = _read_rules_yaml(rules_path)
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=loaded):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({}))

        text = result[0].text
        assert "user mentions a meeting" in text
        assert "99" in text

    def test_shows_disabled_label_for_disabled_rules(self, tmp_path):
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {"id": "r-off", "condition": "IF off", "action_ref": "5", "enabled": False},
        ])
        loaded = _read_rules_yaml(rules_path)
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=loaded):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({}))

        assert "[disabled]" in result[0].text

    def test_resolve_includes_memory_content(self, tmp_path):
        """When resolve=True, action content from the memory DB is included."""
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {"id": "r1", "condition": "IF x", "action_ref": "7", "enabled": True},
        ])
        loaded = _read_rules_yaml(rules_path)

        mock_event = MagicMock()
        mock_event.content = "Do something helpful"
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_event

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=loaded),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_list_rules
            result = asyncio.run(handle_list_rules({"resolve": True}))

        text = result[0].text
        assert "Do something helpful" in text
        mock_provider.get.assert_called_once_with(7)

    def test_resolve_false_omits_memory_content(self, tmp_path):
        """When resolve=False (default), memory content is not fetched."""
        rules_path = _default_rules_path(tmp_path)
        _write_rules_yaml(rules_path, [
            {"id": "r1", "condition": "IF x", "action_ref": "7", "enabled": True},
        ])
        loaded = _read_rules_yaml(rules_path)

        mock_provider = MagicMock()

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=loaded),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_list_rules
            asyncio.run(handle_list_rules({}))

        mock_provider.get.assert_not_called()


# =============================================================================
# add_rule
# =============================================================================


class TestAddRule:
    def test_adds_rule_stores_to_memory_and_returns_id(self, tmp_path):
        """add_rule stores action_content to memory DB and uses returned ID as action_ref."""
        saved_rules = []
        mock_provider = _make_mock_memory(store_return_value=55)

        def fake_load():
            return []

        def fake_save(rules, **kwargs):
            saved_rules.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", side_effect=fake_load),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_add_rule
            result = asyncio.run(handle_add_rule({
                "condition": "user asks about weather",
                "action_content": "Check weather API and respond with a forecast",
            }))

        text = result[0].text
        assert "Rule added:" in text
        assert "55" in text  # action_ref ID echoed back
        assert len(saved_rules) == 1
        assert saved_rules[0]["condition"] == "user asks about weather"
        assert saved_rules[0]["action_ref"] == "55"
        assert saved_rules[0]["enabled"] is True
        mock_provider.store.assert_called_once()

    def test_requires_condition(self):
        mock_provider = _make_mock_memory()
        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_add_rule
            result = asyncio.run(handle_add_rule({"action_content": "do x"}))

        assert "Error" in result[0].text
        assert "condition" in result[0].text.lower()

    def test_requires_action_content(self):
        mock_provider = _make_mock_memory()
        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_add_rule
            result = asyncio.run(handle_add_rule({"condition": "IF x"}))

        assert "Error" in result[0].text
        assert "action_content" in result[0].text.lower()

    def test_fails_gracefully_when_memory_unavailable(self):
        """When memory system is None, add_rule returns an error rather than crashing."""
        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]),
            patch("src.mcp.inbox_server._memory_provider", None),
        ):
            from src.mcp.inbox_server import handle_add_rule
            result = asyncio.run(handle_add_rule({
                "condition": "IF x",
                "action_content": "do something",
            }))

        assert "Error" in result[0].text
        assert "memory" in result[0].text.lower()

    def test_generated_id_is_slug_derived_from_condition(self):
        saved_rules = []
        mock_provider = _make_mock_memory(store_return_value=10)

        def fake_load():
            return []

        def fake_save(rules, **kwargs):
            saved_rules.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", side_effect=fake_load),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_add_rule
            asyncio.run(handle_add_rule({
                "condition": "User mentions a project deadline",
                "action_content": "Note the deadline",
            }))

        assert saved_rules
        rule_id = saved_rules[0]["id"]
        assert "user" in rule_id or "mention" in rule_id or "project" in rule_id

    def test_id_includes_uuid_suffix_for_uniqueness(self):
        """Two rules with the same condition get different IDs due to UUID suffix."""
        ids = []
        mock_provider = _make_mock_memory(store_return_value=1)

        def fake_load():
            return []

        def fake_save(rules, **kwargs):
            if rules:
                ids.append(rules[-1]["id"])

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", side_effect=fake_load),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_add_rule
            asyncio.run(handle_add_rule({"condition": "same condition", "action_content": "do a"}))
            asyncio.run(handle_add_rule({"condition": "same condition", "action_content": "do b"}))

        assert len(ids) == 2
        assert ids[0] != ids[1]


# =============================================================================
# delete_rule
# =============================================================================


class TestDeleteRule:
    def test_deletes_existing_rule_returns_true(self, tmp_path):
        existing = [{"id": "r1", "condition": "IF x", "action_ref": "1", "enabled": True}]
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
            {"id": "r1", "condition": "IF x", "action_ref": "1", "enabled": True},
            {"id": "r2", "condition": "IF y", "action_ref": "2", "enabled": True},
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

    def test_delete_memory_true_deletes_memory_entry(self):
        """When delete_memory=True, the action_ref entry is removed from the memory DB."""
        existing = [{"id": "r1", "condition": "IF x", "action_ref": "42", "enabled": True}]
        mock_provider = MagicMock()
        mock_provider.delete.return_value = True

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules"),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_delete_rule
            result = asyncio.run(handle_delete_rule({"rule_id": "r1", "delete_memory": True}))

        text = result[0].text
        assert "true" in text
        assert "42" in text
        mock_provider.delete.assert_called_once_with(42)

    def test_delete_memory_false_does_not_touch_memory(self):
        """When delete_memory=False (default), memory DB is not touched."""
        existing = [{"id": "r1", "condition": "IF x", "action_ref": "42", "enabled": True}]
        mock_provider = MagicMock()

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules"),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_delete_rule
            asyncio.run(handle_delete_rule({"rule_id": "r1"}))

        mock_provider.delete.assert_not_called()

    def test_delete_memory_graceful_when_memory_unavailable(self):
        """delete_memory=True with no memory provider still deletes the rule."""
        existing = [{"id": "r1", "condition": "IF x", "action_ref": "42", "enabled": True}]

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules"),
            patch("src.mcp.inbox_server._memory_provider", None),
        ):
            from src.mcp.inbox_server import handle_delete_rule
            result = asyncio.run(handle_delete_rule({"rule_id": "r1", "delete_memory": True}))

        text = result[0].text
        assert "true" in text


# =============================================================================
# get_rule
# =============================================================================


class TestGetRule:
    def test_returns_rule_fields(self):
        existing = [
            {
                "id": "check-cal",
                "condition": "user mentions meeting",
                "action_ref": "99",
                "enabled": True,
            }
        ]
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=existing):
            from src.mcp.inbox_server import handle_get_rule
            result = asyncio.run(handle_get_rule({"rule_id": "check-cal"}))

        text = result[0].text
        assert "check-cal" in text
        assert "user mentions meeting" in text
        assert "99" in text
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
                "action_ref": "7",
                "enabled": False,
            }
        ]
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=existing):
            from src.mcp.inbox_server import handle_get_rule
            result = asyncio.run(handle_get_rule({"rule_id": "paused-rule"}))

        text = result[0].text
        assert "paused-rule" in text
        assert "False" in text

    def test_resolve_true_includes_memory_content(self):
        """When resolve=True, the action_ref content is fetched and included."""
        existing = [
            {
                "id": "r1",
                "condition": "user asks about time",
                "action_ref": "13",
                "enabled": True,
            }
        ]
        mock_event = MagicMock()
        mock_event.content = "Check the clock and report the local time"
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_event

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=existing),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_get_rule
            result = asyncio.run(handle_get_rule({"rule_id": "r1", "resolve": True}))

        text = result[0].text
        assert "Check the clock and report the local time" in text
        mock_provider.get.assert_called_once_with(13)

    def test_resolve_false_omits_memory_lookup(self):
        """When resolve=False (default), memory DB is not queried."""
        existing = [
            {"id": "r1", "condition": "IF x", "action_ref": "13", "enabled": True}
        ]
        mock_provider = MagicMock()

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=existing),
            patch("src.mcp.inbox_server._memory_provider", mock_provider),
        ):
            from src.mcp.inbox_server import handle_get_rule
            asyncio.run(handle_get_rule({"rule_id": "r1"}))

        mock_provider.get.assert_not_called()


# =============================================================================
# update_rule
# =============================================================================


class TestUpdateRule:
    def test_disables_enabled_rule(self):
        existing = [
            {"id": "r1", "condition": "IF x", "action_ref": "mem_1", "enabled": True}
        ]
        saved = []

        def fake_save(rules, **kwargs):
            saved.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
        ):
            from src.mcp.inbox_server import handle_update_rule
            result = asyncio.run(handle_update_rule({"rule_id": "r1", "enabled": False}))

        text = result[0].text
        assert "r1" in text
        assert "False" in text
        assert saved[0]["enabled"] is False

    def test_re_enables_disabled_rule(self):
        existing = [
            {"id": "r1", "condition": "IF x", "action_ref": "mem_1", "enabled": False}
        ]
        saved = []

        def fake_save(rules, **kwargs):
            saved.extend(rules)

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules", side_effect=fake_save),
        ):
            from src.mcp.inbox_server import handle_update_rule
            result = asyncio.run(handle_update_rule({"rule_id": "r1", "enabled": True}))

        text = result[0].text
        assert "r1" in text
        assert "True" in text
        assert saved[0]["enabled"] is True

    def test_returns_null_when_rule_not_found(self):
        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]),
            patch("src.mcp.inbox_server._ifttt_save_rules") as mock_save,
        ):
            from src.mcp.inbox_server import handle_update_rule
            result = asyncio.run(handle_update_rule({"rule_id": "nonexistent", "enabled": False}))

        assert "null" in result[0].text
        mock_save.assert_not_called()

    def test_requires_rule_id(self):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_update_rule
            result = asyncio.run(handle_update_rule({"enabled": False}))

        assert "Error" in result[0].text
        assert "rule_id" in result[0].text.lower()

    def test_requires_enabled(self):
        with patch("src.mcp.inbox_server._ifttt_load_rules", return_value=[]):
            from src.mcp.inbox_server import handle_update_rule
            result = asyncio.run(handle_update_rule({"rule_id": "r1"}))

        assert "Error" in result[0].text
        assert "enabled" in result[0].text.lower()

    def test_only_updates_target_rule(self):
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
            from src.mcp.inbox_server import handle_update_rule
            asyncio.run(handle_update_rule({"rule_id": "r1", "enabled": False}))

        assert len(saved) == 2
        r1 = next(r for r in saved if r["id"] == "r1")
        r2 = next(r for r in saved if r["id"] == "r2")
        assert r1["enabled"] is False
        assert r2["enabled"] is True  # untouched

    def test_returns_updated_rule_fields(self):
        existing = [
            {
                "id": "check-cal",
                "condition": "user mentions meeting",
                "action_ref": "mem_xyz",
                "enabled": True,
            }
        ]

        with (
            patch("src.mcp.inbox_server._ifttt_load_rules", return_value=list(existing)),
            patch("src.mcp.inbox_server._ifttt_save_rules"),
        ):
            from src.mcp.inbox_server import handle_update_rule
            result = asyncio.run(handle_update_rule({"rule_id": "check-cal", "enabled": False}))

        text = result[0].text
        assert "check-cal" in text
        assert "user mentions meeting" in text
        assert "mem_xyz" in text
