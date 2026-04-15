"""
Granola Incremental Fetch + Cursor State Management — Slice 2

Manages the state file that tracks where the last ingest run left off.
Implements incremental fetching by paginating until hasMore=false and
atomically persisting the final cursor after each page.

State file: ~/lobster-workspace/granola-notes/.state.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from granola_client import GranolaClient, GranolaNote, ListNotesResult


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

# {
#   "cursor": str | null,       -- cursor to pass to next list_notes() call
#   "last_run": ISO8601 str,    -- when the last successful run completed
#   "last_note_id": str | null  -- id of most recently fetched note (informational)
# }

DEFAULT_STATE: dict = {"cursor": None, "last_run": None, "last_note_id": None}


class StateManager:
    """
    Reads and writes the ingest state file.

    Writes are atomic: data is written to a .tmp file then renamed,
    so a crash mid-write never corrupts the existing state.
    """

    def __init__(self, state_path: Optional[Path] = None):
        if state_path is None:
            state_path = (
                Path.home() / "lobster-workspace" / "granola-notes" / ".state.json"
            )
        self._path = Path(state_path)
        self._tmp_path = self._path.with_suffix(".json.tmp")

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict:
        """Return the current state dict. Returns DEFAULT_STATE if file absent."""
        if not self._path.exists():
            return dict(DEFAULT_STATE)
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults so new keys are always present
            return {**DEFAULT_STATE, **data}
        except (json.JSONDecodeError, OSError):
            # Corrupt state — start fresh
            return dict(DEFAULT_STATE)

    def save(
        self,
        cursor: Optional[str],
        last_note_id: Optional[str] = None,
    ) -> None:
        """
        Atomically persist updated state.

        Args:
            cursor:       The cursor value returned by the last list_notes page.
            last_note_id: The id of the last note seen (for diagnostics).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "cursor": cursor,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_note_id": last_note_id,
        }
        with self._tmp_path.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(self._tmp_path, self._path)


# ---------------------------------------------------------------------------
# Incremental fetcher
# ---------------------------------------------------------------------------


class IncrementalFetcher:
    """
    Wraps GranolaClient + StateManager to yield only new notes on each run.

    Usage::

        fetcher = IncrementalFetcher()
        for note in fetcher.fetch_new():
            storage.write_note(note.raw)
    """

    def __init__(
        self,
        client: Optional[GranolaClient] = None,
        state_manager: Optional[StateManager] = None,
    ):
        self._client = client or GranolaClient()
        self._state = state_manager or StateManager()

    def fetch_new(self) -> Iterator[GranolaNote]:
        """
        Yield all notes that are new since the last saved cursor.

        After each page, the cursor is atomically saved so a mid-run
        crash won't re-fetch already-processed pages on restart.

        Yields GranolaNote objects in API-returned order.
        """
        state = self._state.load()
        cursor: Optional[str] = state.get("cursor")

        last_note_id: Optional[str] = None
        any_fetched = False

        while True:
            result: ListNotesResult = self._client.list_notes(cursor=cursor)

            if not result.notes:
                # No new notes; no state update needed if we had no results
                if not any_fetched:
                    return
                break

            for note in result.notes:
                last_note_id = note.id
                any_fetched = True
                yield note

            # Save cursor after each page (atomic write)
            self._state.save(
                cursor=result.cursor,
                last_note_id=last_note_id,
            )

            if not result.has_more:
                break

            cursor = result.cursor

    def run_and_count(self, on_note=None) -> int:
        """
        Convenience method: run a full incremental fetch and return count of new notes.

        Args:
            on_note: Optional callable(GranolaNote) invoked for each note.

        Returns:
            Number of notes fetched.
        """
        count = 0
        for note in self.fetch_new():
            if on_note:
                on_note(note)
            count += 1
        return count
