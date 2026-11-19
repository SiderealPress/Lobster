"""Reply -> "..." placeholder matching for the Slack router (issue #2325).

Every inbound Slack message gets a "..." typing placeholder.  When the
dispatcher's reply arrives it should replace the placeholder under the
message it is answering.  The dispatcher frequently answers a *burst* of N
messages with one batched reply, so a simple FIFO (pop the oldest) drifts:
N-1 placeholders stay queued and every later reply edits a stale message.

Matching rules (pure, see ``resolve_reply``):

1. Placeholders older than ``max_age`` are expired: never a target, deleted.
2. The reply's *conversation* is chosen:
   - explicit ``reply_to_slack_ts`` -> the placeholder for that inbound message;
   - threaded reply -> placeholders in that thread, or, if none, the top-level
     placeholder whose inbound ``slack_ts`` equals the reply's ``thread_ts``
     (the dispatcher threading its answer under the user's message);
   - unthreaded reply -> every placeholder in the channel (the dispatcher
     usually omits ``thread_ts`` even for threaded inbound messages).
3. The target is the explicit match, else the newest placeholder in the
   conversation.  Every *older* placeholder in that conversation is
   superseded (it belongs to the burst the reply answered) and is deleted.
   Newer ones (only possible with an explicit target) remain pending.
4. No candidate -> no target; the caller posts the reply as a new message.

``PlaceholderStore`` is the thin stateful shell: a lock around an immutable
tuple, persisted to JSON on every mutation so a router restart can restore
pending placeholders and clean up expired ones.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("lobster-slack")

# A "..." that has waited this long without a reply is treated as orphaned.
# The dispatcher normally answers (or acks) within seconds; 10 minutes leaves
# ample headroom for a slow turn while bounding how long an orphan can linger.
DEFAULT_PLACEHOLDER_MAX_AGE_SECONDS: float = 600.0


@dataclass(frozen=True)
class Placeholder:
    channel_id: str
    thread_ts: str | None  # thread the inbound message (and placeholder) live in
    slack_ts: str  # ts of the inbound user message
    placeholder_ts: str  # ts of the "..." message we posted
    created_at: float  # epoch seconds


@dataclass(frozen=True)
class Resolution:
    target: Placeholder | None
    stale: tuple[Placeholder, ...]  # expired or superseded: delete these
    remaining: tuple[Placeholder, ...]  # still pending after this reply


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def is_expired(p: Placeholder, now: float, max_age: float) -> bool:
    return (now - p.created_at) > max_age


def partition_expired(
    pending: Iterable[Placeholder], now: float, max_age: float
) -> tuple[tuple[Placeholder, ...], tuple[Placeholder, ...]]:
    """Split *pending* into ``(fresh, expired)``, preserving order."""
    items = tuple(pending)
    return (
        tuple(p for p in items if not is_expired(p, now, max_age)),
        tuple(p for p in items if is_expired(p, now, max_age)),
    )


def _order_key(p: Placeholder) -> tuple[float, str]:
    return (p.created_at, p.placeholder_ts)


def _conversation(
    fresh_in_channel: tuple[Placeholder, ...],
    thread_ts: str | None,
    reply_to_slack_ts: str | None,
) -> tuple[tuple[Placeholder, ...], Placeholder | None]:
    """Return ``(candidates, explicit_target)`` for a reply."""
    explicit_ts = reply_to_slack_ts
    if explicit_ts is None and thread_ts is not None:
        in_thread = tuple(p for p in fresh_in_channel if p.thread_ts == thread_ts)
        if in_thread:
            return in_thread, None
        explicit_ts = thread_ts  # reply threaded under a top-level message
    if explicit_ts is not None:
        match = next((p for p in fresh_in_channel if p.slack_ts == explicit_ts), None)
        if match is None:
            return (), None
        return tuple(p for p in fresh_in_channel if p.thread_ts == match.thread_ts), match
    return fresh_in_channel, None


def resolve_reply(
    pending: Iterable[Placeholder],
    channel_id: str,
    thread_ts: str | None,
    *,
    now: float,
    max_age: float,
    reply_to_slack_ts: str | None = None,
) -> Resolution:
    """Decide which placeholder a reply fills and which become stale."""
    items = tuple(pending)
    in_channel = tuple(p for p in items if p.channel_id == channel_id)
    others = tuple(p for p in items if p.channel_id != channel_id)
    fresh, expired = partition_expired(in_channel, now, max_age)

    candidates, explicit = _conversation(fresh, thread_ts, reply_to_slack_ts)
    if not candidates:
        return Resolution(target=None, stale=expired, remaining=others + fresh)

    target = explicit or max(candidates, key=_order_key)
    superseded = tuple(
        p for p in candidates if p != target and _order_key(p) <= _order_key(target)
    )
    consumed = {target, *superseded}
    remaining = others + tuple(p for p in fresh if p not in consumed)
    return Resolution(target=target, stale=expired + superseded, remaining=remaining)


# ---------------------------------------------------------------------------
# Serialisation (pure)
# ---------------------------------------------------------------------------

def to_json(pending: Iterable[Placeholder]) -> str:
    return json.dumps([asdict(p) for p in pending], indent=2)


def from_json(raw: str) -> tuple[Placeholder, ...]:
    """Parse persisted state; malformed entries are skipped."""
    data = json.loads(raw)
    if not isinstance(data, list):
        return ()

    def parse(entry: object) -> Placeholder | None:
        if not isinstance(entry, dict):
            return None
        try:
            return Placeholder(
                channel_id=str(entry["channel_id"]),
                thread_ts=entry.get("thread_ts") or None,
                slack_ts=str(entry["slack_ts"]),
                placeholder_ts=str(entry["placeholder_ts"]),
                created_at=float(entry["created_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    return tuple(p for p in map(parse, data) if p is not None)


# ---------------------------------------------------------------------------
# Stateful shell
# ---------------------------------------------------------------------------

class PlaceholderStore:
    """Thread-safe, persisted set of pending placeholders.

    The router touches this from Socket Mode handler threads, poller threads,
    the outbox watcher and the fallback scanner, so all access is locked.
    """

    def __init__(
        self,
        state_path: Path | None,
        *,
        clock: Callable[[], float] = time.time,
        max_age: float = DEFAULT_PLACEHOLDER_MAX_AGE_SECONDS,
    ) -> None:
        self._path = state_path
        self._clock = clock
        self.max_age = max_age
        self._lock = threading.Lock()
        self._pending: tuple[Placeholder, ...] = ()

    def now(self) -> float:
        return self._clock()

    def pending(self) -> tuple[Placeholder, ...]:
        with self._lock:
            return self._pending

    def _set(self, new: tuple[Placeholder, ...]) -> None:
        """Replace state and persist.  Caller holds the lock."""
        if new == self._pending:
            return
        self._pending = new
        self._persist(new)

    def _persist(self, pending: tuple[Placeholder, ...]) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".slack-ph-", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(to_json(pending))
            os.replace(tmp, self._path)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Could not persist Slack placeholder state to %s: %s", self._path, exc)

    def add(self, p: Placeholder) -> None:
        with self._lock:
            self._set(self._pending + (p,))

    def register(
        self, channel_id: str, thread_ts: str | None, slack_ts: str, placeholder_ts: str
    ) -> Placeholder:
        """Record a freshly posted placeholder, stamped with the store's clock."""
        p = Placeholder(
            channel_id=str(channel_id),
            thread_ts=str(thread_ts) if thread_ts else None,
            slack_ts=str(slack_ts),
            placeholder_ts=str(placeholder_ts),
            created_at=float(self._clock()),
        )
        self.add(p)
        return p

    def claim(
        self, channel_id: str, thread_ts: str | None, reply_to_slack_ts: str | None = None
    ) -> Resolution:
        with self._lock:
            r = resolve_reply(
                self._pending, channel_id, thread_ts,
                now=self._clock(), max_age=self.max_age,
                reply_to_slack_ts=reply_to_slack_ts,
            )
            self._set(r.remaining)
            return r

    def sweep(self) -> tuple[Placeholder, ...]:
        """Remove and return every expired placeholder (all channels)."""
        with self._lock:
            fresh, expired = partition_expired(self._pending, self._clock(), self.max_age)
            self._set(fresh)
            return expired

    def load(self) -> tuple[Placeholder, ...]:
        """Restore persisted state; return expired entries for cleanup."""
        if self._path is None or not self._path.exists():
            return ()
        try:
            loaded = from_json(self._path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable Slack placeholder state %s: %s", self._path, exc)
            loaded = ()
        with self._lock:
            fresh, expired = partition_expired(loaded, self._clock(), self.max_age)
            self._pending = fresh
            self._persist(fresh)
            return expired
