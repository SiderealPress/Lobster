"""
Static Memory Backend - Layer 3 Fallback

Reads from memory/canonical/ files and uses grep-like searching
over markdown and JSONL files. This is the fallback backend when
VectorMemory is unavailable (e.g., sqlite-vec not installed).

Static files are always the source of truth. This backend provides
a slower but always-available search mechanism.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .provider import MemoryEvent

log = logging.getLogger("lobster-memory")

# Default paths
DEFAULT_CANONICAL_DIR = Path.home() / "lobster" / "memory" / "canonical"
DEFAULT_EVENT_LOG = Path.home() / "lobster" / "data" / "events.jsonl"


class StaticMemory:
    """Static file-based memory backend.

    Uses grep-like keyword matching over canonical markdown files
    and a JSONL event log. Slower than VectorMemory but always works
    without any special dependencies.
    """

    def __init__(
        self,
        canonical_dir: Path = None,
        event_log: Path = None,
    ):
        self._canonical_dir = canonical_dir or DEFAULT_CANONICAL_DIR
        self._event_log = event_log or DEFAULT_EVENT_LOG
        self._canonical_dir.mkdir(parents=True, exist_ok=True)
        self._event_log.parent.mkdir(parents=True, exist_ok=True)
        # Track next ID from event log
        self._next_id = self._compute_next_id()

    def _compute_next_id(self) -> int:
        """Compute next available event ID from the JSONL log."""
        max_id = 0
        if self._event_log.exists():
            for line in self._event_log.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    eid = event.get("id", 0)
                    if isinstance(eid, int) and eid > max_id:
                        max_id = eid
                except json.JSONDecodeError:
                    continue
        return max_id + 1

    def store(self, event: MemoryEvent) -> int:
        """Append event to JSONL log file."""
        event.id = self._next_id
        self._next_id += 1

        with open(self._event_log, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

        return event.id

    def search(self, query: str, limit: int = 10, project: str = None) -> list[MemoryEvent]:
        """Search across canonical files and event log using keyword matching.

        Searches markdown files in canonical/ and events in the JSONL log.
        """
        results = []

        # Search canonical markdown files
        results.extend(self._search_canonical(query, project))

        # Search event log
        results.extend(self._search_event_log(query, project))

        # Sort by relevance (number of keyword matches) descending
        query_terms = query.lower().split()
        results.sort(
            key=lambda e: sum(
                1 for term in query_terms if term in e.content.lower()
            ),
            reverse=True,
        )

        return results[:limit]

    def _search_canonical(self, query: str, project: str = None) -> list[MemoryEvent]:
        """Search canonical markdown files for keyword matches."""
        results = []
        query_lower = query.lower()
        query_terms = query_lower.split()

        if not self._canonical_dir.exists():
            return results

        for md_file in self._canonical_dir.rglob("*.md"):
            try:
                content = md_file.read_text()
            except (OSError, UnicodeDecodeError):
                continue

            # Check if any query term appears in the file
            content_lower = content.lower()
            if not any(term in content_lower for term in query_terms):
                continue

            # Determine project from file path
            file_project = None
            rel_path = md_file.relative_to(self._canonical_dir)
            if rel_path.parts[0] == "projects" and len(rel_path.parts) > 1:
                file_project = rel_path.parts[1].replace(".md", "")

            # Apply project filter
            if project and file_project != project:
                continue

            # Extract relevant paragraphs containing matches
            paragraphs = content.split("\n\n")
            for para in paragraphs:
                para_lower = para.lower()
                if any(term in para_lower for term in query_terms):
                    results.append(MemoryEvent(
                        id=None,
                        timestamp=datetime.fromtimestamp(
                            md_file.stat().st_mtime, tz=timezone.utc
                        ),
                        type="canonical",
                        source="static_file",
                        project=file_project,
                        content=para.strip(),
                        metadata={"file": str(md_file)},
                        consolidated=True,
                    ))

        return results

    def _search_event_log(self, query: str, project: str = None) -> list[MemoryEvent]:
        """Search JSONL event log for keyword matches."""
        results = []
        query_terms = query.lower().split()

        if not self._event_log.exists():
            return results

        for line in self._event_log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            content = data.get("content", "").lower()
            if not any(term in content for term in query_terms):
                continue

            event = MemoryEvent.from_dict(data)

            if project and event.project != project:
                continue

            results.append(event)

        return results

    def recent(self, hours: int = 24, project: str = None) -> list[MemoryEvent]:
        """Get recent events from the JSONL log."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        results = []

        if not self._event_log.exists():
            return results

        for line in self._event_log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = MemoryEvent.from_dict(data)

            if event.timestamp.tzinfo is None:
                event.timestamp = event.timestamp.replace(tzinfo=timezone.utc)

            if event.timestamp < cutoff:
                continue

            if project and event.project != project:
                continue

            results.append(event)

        # Sort newest first
        results.sort(key=lambda e: e.timestamp, reverse=True)
        return results

    def unconsolidated(self) -> list[MemoryEvent]:
        """Get unconsolidated events from the JSONL log."""
        results = []

        if not self._event_log.exists():
            return results

        for line in self._event_log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if data.get("consolidated", False):
                continue

            results.append(MemoryEvent.from_dict(data))

        results.sort(key=lambda e: e.timestamp)
        return results

    def mark_consolidated(self, event_ids: list[int]) -> None:
        """Mark events as consolidated in the JSONL log.

        Rewrites the log file with updated consolidated flags.
        """
        if not event_ids or not self._event_log.exists():
            return

        ids_set = set(event_ids)
        lines = self._event_log.read_text().splitlines()
        new_lines = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("id") in ids_set:
                    data["consolidated"] = True
                new_lines.append(json.dumps(data))
            except json.JSONDecodeError:
                new_lines.append(line)

        self._event_log.write_text("\n".join(new_lines) + "\n" if new_lines else "")

    def close(self) -> None:
        """No-op for static memory (no connections to close)."""
        pass
