"""
Tests for src/utils/ifttt_rules.py — IFTTT-style behavioral rules store.

Covers:
- prune_lru: LRU ordering, cap enforcement, tie-breaking by access_count
- add_rule: append new rule, replace existing rule, cap enforcement on add
- remove_rule: remove existing, no-op on missing
- touch_rule: access count increment, timestamp update, no-op on missing
- get_enabled_rules: filters disabled rules
- find_rule: lookup by ID
- format_rules_for_context: plain-text rendering, empty case
- load_rules: missing file, malformed YAML, missing keys, valid file
- save_rules: round-trip, atomic write, LRU pruning before write
"""

import os
import tempfile
from datetime import datetime, timezone
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
    prune_lru,
    remove_rule,
    save_rules,
    touch_rule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_rule(
    rule_id: str,
    last_accessed_at: str = "2026-01-01T00:00:00Z",
    access_count: int = 0,
    enabled: bool = True,
) -> dict:
    return {
        "id": rule_id,
        "trigger": f"trigger for {rule_id}",
        "action": f"action for {rule_id}",
        "created_at": "2026-01-01T00:00:00Z",
        "last_accessed_at": last_accessed_at,
        "access_count": access_count,
        "source": "lobster",
        "enabled": enabled,
        "notes": None,
    }


# ---------------------------------------------------------------------------
# prune_lru
# ---------------------------------------------------------------------------


class TestPruneLru:
    def test_no_pruning_when_under_cap(self):
        rules = [make_rule(f"rule-{i}") for i in range(5)]
        result = prune_lru(rules, cap=10)
        assert len(result) == 5

    def test_no_pruning_when_at_cap(self):
        rules = [make_rule(f"rule-{i}") for i in range(10)]
        result = prune_lru(rules, cap=10)
        assert len(result) == 10

    def test_prunes_oldest_accessed(self):
        # rule-old was accessed 2020, rule-new was accessed 2026 — rule-old should go
        old = make_rule("rule-old", last_accessed_at="2020-01-01T00:00:00Z")
        new = make_rule("rule-new", last_accessed_at="2026-01-01T00:00:00Z")
        result = prune_lru([old, new], cap=1)
        assert len(result) == 1
        assert result[0]["id"] == "rule-new"

    def test_breaks_ties_by_access_count(self):
        # Same timestamp, different access counts — lower access_count is pruned first
        low = make_rule("low-count", last_accessed_at="2026-01-01T00:00:00Z", access_count=1)
        high = make_rule("high-count", last_accessed_at="2026-01-01T00:00:00Z", access_count=10)
        result = prune_lru([low, high], cap=1)
        assert result[0]["id"] == "high-count"

    def test_preserves_original_order_of_survivors(self):
        # Survivors should appear in original list order, not sorted order
        rules = [
            make_rule("c", last_accessed_at="2026-03-01T00:00:00Z"),
            make_rule("b", last_accessed_at="2026-02-01T00:00:00Z"),
            make_rule("a", last_accessed_at="2020-01-01T00:00:00Z"),  # oldest → pruned
        ]
        result = prune_lru(rules, cap=2)
        assert [r["id"] for r in result] == ["c", "b"]

    def test_returns_new_list(self):
        rules = [make_rule("x")]
        result = prune_lru(rules, cap=10)
        assert result is not rules

    def test_empty_list(self):
        assert prune_lru([], cap=5) == []

    def test_prunes_to_exact_cap(self):
        rules = [make_rule(f"r{i}", last_accessed_at=f"2026-0{i+1}-01T00:00:00Z") for i in range(5)]
        result = prune_lru(rules, cap=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# add_rule
# ---------------------------------------------------------------------------


class TestAddRule:
    def test_adds_new_rule(self):
        result = add_rule([], rule_id="my-rule", trigger="IF x", action="THEN y")
        assert len(result) == 1
        assert result[0]["id"] == "my-rule"
        assert result[0]["trigger"] == "IF x"
        assert result[0]["action"] == "THEN y"

    def test_new_rule_has_correct_defaults(self):
        result = add_rule([], rule_id="r", trigger="t", action="a")
        rule = result[0]
        assert rule["access_count"] == 0
        assert rule["enabled"] is True
        assert rule["source"] == "lobster"
        assert rule["notes"] is None
        assert "created_at" in rule
        assert "last_accessed_at" in rule

    def test_replaces_existing_rule_with_same_id(self):
        existing = make_rule("dup")
        result = add_rule([existing], rule_id="dup", trigger="new trigger", action="new action")
        assert len(result) == 1
        assert result[0]["trigger"] == "new trigger"

    def test_replacement_resets_metadata(self):
        # When replaced, the rule gets fresh created_at and access_count=0
        existing = make_rule("dup", access_count=99)
        result = add_rule([existing], rule_id="dup", trigger="t", action="a")
        assert result[0]["access_count"] == 0

    def test_prunes_when_over_cap(self):
        # Fill to cap, then add one more — oldest should be pruned
        rules = [
            make_rule(f"r{i}", last_accessed_at=f"2026-0{(i % 9) + 1}-01T00:00:00Z")
            for i in range(5)
        ]
        result = add_rule(rules, rule_id="new", trigger="t", action="a", cap=5)
        assert len(result) == 5
        assert any(r["id"] == "new" for r in result)

    def test_custom_source(self):
        result = add_rule([], rule_id="r", trigger="t", action="a", source="user")
        assert result[0]["source"] == "user"

    def test_custom_notes(self):
        result = add_rule([], rule_id="r", trigger="t", action="a", notes="hello")
        assert result[0]["notes"] == "hello"


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
# touch_rule
# ---------------------------------------------------------------------------


class TestTouchRule:
    def test_increments_access_count(self):
        rules = [make_rule("r", access_count=3)]
        result = touch_rule(rules, "r")
        assert result[0]["access_count"] == 4

    def test_updates_last_accessed_at(self):
        rules = [make_rule("r", last_accessed_at="2020-01-01T00:00:00Z")]
        result = touch_rule(rules, "r")
        assert result[0]["last_accessed_at"] != "2020-01-01T00:00:00Z"

    def test_noop_on_missing_id(self):
        rules = [make_rule("a", access_count=1)]
        result = touch_rule(rules, "nonexistent")
        assert result[0]["access_count"] == 1

    def test_does_not_touch_other_rules(self):
        rules = [make_rule("a", access_count=0), make_rule("b", access_count=0)]
        result = touch_rule(rules, "a")
        assert result[0]["access_count"] == 1
        assert result[1]["access_count"] == 0

    def test_returns_new_list(self):
        rules = [make_rule("a")]
        result = touch_rule(rules, "a")
        assert result is not rules

    def test_returns_new_rule_dict(self):
        rules = [make_rule("a")]
        result = touch_rule(rules, "a")
        assert result[0] is not rules[0]


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
    def test_formats_enabled_rules(self):
        rules = [make_rule("check-cal")]
        rules[0]["trigger"] = "user mentions meeting"
        rules[0]["action"] = "check calendar first"
        output = format_rules_for_context(rules)
        assert "[check-cal] IF user mentions meeting THEN check calendar first" in output

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
        f.write_text(
            "version: 1\nrules:\n  - id: ok\n    trigger: t\n    action: a\n"
            "  - id: no-action\n    trigger: t\n",
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
                    "trigger": "IF meeting",
                    "action": "check cal",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_accessed_at": "2026-01-01T00:00:00Z",
                    "access_count": 5,
                    "source": "lobster",
                    "enabled": True,
                    "notes": None,
                }
            ],
        }
        f.write_text(yaml.dump(data), encoding="utf-8")
        result = load_rules(f)
        assert len(result) == 1
        assert result[0]["id"] == "r1"
        assert result[0]["access_count"] == 5

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
                "trigger": "t",
                "action": "a",
                "created_at": "2026-01-01T00:00:00Z",
                "last_accessed_at": "2026-01-01T00:00:00Z",
                "access_count": 0,
                "source": "lobster",
                "enabled": True,
                "notes": None,
            }
        ]
        save_rules(rules, path=f)
        loaded = load_rules(f)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "r1"

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "rules.yaml"
        save_rules([], path=nested)
        assert nested.exists()

    def test_writes_version_1(self, tmp_path):
        f = tmp_path / "rules.yaml"
        save_rules([], path=f)
        data = yaml.safe_load(f.read_text())
        assert data["version"] == 1

    def test_prunes_before_writing(self, tmp_path):
        f = tmp_path / "rules.yaml"
        rules = [
            make_rule(f"r{i}", last_accessed_at=f"2026-0{(i % 9) + 1}-01T00:00:00Z")
            for i in range(10)
        ]
        save_rules(rules, path=f, cap=5)
        loaded = load_rules(f)
        assert len(loaded) == 5

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
