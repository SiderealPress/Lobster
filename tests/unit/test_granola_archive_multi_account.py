"""
Tests for multi-account support in granola_archive.py.

Covers the two behaviors called out in the PR review:
1. _dedup_notes: deduplication by note ID (primary-first order wins)
2. _run_archive key routing: correct API key per account, unknown account raises error

These tests import granola_archive directly from scheduled-tasks/ by adding
that directory to sys.path — the same pattern used by test_granola_multi_account.py.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add scheduled-tasks/ to path so granola_archive is importable directly
_TASKS_DIR = Path(__file__).parent.parent.parent / "scheduled-tasks"
_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_TASKS_DIR))
sys.path.insert(0, str(_SRC_DIR))

from granola_archive import _dedup_notes, _run_archive  # noqa: E402
from integrations.granola.client import (  # noqa: E402
    GranolaNote,
    GranolaOwner,
    GranolaAccountConfig,
    GranolaUnknownAccountError,
    ACCOUNT_DREW,  # noname
    ACCOUNT_KELLY,  # noname
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRIMARY_API_KEY = "grn_archive_primary_key"
SECONDARY_API_KEY = "grn_archive_secondary_key"


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
# _dedup_notes
# ---------------------------------------------------------------------------


class TestDedupNotes:
    def test_unique_notes_all_kept(self):
        """Notes with distinct IDs all survive deduplication."""
        notes = [
            _make_note("n1", account=ACCOUNT_DREW),   # noname
            _make_note("n2", account=ACCOUNT_KELLY),  # noname
            _make_note("n3", account=ACCOUNT_DREW),   # noname
        ]
        result = _dedup_notes(notes)
        assert [n.id for n in result] == ["n1", "n2", "n3"]

    def test_same_id_in_both_accounts_yields_single_entry(self):
        """When primary and secondary share a note ID, only one entry is in the output."""
        primary_note = _make_note("shared-id", title="primary version", account=ACCOUNT_DREW)   # noname
        secondary_note = _make_note("shared-id", title="secondary version", account=ACCOUNT_KELLY)  # noname
        # Primary is prepended first (mirrors _run_archive ordering)
        result = _dedup_notes([primary_note, secondary_note])
        assert len(result) == 1

    def test_primary_wins_on_duplicate_id(self):
        """When same ID exists in both accounts, the first occurrence (primary) is kept."""
        primary_note = _make_note("shared-id", title="primary version", account=ACCOUNT_DREW)   # noname
        secondary_note = _make_note("shared-id", title="secondary version", account=ACCOUNT_KELLY)  # noname
        result = _dedup_notes([primary_note, secondary_note])
        assert result[0].title == "primary version"

    def test_empty_list_returns_empty(self):
        assert _dedup_notes([]) == []

    def test_single_note_returned_unchanged(self):
        note = _make_note("n1")
        result = _dedup_notes([note])
        assert result == [note]

    def test_order_preserved_for_unique_notes(self):
        """Original insertion order is preserved when no duplicates exist."""
        notes = [_make_note(f"n{i}") for i in range(5)]
        result = _dedup_notes(notes)
        assert [n.id for n in result] == ["n0", "n1", "n2", "n3", "n4"]

    def test_many_duplicates_reduced_to_one(self):
        """Multiple occurrences of the same ID all collapse to the first one."""
        notes = [_make_note("dup", title=f"copy-{i}") for i in range(4)]
        result = _dedup_notes(notes)
        assert len(result) == 1
        assert result[0].title == "copy-0"


# ---------------------------------------------------------------------------
# _run_archive: key routing and unknown-account guard
# ---------------------------------------------------------------------------


class TestRunArchiveKeyRouting:
    """
    Verify that _run_archive passes the correct per-account API key to
    _fetch_full_note, and raises GranolaUnknownAccountError before any
    network call when a note carries an unregistered account name.

    Strategy: patch the three I/O-touching functions so the test is purely
    about control flow and argument routing, not file system or network state.
    """

    def _make_accounts(self) -> list[GranolaAccountConfig]:
        return [
            GranolaAccountConfig(name=ACCOUNT_DREW, api_key=PRIMARY_API_KEY),    # noname
            GranolaAccountConfig(name=ACCOUNT_KELLY, api_key=SECONDARY_API_KEY), # noname
        ]

    @patch("granola_archive._save_index")
    @patch("granola_archive._archive_note")
    @patch("granola_archive._fetch_full_note")
    @patch("granola_archive.iter_all_notes_for_account")
    @patch("granola_archive.build_account_configs_from_env")
    def test_primary_account_note_uses_primary_api_key(
        self,
        mock_build_accounts,
        mock_iter_notes,
        mock_fetch_full,
        mock_archive_note,
        mock_save_index,
    ):
        """Notes from the primary account are fetched with the primary API key."""
        note = _make_note("d1", account=ACCOUNT_DREW)  # noname
        mock_build_accounts.return_value = self._make_accounts()
        mock_iter_notes.return_value = [note]
        mock_fetch_full.return_value = note
        mock_archive_note.return_value = (True, [], "2026-04-10_10-00_meeting.md")

        log = MagicMock()
        _run_archive(backfill=True, log=log)

        mock_fetch_full.assert_called_once()
        _, kwargs = mock_fetch_full.call_args
        assert kwargs["api_key"] == PRIMARY_API_KEY

    @patch("granola_archive._save_index")
    @patch("granola_archive._archive_note")
    @patch("granola_archive._fetch_full_note")
    @patch("granola_archive.iter_all_notes_for_account")
    @patch("granola_archive.build_account_configs_from_env")
    def test_secondary_account_note_uses_secondary_api_key(
        self,
        mock_build_accounts,
        mock_iter_notes,
        mock_fetch_full,
        mock_archive_note,
        mock_save_index,
    ):
        """Notes from the secondary account are fetched with the secondary API key."""
        note = _make_note("k1", account=ACCOUNT_KELLY)  # noname
        mock_build_accounts.return_value = self._make_accounts()
        mock_iter_notes.return_value = [note]
        mock_fetch_full.return_value = note
        mock_archive_note.return_value = (True, [], "2026-04-10_10-00_meeting.md")

        log = MagicMock()
        _run_archive(backfill=True, log=log)

        mock_fetch_full.assert_called_once()
        _, kwargs = mock_fetch_full.call_args
        assert kwargs["api_key"] == SECONDARY_API_KEY

    @patch("granola_archive._save_index")
    @patch("granola_archive._archive_note")
    @patch("granola_archive._fetch_full_note")
    @patch("granola_archive.iter_all_notes_for_account")
    @patch("granola_archive.build_account_configs_from_env")
    def test_mixed_accounts_each_get_correct_key(
        self,
        mock_build_accounts,
        mock_iter_notes,
        mock_fetch_full,
        mock_archive_note,
        mock_save_index,
    ):
        """In a mixed list, each note is fetched with its own account's key."""
        primary_note = _make_note("d1", account=ACCOUNT_DREW)   # noname
        secondary_note = _make_note("k1", account=ACCOUNT_KELLY)  # noname

        mock_build_accounts.return_value = self._make_accounts()
        # iter_all_notes_for_account is called once per account; side_effect
        # returns primary notes on first call, secondary on second.
        mock_iter_notes.side_effect = [[primary_note], [secondary_note]]
        mock_fetch_full.side_effect = [primary_note, secondary_note]
        mock_archive_note.return_value = (True, [], "2026-04-10_10-00_meeting.md")

        log = MagicMock()
        _run_archive(backfill=True, log=log)

        assert mock_fetch_full.call_count == 2
        calls = mock_fetch_full.call_args_list
        # First call: primary note → primary key
        assert calls[0][1]["api_key"] == PRIMARY_API_KEY
        # Second call: secondary note → secondary key
        assert calls[1][1]["api_key"] == SECONDARY_API_KEY

    @patch("granola_archive._fetch_full_note")
    @patch("granola_archive.iter_all_notes_for_account")
    @patch("granola_archive.build_account_configs_from_env")
    def test_unknown_account_raises_error_before_any_network_call(
        self,
        mock_build_accounts,
        mock_iter_notes,
        mock_fetch_full,
    ):
        """
        A note tagged with an account name not in the registered config must
        raise GranolaUnknownAccountError before _fetch_full_note is ever called.
        This is the regression test for the silent-fallback bug.
        """
        # Only primary account is registered; the note claims a phantom account
        mock_build_accounts.return_value = [
            GranolaAccountConfig(name=ACCOUNT_DREW, api_key=PRIMARY_API_KEY),  # noname
        ]
        phantom_note = _make_note("x1", account="phantom-account")
        mock_iter_notes.return_value = [phantom_note]

        log = MagicMock()
        with pytest.raises(GranolaUnknownAccountError):
            _run_archive(backfill=True, log=log)

        # _fetch_full_note must NOT have been called — no network attempt with wrong key
        mock_fetch_full.assert_not_called()
