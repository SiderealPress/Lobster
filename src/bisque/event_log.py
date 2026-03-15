"""Bounded event log for SSE replay support.

Stores a fixed-size window of recent events using a deque, enabling
clients that reconnect to replay missed events via Last-Event-ID.
"""

from __future__ import annotations

from collections import deque


class EventLog:
    """Bounded log of (event_id, serialized_frame) tuples.

    Uses a deque with a fixed maxlen so oldest events are automatically
    evicted when the log reaches capacity. No locking -- this is a pure
    data structure intended for single-threaded use.
    """

    def __init__(self, max_events: int = 500) -> None:
        self._events: deque[tuple[str, str]] = deque(maxlen=max_events)
        self._id_set: set[str] = set()

    # -- mutators --

    def append(self, event_id: str, frame: str) -> None:
        """Add an event. Oldest entry is auto-evicted at capacity."""
        if len(self._events) == self._events.maxlen:
            evicted_id, _ = self._events[0]
            self._id_set.discard(evicted_id)
        self._events.append((event_id, frame))
        self._id_set.add(event_id)

    def clear(self) -> None:
        """Remove all events."""
        self._events.clear()
        self._id_set.clear()

    # -- queries --

    def replay_after(self, last_event_id: str) -> list[str] | None:
        """Return serialized frames after *last_event_id*.

        Returns None if *last_event_id* is not in the log (evicted or
        never existed), signalling the client must do a full refresh.
        """
        if last_event_id not in self._id_set:
            return None

        frames: list[str] = []
        found = False
        for eid, frame in self._events:
            if found:
                frames.append(frame)
            elif eid == last_event_id:
                found = True
        return frames

    def get_latest_id(self) -> str | None:
        """Return the most recent event_id, or None if the log is empty."""
        if not self._events:
            return None
        return self._events[-1][0]

    def contains(self, event_id: str) -> bool:
        """Check whether *event_id* is present in the log."""
        return event_id in self._id_set

    def __len__(self) -> int:
        return len(self._events)
