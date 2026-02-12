"""
Memory Provider Protocol and MemoryEvent dataclass.

Defines the interface that all memory backends must implement.
This allows hot-swapping between VectorMemory and StaticMemory
without changing any calling code.
"""

from dataclasses import dataclass, field
from typing import Protocol, Optional
from datetime import datetime


@dataclass
class MemoryEvent:
    """A single event stored in memory.

    Events represent messages, tasks, decisions, notes, or links
    that Lobster should remember. Each event is stored with its
    embedding for vector search and indexed for keyword search.
    """
    id: Optional[int]
    timestamp: datetime
    type: str        # 'message', 'task', 'decision', 'note', 'link'
    source: str      # 'telegram', 'github', 'internal'
    project: Optional[str]
    content: str
    metadata: dict = field(default_factory=dict)
    consolidated: bool = False

    def to_dict(self) -> dict:
        """Serialize to a dictionary."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "type": self.type,
            "source": self.source,
            "project": self.project,
            "content": self.content,
            "metadata": self.metadata,
            "consolidated": self.consolidated,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEvent":
        """Deserialize from a dictionary."""
        ts = data.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        elif ts is None:
            ts = datetime.now()
        return cls(
            id=data.get("id"),
            timestamp=ts,
            type=data.get("type", "note"),
            source=data.get("source", "internal"),
            project=data.get("project"),
            content=data.get("content", ""),
            metadata=data.get("metadata", {}),
            consolidated=data.get("consolidated", False),
        )


class MemoryProvider(Protocol):
    """Protocol for memory backends.

    Any memory backend (vector DB, static files, etc.) must implement
    these methods. This allows the vector system to be removed or
    swapped without incident.
    """

    def store(self, event: MemoryEvent) -> int:
        """Store an event and return its ID."""
        ...

    def search(self, query: str, limit: int = 10, project: str = None) -> list[MemoryEvent]:
        """Search memory for events matching the query.

        Uses hybrid search (vector + keyword) when available,
        falls back to keyword-only search otherwise.
        """
        ...

    def recent(self, hours: int = 24, project: str = None) -> list[MemoryEvent]:
        """Get recent events from the last N hours."""
        ...

    def unconsolidated(self) -> list[MemoryEvent]:
        """Get all events that haven't been consolidated yet."""
        ...

    def mark_consolidated(self, event_ids: list[int]) -> None:
        """Mark events as consolidated (processed by nightly job)."""
        ...

    def close(self) -> None:
        """Clean up resources (close DB connections, etc.)."""
        ...
