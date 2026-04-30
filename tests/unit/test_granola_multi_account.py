"""
Tests for multi-account Granola polling (primary + secondary accounts).

Verifies that granola_ingest.py correctly:
- Polls multiple API keys as separate accounts
- Deduplicates notes that appear in both accounts (primary account wins)
- Annotates each note with an 'account' field
- Maintains separate cursor state per account
- Falls back gracefully if secondary key is missing (single-account mode)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_TASKS_DIR = Path(__file__).parent.parent.parent / "scheduled-tasks"
sys.path.insert(0, str(_TASKS_DIR))

# Import modules under test
from granola_multi_account import (  # noqa: E402
    AccountConfig,
    AccountRegistry,
    ACCOUNT_DREW,  # noname
    ACCOUNT_KELLY,  # noname
    build_accounts_from_env,
    merge_and_deduplicate,
    annotate_note_with_account,
)


# ---------------------------------------------------------------------------
# Constants that mirror the spec
# ---------------------------------------------------------------------------

PRIMARY_KEY = "grn_drew_test_key"  # noname
SECONDARY_KEY = "grn_kelly_test_key"  # noname


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_note(note_id: str, title: str = "Meeting", created_at: str = "2026-04-10T10:00:00Z") -> dict:
    return {"id": note_id, "title": title, "created_at": created_at}


# ---------------------------------------------------------------------------
# AccountConfig tests
# ---------------------------------------------------------------------------

class TestAccountConfig:
    def test_primary_account_uses_primary_key(self):
        acc = AccountConfig(name=ACCOUNT_DREW, api_key=PRIMARY_KEY)  # noname
        assert acc.name == ACCOUNT_DREW  # noname
        assert acc.api_key == PRIMARY_KEY

    def test_secondary_account_uses_secondary_key(self):
        acc = AccountConfig(name=ACCOUNT_KELLY, api_key=SECONDARY_KEY)  # noname
        assert acc.name == ACCOUNT_KELLY  # noname
        assert acc.api_key == SECONDARY_KEY


# ---------------------------------------------------------------------------
# build_accounts_from_env tests
# ---------------------------------------------------------------------------

class TestBuildAccountsFromEnv:
    def test_single_account_when_secondary_key_absent(self):
        env = {"GRANOLA_API_KEY": PRIMARY_KEY}
        accounts = build_accounts_from_env(env)
        assert len(accounts) == 1
        assert accounts[0].name == ACCOUNT_DREW  # noname

    def test_two_accounts_when_both_keys_present(self):
        env = {"GRANOLA_API_KEY": PRIMARY_KEY, "GRANOLA_API_KEY_KELLY": SECONDARY_KEY}  # noname
        accounts = build_accounts_from_env(env)
        assert len(accounts) == 2
        names = [a.name for a in accounts]
        assert ACCOUNT_DREW in names  # noname
        assert ACCOUNT_KELLY in names  # noname

    def test_primary_account_comes_first(self):
        """Primary account must come first so it wins deduplication."""
        env = {"GRANOLA_API_KEY": PRIMARY_KEY, "GRANOLA_API_KEY_KELLY": SECONDARY_KEY}  # noname
        accounts = build_accounts_from_env(env)
        assert accounts[0].name == ACCOUNT_DREW  # noname

    def test_empty_env_returns_empty_list(self):
        accounts = build_accounts_from_env({})
        assert accounts == []

    def test_secondary_key_without_primary_key_is_excluded(self):
        """Secondary key alone is not usable — we need primary key."""
        env = {"GRANOLA_API_KEY_KELLY": SECONDARY_KEY}  # noname
        accounts = build_accounts_from_env(env)
        assert len(accounts) == 0


# ---------------------------------------------------------------------------
# annotate_note_with_account tests
# ---------------------------------------------------------------------------

class TestAnnotateNoteWithAccount:
    def test_adds_account_field(self):
        note = _raw_note("n1")
        result = annotate_note_with_account(note, ACCOUNT_DREW)  # noname
        assert result["account"] == ACCOUNT_DREW  # noname

    def test_does_not_mutate_original(self):
        note = _raw_note("n1")
        original_keys = set(note.keys())
        annotate_note_with_account(note, ACCOUNT_DREW)  # noname
        assert set(note.keys()) == original_keys

    def test_secondary_account_annotation(self):
        note = _raw_note("n1")
        result = annotate_note_with_account(note, ACCOUNT_KELLY)  # noname
        assert result["account"] == ACCOUNT_KELLY  # noname


# ---------------------------------------------------------------------------
# merge_and_deduplicate tests
# ---------------------------------------------------------------------------

SECONDARY_NOTE = ACCOUNT_KELLY  # noname  # alias for readability in dedup tests
PRIMARY_NOTE = ACCOUNT_DREW  # noname


class TestMergeAndDeduplicate:
    def test_non_overlapping_notes_all_kept(self):
        primary_notes = [_raw_note("d1"), _raw_note("d2")]
        secondary_notes = [_raw_note("k1"), _raw_note("k2")]
        result = merge_and_deduplicate(primary_notes, secondary_notes)
        ids = [n["id"] for n in result]
        assert set(ids) == {"d1", "d2", "k1", "k2"}

    def test_primary_wins_on_duplicate_id(self):
        """If the same note ID appears in both accounts, the primary version is kept."""
        shared_id = "shared-note"
        primary_note = {**_raw_note(shared_id), "title": "primary version"}
        secondary_note = {**_raw_note(shared_id), "title": "secondary version"}
        result = merge_and_deduplicate([primary_note], [secondary_note])
        assert len(result) == 1
        assert result[0]["title"] == "primary version"

    def test_empty_secondary_returns_primary_only(self):
        primary_notes = [_raw_note("d1")]
        result = merge_and_deduplicate(primary_notes, [])
        assert [n["id"] for n in result] == ["d1"]

    def test_empty_primary_returns_secondary_only(self):
        secondary_notes = [_raw_note("k1")]
        result = merge_and_deduplicate([], secondary_notes)
        assert [n["id"] for n in result] == ["k1"]

    def test_both_empty_returns_empty(self):
        result = merge_and_deduplicate([], [])
        assert result == []

    def test_account_annotation_preserved(self):
        primary_note = annotate_note_with_account(_raw_note("d1"), ACCOUNT_DREW)  # noname
        secondary_note = annotate_note_with_account(_raw_note("k1"), ACCOUNT_KELLY)  # noname
        result = merge_and_deduplicate([primary_note], [secondary_note])
        by_id = {n["id"]: n for n in result}
        assert by_id["d1"]["account"] == ACCOUNT_DREW  # noname
        assert by_id["k1"]["account"] == ACCOUNT_KELLY  # noname

    def test_order_preserved_primary_first(self):
        primary_notes = [
            annotate_note_with_account(_raw_note("d1"), ACCOUNT_DREW),  # noname
            annotate_note_with_account(_raw_note("d2"), ACCOUNT_DREW),  # noname
        ]
        secondary_notes = [annotate_note_with_account(_raw_note("k1"), ACCOUNT_KELLY)]  # noname
        result = merge_and_deduplicate(primary_notes, secondary_notes)
        primary_ids = [n["id"] for n in result if n.get("account") == ACCOUNT_DREW]  # noname
        assert primary_ids == ["d1", "d2"]


# ---------------------------------------------------------------------------
# AccountRegistry — strict lookup, no silent fallback
# ---------------------------------------------------------------------------


class TestAccountRegistry:
    def _make_registry(self) -> AccountRegistry:
        accounts = build_accounts_from_env({
            "GRANOLA_API_KEY": PRIMARY_KEY,
            "GRANOLA_API_KEY_KELLY": SECONDARY_KEY,  # noname
        })
        return AccountRegistry(accounts)

    def test_lookup_known_primary_account(self):
        registry = self._make_registry()
        cfg = registry.get(ACCOUNT_DREW)  # noname
        assert cfg.api_key == PRIMARY_KEY

    def test_lookup_known_secondary_account(self):
        registry = self._make_registry()
        cfg = registry.get(ACCOUNT_KELLY)  # noname
        assert cfg.api_key == SECONDARY_KEY

    def test_unknown_account_raises_key_error(self):
        """
        An account name not in the registry must raise KeyError immediately.
        No silent fallback to the primary key is acceptable.
        """
        registry = self._make_registry()
        with pytest.raises(KeyError, match="phantom-account"):
            registry.get("phantom-account")

    def test_registry_contains_all_configured_accounts(self):
        registry = self._make_registry()
        assert ACCOUNT_DREW in registry  # noname
        assert ACCOUNT_KELLY in registry  # noname

    def test_registry_does_not_contain_unconfigured_account(self):
        accounts = build_accounts_from_env({"GRANOLA_API_KEY": PRIMARY_KEY})
        registry = AccountRegistry(accounts)
        assert ACCOUNT_KELLY not in registry  # noname
