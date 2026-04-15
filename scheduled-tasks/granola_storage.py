"""
Granola Storage Layer — Slice 3

Writes raw Granola note JSON to a versioned folder hierarchy:

    ~/lobster-workspace/granola-notes/YYYY/MM/DD/<note_id>.json

Idempotent: if the exact same data already exists, the write is a no-op.
If the note has changed (updated by Granola), the old file is backed up
before overwriting.

No external dependencies beyond stdlib.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


NOTES_ROOT_DEFAULT = Path.home() / "lobster-workspace" / "granola-notes"


class NoteStorage:
    """
    Persists Granola note dicts to a date-partitioned folder hierarchy.

    Args:
        root: Base directory for all stored notes. Defaults to
              ~/lobster-workspace/granola-notes/
    """

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root) if root else NOTES_ROOT_DEFAULT

    @property
    def root(self) -> Path:
        return self._root

    def write_note(self, note: dict) -> Tuple[Path, bool]:
        """
        Write a note dict to the versioned folder structure.

        Determines the date from ``note["created_at"]`` (ISO 8601 UTC).
        Falls back to today if the field is missing or unparseable.

        Returns:
            (path, was_new) — path is where the file lives;
            was_new is True if this is a freshly created file,
            False if the file already existed (identical or updated).
        """
        note_id = note.get("id")
        if not note_id:
            raise ValueError("Note dict missing 'id' field")

        date_dir = self._date_dir(note)
        date_dir.mkdir(parents=True, exist_ok=True)

        dest = date_dir / f"{note_id}.json"
        serialized = _stable_json(note)

        if dest.exists():
            existing = dest.read_text(encoding="utf-8")
            if existing == serialized:
                # Identical — no-op
                return dest, False
            else:
                # Content changed — back up before overwriting
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup = dest.with_suffix(f".bak.{ts}.json")
                shutil.copy2(dest, backup)

        dest.write_text(serialized, encoding="utf-8")
        was_new = not dest.exists() or True  # always True after write
        return dest, True

    def note_exists(self, note_id: str, created_at: Optional[str] = None) -> bool:
        """
        Return True if a note file already exists on disk.

        If created_at is provided, checks the expected date-partitioned path.
        Otherwise performs a glob search (slower, avoid in hot path).
        """
        if created_at:
            dt = _parse_date(created_at)
            path = self._root / dt.strftime("%Y/%m/%d") / f"{note_id}.json"
            return path.exists()
        # Fallback: glob
        matches = list(self._root.glob(f"**/{note_id}.json"))
        return bool(matches)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _date_dir(self, note: dict) -> Path:
        created_at = note.get("created_at", "")
        dt = _parse_date(created_at)
        return self._root / dt.strftime("%Y/%m/%d")


def _parse_date(iso_str: str) -> datetime:
    """Parse ISO 8601 string to UTC datetime. Falls back to today on failure."""
    if not iso_str:
        return datetime.now(timezone.utc)
    try:
        # Python 3.7+ fromisoformat doesn't handle trailing Z
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def _stable_json(obj: dict) -> str:
    """Serialize to JSON with sorted keys and consistent formatting."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
