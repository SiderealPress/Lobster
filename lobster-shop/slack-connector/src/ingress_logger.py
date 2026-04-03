"""
Slack Ingress Logger — writes raw Slack events to JSONL log files.

Called by slack_router.py on every event, BEFORE any LLM routing.
No LLM calls. No blocking network I/O. Fast path only.

Design principles:
- Pure data transformations for record building (no side effects)
- Side effects isolated to log_event() boundary (file I/O, dedup DB)
- Deduplication via SQLite (ts + channel_id composite key)
- Automatic pruning of stale dedup records (>7 days)
- Account-mode aware: person mode logs all messages but filters self-messages
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("slack-ingress-logger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1
_DEDUP_RETENTION_DAYS = 7

_DEFAULT_LOG_ROOT = Path(
    os.environ.get(
        "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
    )
) / "slack-connector" / "logs"

_DEFAULT_DEDUP_DB = Path(
    os.environ.get(
        "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
    )
) / "slack-connector" / "state" / "dedup.db"


# ---------------------------------------------------------------------------
# Pure functions — record building (no side effects)
# ---------------------------------------------------------------------------


def build_record(
    *,
    event: dict[str, Any],
    channel_id: str,
    channel_name: str = "",
    user_id: str = "",
    username: str = "",
    display_name: str = "",
    is_dm: bool = False,
) -> dict[str, Any]:
    """Build a JSONL record from a raw Slack event.

    Pure function: takes data in, returns data out, no I/O.
    """
    now = datetime.now(timezone.utc).isoformat()

    return {
        "schema": _SCHEMA_VERSION,
        "event_id": event.get("event_id", event.get("client_msg_id", "")),
        "ts": event.get("ts", ""),
        "channel_id": channel_id,
        "channel_name": channel_name,
        "user_id": user_id or event.get("user", ""),
        "username": username,
        "display_name": display_name,
        "text": event.get("text", ""),
        "thread_ts": event.get("thread_ts"),
        "parent_ts": event.get("parent_user_id") and event.get("thread_ts"),
        "files": _extract_files(event),
        "reactions": _extract_reactions(event),
        "subtypes": _extract_subtypes(event),
        "logged_at": now,
        "raw": event,
    }


def _extract_files(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract file metadata from a Slack event. Pure function."""
    return [
        {
            "id": f.get("id", ""),
            "name": f.get("name", ""),
            "mimetype": f.get("mimetype", ""),
            "size": f.get("size", 0),
            "url": f.get("url_private", ""),
        }
        for f in event.get("files", [])
    ]


def _extract_reactions(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract reactions from a Slack event. Pure function."""
    return [
        {
            "name": r.get("name", ""),
            "users": r.get("users", []),
            "count": r.get("count", 0),
        }
        for r in event.get("reactions", [])
    ]


def _extract_subtypes(event: dict[str, Any]) -> list[str]:
    """Extract subtypes from a Slack event. Pure function."""
    subtype = event.get("subtype")
    return [subtype] if subtype else []


def is_self_message(event: dict[str, Any], own_user_id: str) -> bool:
    """Check if an event was authored by our own user account.

    Pure function. Used in person mode to prevent Lobster from
    responding to (or re-logging) its own messages.

    Args:
        event: The raw Slack event dict.
        own_user_id: The Slack user ID of the Lobster account.

    Returns:
        True if the event was authored by own_user_id.
    """
    if not own_user_id:
        return False
    return event.get("user", "") == own_user_id


def should_log_in_mode(
    *,
    account_type: str,
    is_mention: bool,
    is_dm: bool,
    is_self: bool,
) -> bool:
    """Decide whether an event should be logged given the account mode.

    Pure function. In bot mode, all events are logged (filtering happens
    at the channel config level). In person mode, self-messages are
    excluded to prevent feedback loops.

    Args:
        account_type: 'bot' or 'person'.
        is_mention: Whether the event mentions our account.
        is_dm: Whether the event is a direct message.
        is_self: Whether the event was authored by our account.

    Returns:
        True if the event should be logged.
    """
    # Never log our own messages in person mode
    if account_type == "person" and is_self:
        return False
    return True


def should_route_to_llm_for_mode(
    *,
    account_type: str,
    is_mention: bool,
    is_dm: bool,
    is_self: bool,
    channel_mode: str = "monitor",
) -> bool:
    """Decide whether an event should be routed to the LLM inbox.

    Pure function. Combines account-mode filtering with channel-mode routing.

    In person mode: route ALL messages in 'respond'/'full' channels
    (not just @mentions), except self-messages.
    In bot mode: follow standard channel_config routing (mentions/DMs only
    in 'respond' mode).
    """
    # Never route self-messages
    if is_self:
        return False

    if channel_mode in ("monitor", "ignore"):
        return False

    if channel_mode == "full":
        return True

    if channel_mode == "respond":
        # Person mode: route all messages, not just mentions
        if account_type == "person":
            return True
        # Bot mode: only route mentions and DMs
        return is_mention or is_dm

    return False


def log_path_for_event(
    *, channel_id: str, is_dm: bool, log_root: Path, date: Optional[str] = None
) -> Path:
    """Compute the JSONL log file path for a given channel and date.

    Pure function: deterministic path from inputs.
    """
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    category = "dms" if is_dm else "channels"
    return log_root / category / channel_id / f"{date_str}.jsonl"


# ---------------------------------------------------------------------------
# Dedup store — thin side-effect boundary around SQLite
# ---------------------------------------------------------------------------


class DedupStore:
    """SQLite-backed deduplication for Slack event timestamps.

    Keeps (ts, channel_id) pairs for _DEDUP_RETENTION_DAYS days.
    Thread-safe via SQLite's built-in locking.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_events (
                ts TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                logged_at TEXT NOT NULL,
                PRIMARY KEY (ts, channel_id)
            )
            """
        )
        self._conn.commit()

    def is_duplicate(self, ts: str, channel_id: str) -> bool:
        """Check whether (ts, channel_id) has already been seen."""
        cursor = self._conn.execute(
            "SELECT 1 FROM seen_events WHERE ts = ? AND channel_id = ?",
            (ts, channel_id),
        )
        return cursor.fetchone() is not None

    def mark_seen(self, ts: str, channel_id: str) -> None:
        """Record (ts, channel_id) as seen. Idempotent (INSERT OR IGNORE)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_events (ts, channel_id, logged_at) VALUES (?, ?, ?)",
            (ts, channel_id, now),
        )
        self._conn.commit()

    def check_and_mark(self, ts: str, channel_id: str) -> bool:
        """Atomically check and mark. Returns True if this is a NEW event."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO seen_events (ts, channel_id, logged_at) VALUES (?, ?, ?)",
                (ts, channel_id, now),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def prune(self, retention_days: int = _DEDUP_RETENTION_DAYS) -> int:
        """Delete seen_events older than retention_days. Returns count deleted."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        cursor = self._conn.execute(
            "DELETE FROM seen_events WHERE logged_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# SlackIngressLogger — orchestrates pure transforms + side effects
# ---------------------------------------------------------------------------


class SlackIngressLogger:
    """Writes raw Slack events to the JSONL log store.

    Side effects are isolated here: file writes and dedup DB access.
    All data transformation is delegated to pure functions above.

    Supports both bot and person account modes:
    - Bot mode: logs all events, delegates filtering to channel config
    - Person mode: logs all events except self-authored messages
    """

    def __init__(
        self,
        log_root: Optional[Path] = None,
        dedup_db_path: Optional[Path] = None,
        account_type: str = "bot",
        own_user_id: str = "",
    ) -> None:
        self._log_root = log_root or _DEFAULT_LOG_ROOT
        self._dedup = DedupStore(dedup_db_path or _DEFAULT_DEDUP_DB)
        self._account_type = account_type
        self._own_user_id = own_user_id

    def log_message(self, event: dict[str, Any], channel_id: str, **kwargs: Any) -> None:
        """Log a message event. Deduplicates by (ts, channel_id)."""
        self._log_event(event=event, channel_id=channel_id, **kwargs)

    def log_reaction(self, event: dict[str, Any], channel_id: str, **kwargs: Any) -> None:
        """Log a reaction_added event. Deduplicates by (ts, channel_id)."""
        self._log_event(event=event, channel_id=channel_id, **kwargs)

    def log_file(self, event: dict[str, Any], channel_id: str, **kwargs: Any) -> None:
        """Log a file_shared event. Deduplicates by (ts, channel_id)."""
        self._log_event(event=event, channel_id=channel_id, **kwargs)

    def prune_dedup(self) -> int:
        """Prune stale dedup records. Returns count pruned."""
        return self._dedup.prune()

    def close(self) -> None:
        """Release resources."""
        self._dedup.close()

    # -- internal --

    def _log_event(
        self,
        *,
        event: dict[str, Any],
        channel_id: str,
        channel_name: str = "",
        user_id: str = "",
        username: str = "",
        display_name: str = "",
        is_dm: bool = False,
    ) -> None:
        """Core logging pipeline: self-check → dedup → build record → append to file."""
        ts = event.get("ts", "")
        if not ts or not channel_id:
            log.warning("Skipping event with missing ts or channel_id")
            return

        # In person mode, skip self-authored messages
        if not should_log_in_mode(
            account_type=self._account_type,
            is_mention=False,  # not relevant for logging decision
            is_dm=is_dm,
            is_self=is_self_message(event, self._own_user_id),
        ):
            log.debug("Skipping self-message ts=%s in person mode", ts)
            return

        # Dedup check (atomic check-and-mark)
        if not self._dedup.check_and_mark(ts, channel_id):
            log.debug("Duplicate event ts=%s channel=%s, skipping", ts, channel_id)
            return

        # Build record (pure)
        record = build_record(
            event=event,
            channel_id=channel_id,
            channel_name=channel_name,
            user_id=user_id,
            username=username,
            display_name=display_name,
            is_dm=is_dm,
        )

        # Write to JSONL file (side effect)
        path = log_path_for_event(
            channel_id=channel_id,
            is_dm=is_dm,
            log_root=self._log_root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

        log.debug("Logged event ts=%s to %s", ts, path)
