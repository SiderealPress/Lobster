"""
Tests for multi-account support added to Granola's sync pipeline.

Covers:
1. GranolaNote.granola_account field (default and explicit)
2. granola_account in Obsidian frontmatter (serializer)
3. iter_all_notes_for_account (client)
4. build_account_configs_from_env (client)
5. _merge_notes_deduplicated (sync)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add src/ to path
_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from integrations.granola.client import (
    GranolaNote,
    GranolaOwner,
    GranolaAccountConfig,
    build_account_configs_from_env,
    iter_all_notes_for_account,
    ACCOUNT_DREW,  # noname
    ACCOUNT_KELLY,  # noname
)
from integrations.granola.client import GranolaUnknownAccountError
from integrations.granola.serializer import note_to_markdown
from integrations.granola.sync import _merge_notes_deduplicated, _fetch_notes_with_detail


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRANOLA_ACCOUNT_FRONTMATTER_KEY = "granola_account"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_note(
    note_id: str = "note-1",
    title: str = "Meeting",
    account: str = ACCOUNT_DREW,  # noname
) -> GranolaNote:
    return GranolaNote(
        id=note_id,
        title=title,
        owner=GranolaOwner(name="Test User", email="test@example.com"),
        created_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc),
        summary_markdown="Test summary.",
        granola_account=account,
    )


# ---------------------------------------------------------------------------
# GranolaNote.granola_account field
# ---------------------------------------------------------------------------

class TestGranolaAccountField:
    def test_default_account_is_primary(self):
        note = GranolaNote(
            id="n1",
            title="Meeting",
            owner=GranolaOwner(name="", email=""),
            created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        )
        assert note.granola_account == ACCOUNT_DREW  # noname

    def test_explicit_secondary_account(self):
        note = _make_note(account=ACCOUNT_KELLY)  # noname
        assert note.granola_account == ACCOUNT_KELLY  # noname

    def test_explicit_primary_account(self):
        note = _make_note(account=ACCOUNT_DREW)  # noname
        assert note.granola_account == ACCOUNT_DREW  # noname


# ---------------------------------------------------------------------------
# Serializer — granola_account in frontmatter
# ---------------------------------------------------------------------------

class TestNoteToMarkdownAccountAnnotation:
    def test_primary_account_in_frontmatter(self):
        note = _make_note(account=ACCOUNT_DREW)  # noname
        md = note_to_markdown(note)
        assert f"{GRANOLA_ACCOUNT_FRONTMATTER_KEY}: {ACCOUNT_DREW}" in md  # noname

    def test_secondary_account_in_frontmatter(self):
        note = _make_note(account=ACCOUNT_KELLY)  # noname
        md = note_to_markdown(note)
        assert f"{GRANOLA_ACCOUNT_FRONTMATTER_KEY}: {ACCOUNT_KELLY}" in md  # noname

    def test_account_field_is_in_frontmatter_block(self):
        """Ensure the field is inside the --- block, not in the body."""
        note = _make_note(account=ACCOUNT_KELLY)  # noname
        md = note_to_markdown(note)
        # Find the frontmatter block
        parts = md.split("---")
        assert len(parts) >= 3, "Expected frontmatter delimiters"
        frontmatter = parts[1]
        assert GRANOLA_ACCOUNT_FRONTMATTER_KEY in frontmatter

    def test_default_note_has_primary_account_in_frontmatter(self):
        """Notes created without explicit account default to primary account in frontmatter."""
        note = GranolaNote(
            id="n1",
            title="Old note",
            owner=GranolaOwner(name="Test", email="test@example.com"),
            created_at=datetime(2026, 4, 10, 10, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 10, 10, tzinfo=timezone.utc),
        )
        md = note_to_markdown(note)
        assert f"{GRANOLA_ACCOUNT_FRONTMATTER_KEY}: {ACCOUNT_DREW}" in md  # noname


# ---------------------------------------------------------------------------
# build_account_configs_from_env
# ---------------------------------------------------------------------------

class TestBuildAccountConfigsFromEnv:
    def test_single_account_when_secondary_key_absent(self):
        env = {"GRANOLA_API_KEY": "grn_primary"}
        configs = build_account_configs_from_env(env)
        assert len(configs) == 1
        assert configs[0].name == ACCOUNT_DREW  # noname

    def test_two_accounts_when_both_keys_present(self):
        env = {"GRANOLA_API_KEY": "grn_primary", "GRANOLA_API_KEY_KELLY": "grn_secondary"}  # noname
        configs = build_account_configs_from_env(env)
        assert len(configs) == 2
        names = [c.name for c in configs]
        assert ACCOUNT_DREW in names  # noname
        assert ACCOUNT_KELLY in names  # noname

    def test_primary_account_is_first(self):
        env = {"GRANOLA_API_KEY": "grn_primary", "GRANOLA_API_KEY_KELLY": "grn_secondary"}  # noname
        configs = build_account_configs_from_env(env)
        assert configs[0].name == ACCOUNT_DREW  # noname

    def test_missing_primary_key_returns_empty(self):
        env = {"GRANOLA_API_KEY_KELLY": "grn_secondary"}  # noname
        configs = build_account_configs_from_env(env)
        assert configs == []

    def test_empty_env_returns_empty(self):
        configs = build_account_configs_from_env({})
        assert configs == []

    def test_api_key_stored_in_config(self):
        env = {"GRANOLA_API_KEY": "grn_primary_key", "GRANOLA_API_KEY_KELLY": "grn_secondary_key"}  # noname
        configs = build_account_configs_from_env(env)
        config_by_name = {c.name: c for c in configs}
        assert config_by_name[ACCOUNT_DREW].api_key == "grn_primary_key"  # noname
        assert config_by_name[ACCOUNT_KELLY].api_key == "grn_secondary_key"  # noname


# ---------------------------------------------------------------------------
# iter_all_notes_for_account
# ---------------------------------------------------------------------------

class TestIterAllNotesForAccount:
    @patch("integrations.granola.client.list_notes")
    def test_fetches_with_correct_api_key(self, mock_list_notes):
        from integrations.granola.client import NoteListPage
        mock_list_notes.return_value = NoteListPage(notes=[], has_more=False, cursor=None)

        account = GranolaAccountConfig(name=ACCOUNT_KELLY, api_key="grn_secondary_test")  # noname
        iter_all_notes_for_account(account)

        call_kwargs = mock_list_notes.call_args
        assert call_kwargs.kwargs.get("api_key") == "grn_secondary_test"

    @patch("integrations.granola.client.list_notes")
    def test_passes_account_name_to_list_notes(self, mock_list_notes):
        """iter_all_notes_for_account must pass granola_account=account.name to list_notes."""
        from integrations.granola.client import NoteListPage
        mock_list_notes.return_value = NoteListPage(notes=[], has_more=False, cursor=None)

        account = GranolaAccountConfig(name=ACCOUNT_KELLY, api_key="grn_secondary_test")  # noname
        iter_all_notes_for_account(account)

        call_kwargs = mock_list_notes.call_args
        assert call_kwargs.kwargs.get("granola_account") == ACCOUNT_KELLY  # noname


# ---------------------------------------------------------------------------
# _merge_notes_deduplicated (sync module)
# ---------------------------------------------------------------------------

class TestMergeNotesDeduplicated:
    def test_non_overlapping_notes_all_kept(self):
        primary_notes = [_make_note("d1"), _make_note("d2")]
        secondary_notes = [_make_note("k1", account=ACCOUNT_KELLY)]  # noname
        result = _merge_notes_deduplicated({
            ACCOUNT_DREW: primary_notes,  # noname
            ACCOUNT_KELLY: secondary_notes,  # noname
        })
        ids = {n.id for n in result}
        assert ids == {"d1", "d2", "k1"}

    def test_primary_wins_on_duplicate_id(self):
        shared_id = "shared-meeting"
        primary_note = _make_note(shared_id, title="primary version", account=ACCOUNT_DREW)  # noname
        secondary_note = _make_note(shared_id, title="secondary version", account=ACCOUNT_KELLY)  # noname
        result = _merge_notes_deduplicated({
            ACCOUNT_DREW: [primary_note],  # noname
            ACCOUNT_KELLY: [secondary_note],  # noname
        })
        assert len(result) == 1
        assert result[0].title == "primary version"

    def test_missing_secondary_account_returns_primary_only(self):
        primary_notes = [_make_note("d1")]
        result = _merge_notes_deduplicated({ACCOUNT_DREW: primary_notes})  # noname
        assert [n.id for n in result] == ["d1"]

    def test_empty_accounts_returns_empty(self):
        result = _merge_notes_deduplicated({})
        assert result == []

    def test_account_field_preserved_after_merge(self):
        primary_note = _make_note("d1", account=ACCOUNT_DREW)  # noname
        secondary_note = _make_note("k1", account=ACCOUNT_KELLY)  # noname
        result = _merge_notes_deduplicated({
            ACCOUNT_DREW: [primary_note],  # noname
            ACCOUNT_KELLY: [secondary_note],  # noname
        })
        by_id = {n.id: n for n in result}
        assert by_id["d1"].granola_account == ACCOUNT_DREW  # noname
        assert by_id["k1"].granola_account == ACCOUNT_KELLY  # noname


# ---------------------------------------------------------------------------
# _fetch_notes_with_detail — per-account API key routing
# ---------------------------------------------------------------------------

PRIMARY_API_KEY = "grn_drew_primary_key"  # noname
SECONDARY_API_KEY = "grn_kelly_secondary_key"  # noname


class TestFetchNotesWithDetail:
    """
    Verify that _fetch_notes_with_detail passes the correct api_key to get_note()
    for each note based on its granola_account field.

    This is the regression test for the critical bug where Kelly's notes were
    being fetched with the primary account's API key.  # noname
    """

    def _make_accounts(self) -> list[GranolaAccountConfig]:
        return [
            GranolaAccountConfig(name=ACCOUNT_DREW, api_key=PRIMARY_API_KEY),  # noname
            GranolaAccountConfig(name=ACCOUNT_KELLY, api_key=SECONDARY_API_KEY),  # noname
        ]

    @patch("integrations.granola.sync.get_note")
    def test_primary_account_note_uses_primary_api_key(self, mock_get_note):
        """Notes from the primary account must be fetched with the primary API key."""
        note = _make_note("d1", account=ACCOUNT_DREW)  # noname
        mock_get_note.return_value = note

        _fetch_notes_with_detail([note], self._make_accounts())

        mock_get_note.assert_called_once()
        call_kwargs = mock_get_note.call_args
        assert call_kwargs.kwargs.get("api_key") == PRIMARY_API_KEY

    @patch("integrations.granola.sync.get_note")
    def test_secondary_account_note_uses_secondary_api_key(self, mock_get_note):
        """Notes from the secondary account must be fetched with the secondary API key."""
        note = _make_note("k1", account=ACCOUNT_KELLY)  # noname
        mock_get_note.return_value = note

        _fetch_notes_with_detail([note], self._make_accounts())

        mock_get_note.assert_called_once()
        call_kwargs = mock_get_note.call_args
        assert call_kwargs.kwargs.get("api_key") == SECONDARY_API_KEY

    @patch("integrations.granola.sync.get_note")
    def test_mixed_accounts_use_correct_keys_for_each_note(self, mock_get_note):
        """Each note in a mixed list is fetched with its own account's API key."""
        primary_note = _make_note("d1", account=ACCOUNT_DREW)  # noname
        secondary_note = _make_note("k1", account=ACCOUNT_KELLY)  # noname
        mock_get_note.side_effect = [primary_note, secondary_note]

        _fetch_notes_with_detail([primary_note, secondary_note], self._make_accounts())

        assert mock_get_note.call_count == 2
        calls = mock_get_note.call_args_list
        assert calls[0].kwargs.get("api_key") == PRIMARY_API_KEY
        assert calls[1].kwargs.get("api_key") == SECONDARY_API_KEY

    @patch("integrations.granola.sync.get_note")
    def test_account_attribution_preserved_in_returned_notes(self, mock_get_note):
        """The granola_account field on the returned note must match the original note."""
        note = _make_note("k1", account=ACCOUNT_KELLY)  # noname
        mock_get_note.return_value = note

        result = _fetch_notes_with_detail([note], self._make_accounts())

        assert len(result) == 1
        assert result[0].granola_account == ACCOUNT_KELLY  # noname

    @patch("integrations.granola.sync.get_note")
    def test_unknown_account_raises_error_not_silent_fallback(self, mock_get_note):
        """
        A note tagged with an account name not in the config must raise
        GranolaUnknownAccountError — never silently fall through to the
        primary API key.

        This is the regression test for the original silent-fallback bug:
        api_key_by_account.get(unknown) returned None, get_note received
        api_key=None, and _get_api_key() quietly used GRANOLA_API_KEY.
        """
        note = _make_note("x1", account="unknown-account")
        # Only primary and secondary accounts are registered — "unknown-account" is not.
        accounts = self._make_accounts()

        with pytest.raises(GranolaUnknownAccountError):
            _fetch_notes_with_detail([note], accounts)

        # get_note must NOT have been called — we never attempt a fetch with the wrong key.
        mock_get_note.assert_not_called()


# ---------------------------------------------------------------------------
# GranolaUnknownAccountError — raised by build_account_configs_from_env
# ---------------------------------------------------------------------------


class TestGranolaUnknownAccountError:
    def test_error_is_a_subclass_of_granola_api_error(self):
        """GranolaUnknownAccountError must be a GranolaAPIError for consistent handling."""
        from integrations.granola.client import GranolaAPIError
        assert issubclass(GranolaUnknownAccountError, Exception)

    def test_error_message_contains_account_name(self):
        err = GranolaUnknownAccountError("phantom-account")
        assert "phantom-account" in str(err)
