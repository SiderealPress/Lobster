"""
Tests for granola_state.py (Slice 2)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TASKS_DIR = Path(__file__).parent.parent.parent / "scheduled-tasks"
sys.path.insert(0, str(_TASKS_DIR))

from granola_client import GranolaNote, ListNotesResult  # noqa: E402
from granola_state import IncrementalFetcher, StateManager  # noqa: E402


# ---------------------------------------------------------------------------
# StateManager tests
# ---------------------------------------------------------------------------

class TestStateManager:
    def test_load_returns_defaults_when_no_file(self, tmp_path):
        sm = StateManager(state_path=tmp_path / ".state.json")
        state = sm.load()
        assert state["cursor"] is None
        assert state["last_run"] is None
        assert state["last_note_id"] is None

    def test_save_and_load_round_trip(self, tmp_path):
        sm = StateManager(state_path=tmp_path / ".state.json")
        sm.save(cursor="cursor_xyz", last_note_id="note_123")
        state = sm.load()
        assert state["cursor"] == "cursor_xyz"
        assert state["last_note_id"] == "note_123"
        assert state["last_run"] is not None  # should be set to now

    def test_save_is_atomic(self, tmp_path):
        """Temp file should not persist after a successful save."""
        sm = StateManager(state_path=tmp_path / ".state.json")
        sm.save(cursor="abc")
        assert not (tmp_path / ".state.json.tmp").exists()
        assert (tmp_path / ".state.json").exists()

    def test_load_handles_corrupt_file(self, tmp_path):
        state_path = tmp_path / ".state.json"
        state_path.write_text("not valid json", encoding="utf-8")
        sm = StateManager(state_path=state_path)
        state = sm.load()
        assert state["cursor"] is None

    def test_save_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / ".state.json"
        sm = StateManager(state_path=nested)
        sm.save(cursor="x")
        assert nested.exists()

    def test_load_merges_new_keys(self, tmp_path):
        """Old state files missing new keys should get default values."""
        state_path = tmp_path / ".state.json"
        state_path.write_text(json.dumps({"cursor": "old_cursor"}), encoding="utf-8")
        sm = StateManager(state_path=state_path)
        state = sm.load()
        assert state["cursor"] == "old_cursor"
        assert state["last_run"] is None
        assert state["last_note_id"] is None


# ---------------------------------------------------------------------------
# IncrementalFetcher tests
# ---------------------------------------------------------------------------

def _make_note(note_id: str) -> GranolaNote:
    return GranolaNote(
        id=note_id,
        title=f"Note {note_id}",
        created_at="2026-04-10T10:00:00Z",
        raw={"id": note_id, "title": f"Note {note_id}", "created_at": "2026-04-10T10:00:00Z"},
    )


def _make_result(
    note_ids: list[str],
    has_more: bool = False,
    cursor: str | None = None,
) -> ListNotesResult:
    return ListNotesResult(
        notes=[_make_note(nid) for nid in note_ids],
        has_more=has_more,
        cursor=cursor,
    )


class TestIncrementalFetcher:
    def test_yields_notes_from_single_page(self, tmp_path):
        client = MagicMock()
        client.list_notes.return_value = _make_result(["n1", "n2", "n3"])
        sm = StateManager(state_path=tmp_path / ".state.json")
        fetcher = IncrementalFetcher(client=client, state_manager=sm)

        notes = list(fetcher.fetch_new())
        assert [n.id for n in notes] == ["n1", "n2", "n3"]

    def test_follows_pagination(self, tmp_path):
        client = MagicMock()
        client.list_notes.side_effect = [
            _make_result(["n1", "n2"], has_more=True, cursor="pg2"),
            _make_result(["n3", "n4"], has_more=False, cursor=None),
        ]
        sm = StateManager(state_path=tmp_path / ".state.json")
        fetcher = IncrementalFetcher(client=client, state_manager=sm)

        notes = list(fetcher.fetch_new())
        assert [n.id for n in notes] == ["n1", "n2", "n3", "n4"]
        # Should have been called twice: once with cursor=None, once with cursor="pg2"
        assert client.list_notes.call_count == 2

    def test_saves_cursor_after_each_page(self, tmp_path):
        client = MagicMock()
        client.list_notes.side_effect = [
            _make_result(["n1"], has_more=True, cursor="pg2"),
            _make_result(["n2"], has_more=False, cursor="final"),
        ]
        sm = StateManager(state_path=tmp_path / ".state.json")
        fetcher = IncrementalFetcher(client=client, state_manager=sm)

        list(fetcher.fetch_new())

        state = sm.load()
        assert state["cursor"] == "final"

    def test_empty_result_does_not_update_state(self, tmp_path):
        client = MagicMock()
        client.list_notes.return_value = _make_result([])
        sm = StateManager(state_path=tmp_path / ".state.json")
        fetcher = IncrementalFetcher(client=client, state_manager=sm)

        list(fetcher.fetch_new())

        # State file should not exist (no successful fetch)
        assert not sm.path.exists()

    def test_uses_saved_cursor_on_next_run(self, tmp_path):
        sm = StateManager(state_path=tmp_path / ".state.json")
        sm.save(cursor="saved_cursor")

        client = MagicMock()
        client.list_notes.return_value = _make_result(["n5"])
        fetcher = IncrementalFetcher(client=client, state_manager=sm)

        list(fetcher.fetch_new())

        # The first call should use the saved cursor
        call_kwargs = client.list_notes.call_args
        assert call_kwargs[1].get("cursor") == "saved_cursor" or \
               call_kwargs[0][0] == "saved_cursor" if call_kwargs[0] else \
               client.list_notes.call_args_list[0].kwargs.get("cursor") == "saved_cursor"

    def test_run_and_count_returns_correct_count(self, tmp_path):
        client = MagicMock()
        client.list_notes.return_value = _make_result(["a", "b", "c"])
        sm = StateManager(state_path=tmp_path / ".state.json")
        fetcher = IncrementalFetcher(client=client, state_manager=sm)

        count = fetcher.run_and_count()
        assert count == 3
