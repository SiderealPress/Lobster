"""
Tests for src/utils/ifttt_rules.py — IFTTT-style behavioral rules store.

Covers:
- add_rule: append new rule, replace existing rule, cap enforcement (FIFO tail drop)
- remove_rule: remove existing, no-op on missing
- get_enabled_rules: filters disabled rules
- find_rule: lookup by ID
- format_rules_for_context: plain-text rendering with action_ref, empty case
- load_rules: missing file, malformed YAML, missing keys, valid file
- save_rules: round-trip, atomic write, cap enforcement before write
"""

import os
from pathlib import Path

import pytest
import yaml

from src.utils.ifttt_rules import (
    MAX_RULES,
    add_rule,
    find_rule,
    format_rules_for_context,
    get_enabled_rules,
    load_rules,
    remove_rule,
    save_rules,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_rule(
    rule_id: str,
    enabled: bool = True,
    action_ref: str | None = None,
) -> dict:
    return {
        "id": rule_id,
        "condition": f"condition for {rule_id}",
        "action_ref": action_ref or f"mem_{rule_id}",
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# add_rule
# ---------------------------------------------------------------------------


class TestAddRule:
    def test_adds_new_rule(self):
        result = add_rule([], rule_id="my-rule", condition="IF x", action_ref="mem_abc")
        assert len(result) == 1
        assert result[0]["id"] == "my-rule"
        assert result[0]["condition"] == "IF x"
        assert result[0]["action_ref"] == "mem_abc"

    def test_new_rule_enabled_by_default(self):
        result = add_rule([], rule_id="r", condition="c", action_ref="mem_r")
        assert result[0]["enabled"] is True

    def test_replaces_existing_rule_with_same_id(self):
        existing = make_rule("dup")
        result = add_rule(
            [existing], rule_id="dup", condition="new condition", action_ref="mem_new"
        )
        assert len(result) == 1
        assert result[0]["condition"] == "new condition"
        assert result[0]["action_ref"] == "mem_new"

    def test_replacement_preserves_list_position(self):
        rules = [make_rule("a"), make_rule("dup"), make_rule("b")]
        result = add_rule(rules, rule_id="dup", condition="updated", action_ref="mem_x")
        assert [r["id"] for r in result] == ["a", "dup", "b"]
        assert result[1]["condition"] == "updated"

    def test_drops_oldest_when_over_cap(self):
        # Fill to cap, then add one more — the oldest (head) rule should be dropped
        rules = [make_rule(f"r{i}") for i in range(5)]
        result = add_rule(rules, rule_id="new", condition="c", action_ref="mem_new", cap=5)
        assert len(result) == 5
        # "new" (newest) should be present
        assert any(r["id"] == "new" for r in result)
        # r0 (oldest) should be gone
        assert not any(r["id"] == "r0" for r in result)

    def test_no_drop_when_under_cap(self):
        rules = [make_rule(f"r{i}") for i in range(3)]
        result = add_rule(rules, rule_id="new", condition="c", action_ref="m", cap=10)
        assert len(result) == 4

    def test_no_drop_at_exact_cap_on_replace(self):
        # Replacing an existing rule should not change count
        rules = [make_rule(f"r{i}") for i in range(5)]
        result = add_rule(rules, rule_id="r0", condition="updated", action_ref="m", cap=5)
        assert len(result) == 5

    def test_returns_new_list(self):
        rules = [make_rule("a")]
        result = add_rule(rules, rule_id="b", condition="c", action_ref="m")
        assert result is not rules


# ---------------------------------------------------------------------------
# remove_rule
# ---------------------------------------------------------------------------


class TestRemoveRule:
    def test_removes_existing_rule(self):
        rules = [make_rule("a"), make_rule("b")]
        result = remove_rule(rules, "a")
        assert len(result) == 1
        assert result[0]["id"] == "b"

    def test_noop_on_missing_rule(self):
        rules = [make_rule("a")]
        result = remove_rule(rules, "nonexistent")
        assert len(result) == 1

    def test_returns_new_list(self):
        rules = [make_rule("a")]
        result = remove_rule(rules, "nonexistent")
        assert result is not rules

    def test_empty_list(self):
        assert remove_rule([], "x") == []


# ---------------------------------------------------------------------------
# get_enabled_rules
# ---------------------------------------------------------------------------


class TestGetEnabledRules:
    def test_filters_disabled_rules(self):
        rules = [make_rule("a", enabled=True), make_rule("b", enabled=False)]
        result = get_enabled_rules(rules)
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_includes_rules_without_enabled_key(self):
        rule = make_rule("a")
        del rule["enabled"]
        result = get_enabled_rules([rule])
        assert len(result) == 1

    def test_empty_list(self):
        assert get_enabled_rules([]) == []

    def test_all_disabled(self):
        rules = [make_rule("a", enabled=False), make_rule("b", enabled=False)]
        assert get_enabled_rules(rules) == []


# ---------------------------------------------------------------------------
# find_rule
# ---------------------------------------------------------------------------


class TestFindRule:
    def test_finds_existing_rule(self):
        rules = [make_rule("a"), make_rule("b")]
        result = find_rule(rules, "b")
        assert result is not None
        assert result["id"] == "b"

    def test_returns_none_on_missing(self):
        rules = [make_rule("a")]
        assert find_rule(rules, "nonexistent") is None

    def test_empty_list(self):
        assert find_rule([], "x") is None


# ---------------------------------------------------------------------------
# format_rules_for_context
# ---------------------------------------------------------------------------


class TestFormatRulesForContext:
    def test_formats_enabled_rules_with_action_ref(self):
        rules = [make_rule("check-cal", action_ref="mem_abc123")]
        rules[0]["condition"] = "user mentions meeting"
        output = format_rules_for_context(rules)
        assert "[check-cal] IF user mentions meeting THEN lookup mem_abc123 in memory DB" in output

    def test_omits_disabled_rules(self):
        rules = [make_rule("a", enabled=False)]
        output = format_rules_for_context(rules)
        assert output == ""

    def test_empty_list_returns_empty_string(self):
        assert format_rules_for_context([]) == ""

    def test_multiple_rules_on_separate_lines(self):
        rules = [make_rule("a"), make_rule("b")]
        output = format_rules_for_context(rules)
        lines = output.strip().split("\n")
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# load_rules (I/O)
# ---------------------------------------------------------------------------


class TestLoadRules:
    def test_returns_empty_list_when_file_absent(self, tmp_path):
        result = load_rules(tmp_path / "nonexistent.yaml")
        assert result == []

    def test_returns_empty_list_on_malformed_yaml(self, tmp_path):
        bad_file = tmp_path / "rules.yaml"
        bad_file.write_text("}{{{ not yaml", encoding="utf-8")
        result = load_rules(bad_file)
        assert result == []

    def test_returns_empty_list_when_rules_key_missing(self, tmp_path):
        f = tmp_path / "rules.yaml"
        f.write_text("version: 1\n", encoding="utf-8")
        result = load_rules(f)
        assert result == []

    def test_skips_entries_missing_required_keys(self, tmp_path):
        f = tmp_path / "rules.yaml"
        # First entry valid (has id/condition/action_ref), second missing action_ref
        f.write_text(
            "version: 1\nrules:\n"
            "  - id: ok\n    condition: c\n    action_ref: mem_ok\n"
            "  - id: bad\n    condition: c\n",
            encoding="utf-8",
        )
        result = load_rules(f)
        assert len(result) == 1
        assert result[0]["id"] == "ok"

    def test_loads_valid_file(self, tmp_path):
        f = tmp_path / "rules.yaml"
        data = {
            "version": 1,
            "rules": [
                {
                    "id": "r1",
                    "condition": "user asks about meeting",
                    "action_ref": "mem_r1",
                    "enabled": True,
                }
            ],
        }
        f.write_text(yaml.dump(data), encoding="utf-8")
        result = load_rules(f)
        assert len(result) == 1
        assert result[0]["id"] == "r1"
        assert result[0]["action_ref"] == "mem_r1"

    def test_returns_empty_list_when_rules_is_not_list(self, tmp_path):
        f = tmp_path / "rules.yaml"
        f.write_text("version: 1\nrules: not-a-list\n", encoding="utf-8")
        result = load_rules(f)
        assert result == []

    def test_returns_empty_list_when_top_level_not_dict(self, tmp_path):
        f = tmp_path / "rules.yaml"
        f.write_text("- just a list\n", encoding="utf-8")
        result = load_rules(f)
        assert result == []


# ---------------------------------------------------------------------------
# save_rules (I/O)
# ---------------------------------------------------------------------------


class TestSaveRules:
    def test_round_trip(self, tmp_path):
        f = tmp_path / "rules.yaml"
        rules = [
            {
                "id": "r1",
                "condition": "user asks about calendar",
                "action_ref": "mem_r1",
                "enabled": True,
            }
        ]
        save_rules(rules, path=f)
        loaded = load_rules(f)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "r1"
        assert loaded[0]["action_ref"] == "mem_r1"

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "rules.yaml"
        save_rules([], path=nested)
        assert nested.exists()

    def test_writes_version_1(self, tmp_path):
        f = tmp_path / "rules.yaml"
        save_rules([], path=f)
        data = yaml.safe_load(f.read_text())
        assert data["version"] == 1

    def test_caps_before_writing(self, tmp_path):
        f = tmp_path / "rules.yaml"
        rules = [make_rule(f"r{i}") for i in range(10)]
        save_rules(rules, path=f, cap=5)
        loaded = load_rules(f)
        assert len(loaded) == 5
        # Drops oldest (head): last 5 rules are kept
        assert loaded[0]["id"] == "r5"
        assert loaded[4]["id"] == "r9"

    def test_atomic_write_leaves_no_tmp_files(self, tmp_path):
        f = tmp_path / "rules.yaml"
        save_rules([make_rule("r")], path=f)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_empty_rules_write_and_reload(self, tmp_path):
        f = tmp_path / "rules.yaml"
        save_rules([], path=f)
        loaded = load_rules(f)
        assert loaded == []
