"""
Tests for granola_storage.py (Slice 3)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TASKS_DIR = Path(__file__).parent.parent.parent / "scheduled-tasks"
sys.path.insert(0, str(_TASKS_DIR))

from granola_storage import NoteStorage, _parse_date, _stable_json  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _note(
    note_id: str = "test-note-001",
    created_at: str = "2026-04-10T15:30:00Z",
    title: str = "Test Meeting",
) -> dict:
    return {"id": note_id, "title": title, "created_at": created_at}


# ---------------------------------------------------------------------------
# NoteStorage tests
# ---------------------------------------------------------------------------

class TestNoteStorage:
    def test_writes_to_correct_path(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = _note(note_id="abc123", created_at="2026-04-10T15:30:00Z")
        path, was_new = storage.write_note(note)

        expected = tmp_path / "2026" / "04" / "10" / "abc123.json"
        assert path == expected
        assert path.exists()
        assert was_new is True

    def test_written_content_is_valid_json(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = _note()
        path, _ = storage.write_note(note)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"] == note["id"]

    def test_idempotent_same_content(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = _note()
        path1, was_new1 = storage.write_note(note)
        path2, was_new2 = storage.write_note(note)

        assert path1 == path2
        assert was_new1 is True
        assert was_new2 is False

    def test_changed_content_creates_backup(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = _note()
        storage.write_note(note)

        updated_note = {**note, "title": "Updated Title"}
        path, was_new = storage.write_note(updated_note)

        # Original note dir
        note_dir = tmp_path / "2026" / "04" / "10"
        backups = list(note_dir.glob("*.bak.*.json"))
        assert len(backups) == 1
        assert was_new is True  # was overwritten = "new" in our API

        # Current file has updated content
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["title"] == "Updated Title"

    def test_creates_directories_automatically(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = _note(created_at="2025-12-31T23:59:59Z")
        path, _ = storage.write_note(note)
        assert (tmp_path / "2025" / "12" / "31").is_dir()

    def test_raises_if_note_missing_id(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        with pytest.raises(ValueError, match="id"):
            storage.write_note({"title": "No ID"})

    def test_note_exists_true(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = _note(note_id="xyz789", created_at="2026-03-15T10:00:00Z")
        storage.write_note(note)
        assert storage.note_exists("xyz789", created_at="2026-03-15T10:00:00Z") is True

    def test_note_exists_false(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        assert storage.note_exists("nonexistent", created_at="2026-03-15T10:00:00Z") is False

    def test_note_exists_fallback_glob(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = _note(note_id="glob_note")
        storage.write_note(note)
        # Check without created_at triggers glob search
        assert storage.note_exists("glob_note") is True
        assert storage.note_exists("missing_note") is False

    def test_fallback_date_when_created_at_missing(self, tmp_path):
        storage = NoteStorage(root=tmp_path)
        note = {"id": "no_date", "title": "Dateless"}
        path, was_new = storage.write_note(note)
        assert path.exists()  # should use today's date
        assert was_new is True


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_parse_date_standard_iso(self):
        dt = _parse_date("2026-04-10T15:30:00Z")
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 10

    def test_parse_date_fallback_on_empty(self):
        from datetime import datetime, timezone
        dt = _parse_date("")
        # Should be close to now
        now = datetime.now(timezone.utc)
        assert abs((dt - now).total_seconds()) < 5

    def test_stable_json_sorted_keys(self):
        obj = {"z": 1, "a": 2, "m": 3}
        output = _stable_json(obj)
        keys = [line.strip().split(":")[0].strip('"') for line in output.split("\n") if ":" in line]
        assert keys == sorted(keys)

    def test_stable_json_ends_with_newline(self):
        assert _stable_json({"x": 1}).endswith("\n")
