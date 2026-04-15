"""
Keyword Index — SQLite FTS5 full-text search over Slack message logs.

Provides near-real-time keyword search across all logged Slack messages.
Uses SQLite FTS5 with porter stemming for English-language queries.
Cursor-based incremental indexing prevents reprocessing.

Design principles:
- Pure query functions where possible
- Side effects (DB writes) isolated to index_messages / set_cursor
- Composable search with optional channel filtering
- Idempotent schema creation (safe to call repeatedly)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger("slack-keyword-index")

_DEFAULT_STATE_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
) / "slack-connector" / "state"

_DB_FILENAME = "keyword_index.db"

# ---------------------------------------------------------------------------
# Schema SQL (pure constants)
# ---------------------------------------------------------------------------

_CREATE_FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    ts, channel_id, user_id, text,
    tokenize='porter ascii'
);
"""

_CREATE_CURSOR_TABLE = """
CREATE TABLE IF NOT EXISTS index_cursors (
    channel_id TEXT PRIMARY KEY,
    last_ts TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _build_fts_row(msg: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Extract FTS-indexable fields from a message record.

    Pure function. Returns None if the message lacks required fields.
    """
    ts = msg.get("ts", "")
    channel_id = msg.get("channel_id", "")
    text = msg.get("text", "")

    if not ts or not channel_id or not text:
        return None

    user_id = msg.get("user_id", "")
    return (ts, channel_id, user_id, text)


def _filter_after_cursor(
    messages: list[dict[str, Any]], cursor_ts: str | None
) -> list[dict[str, Any]]:
    """Filter messages to only those with ts > cursor_ts.

    Pure function. Returns all messages if cursor_ts is None.
    """
    if cursor_ts is None:
        return messages
    return [m for m in messages if m.get("ts", "") > cursor_ts]


def _max_ts(messages: list[dict[str, Any]]) -> str | None:
    """Find the maximum timestamp from a list of messages.

    Pure function. Returns None for empty lists.
    """
    timestamps = [m.get("ts", "") for m in messages if m.get("ts")]
    return max(timestamps) if timestamps else None


# ---------------------------------------------------------------------------
# KeywordIndex
# ---------------------------------------------------------------------------


class KeywordIndex:
    """SQLite FTS5 keyword index for Slack messages.

    Manages a full-text search index backed by SQLite. Supports
    incremental indexing via cursor tracking per channel.
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir or _DEFAULT_STATE_DIR
        self._db_path = self._state_dir / _DB_FILENAME
        self._conn: sqlite3.Connection | None = None

    def _ensure_db(self) -> sqlite3.Connection:
        """Lazily create DB connection and ensure schema exists."""
        if self._conn is not None:
            return self._conn

        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_FTS_TABLE)
        self._conn.execute(_CREATE_CURSOR_TABLE)
        self._conn.commit()
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def index_messages(self, messages: list[dict[str, Any]]) -> int:
        """Index a batch of messages into FTS5.

        Skips messages missing required fields (ts, channel_id, text).
        Returns count of messages successfully indexed.
        """
        conn = self._ensure_db()
        rows = [
            row
            for msg in messages
            if (row := _build_fts_row(msg)) is not None
        ]

        if not rows:
            return 0

        conn.executemany(
            "INSERT INTO messages_fts(ts, channel_id, user_id, text) VALUES (?, ?, ?, ?);",
            rows,
        )
        conn.commit()
        log.info("Indexed %d messages into FTS5", len(rows))
        return len(rows)

    def search(
        self,
        query: str,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Full-text search over indexed messages.

        Uses FTS5 MATCH syntax (supports AND, OR, NOT, phrase queries).
        Optionally filters by channel_id.

        Returns list of matching records as dicts with keys:
        ts, channel_id, user_id, text, rank.
        """
        conn = self._ensure_db()

        if channel_id:
            sql = (
                "SELECT ts, channel_id, user_id, text, rank "
                "FROM messages_fts "
                "WHERE messages_fts MATCH ? AND channel_id = ? "
                "ORDER BY rank "
                "LIMIT ?;"
            )
            cursor = conn.execute(sql, (query, channel_id, limit))
        else:
            sql = (
                "SELECT ts, channel_id, user_id, text, rank "
                "FROM messages_fts "
                "WHERE messages_fts MATCH ? "
                "ORDER BY rank "
                "LIMIT ?;"
            )
            cursor = conn.execute(sql, (query, limit))

        return [
            {
                "ts": row[0],
                "channel_id": row[1],
                "user_id": row[2],
                "text": row[3],
                "rank": row[4],
            }
            for row in cursor.fetchall()
        ]

    def get_cursor(self, channel_id: str) -> str | None:
        """Get the last-indexed timestamp for a channel.

        Returns None if no messages have been indexed for this channel.
        """
        conn = self._ensure_db()
        row = conn.execute(
            "SELECT last_ts FROM index_cursors WHERE channel_id = ?;",
            (channel_id,),
        ).fetchone()
        return row[0] if row else None

    def set_cursor(self, channel_id: str, ts: str) -> None:
        """Update the last-indexed timestamp for a channel.

        Uses INSERT OR REPLACE for idempotent upsert.
        """
        conn = self._ensure_db()
        conn.execute(
            "INSERT OR REPLACE INTO index_cursors(channel_id, last_ts) VALUES (?, ?);",
            (channel_id, ts),
        )
        conn.commit()
