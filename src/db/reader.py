"""
src/db/reader.py — Pure-functional read layer for messages.db (BIS-164 Slice 3)

Provides composable query functions that serve message reads from the SQLite
database instead of scanning JSON files on disk.  Every function is pure with
respect to in-memory state — all I/O is explicit via the *conn* parameter.

Design principles:
  - All public functions accept an open sqlite3.Connection (injected dependency)
  - Return types are plain Python dicts/lists — no ORM objects
  - Queries prefer explicit column lists over SELECT * for stability
  - FTS5 is used for keyword search; LIKE fallback when FTS5 is unavailable
  - Pagination (limit/offset) is delegated to SQL, not Python
  - None is returned when a record is not found (callers decide how to react)

The relay layer (inbox_server.py) calls these functions and falls back to the
existing filesystem scan only when the DB is not available or the query returns
no results (dual-read mode during the migration window).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import canonical user types for sender_type filtering.
# message_types.py lives alongside inbox_server.py in src/mcp/.
_MCP_DIR = str(Path(__file__).resolve().parent.parent / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)
try:
    from message_types import INBOX_USER_TYPES as _INBOX_USER_TYPES
except ImportError:  # pragma: no cover — fallback when run outside normal tree
    _INBOX_USER_TYPES = frozenset()

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Row = dict[str, Any]


# ---------------------------------------------------------------------------
# Internal helpers — pure transformations
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> Row:
    """Convert a sqlite3.Row to a plain Python dict."""
    return dict(row)


def _merge_extra(row_dict: Row) -> Row:
    """
    Expand the 'extra' JSON column back into the top-level dict and drop the
    raw 'extra' key.  If 'extra' is absent or invalid JSON, return unchanged.
    """
    extra_raw = row_dict.pop("extra", None)
    if not extra_raw:
        return row_dict
    try:
        extra = json.loads(extra_raw)
        if isinstance(extra, dict):
            row_dict.update(extra)
    except (json.JSONDecodeError, TypeError):
        pass
    return row_dict


def _normalize_direction(row_dict: Row) -> Row:
    """
    Map the DB 'direction' column ('in'/'out') to a '_direction' key using
    the vocabulary expected by conversation-history formatters.
    """
    direction = row_dict.get("direction", "in")
    row_dict["_direction"] = "received" if direction == "in" else "sent"
    return row_dict


def _parse_timestamp(ts: str | None) -> datetime:
    """
    Parse an ISO-8601 timestamp string into a timezone-aware datetime.
    Returns datetime.min (UTC) for missing or malformed values so that
    sort comparisons never raise.
    """
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Sender-type filter helpers — pure functions
# ---------------------------------------------------------------------------

def _sender_type_sql(sender_type: str | None) -> tuple[list[str], list[Any]]:
    """
    Return (conditions, params) for a sender_type filter.

    sender_type semantics:
      "user"         — inbound messages whose type is a user-initiated type
                       (text, photo, voice, document, etc. — excludes system noise)
      "lobster"      — all outbound messages (direction='out')
      "conversation" — union of user and lobster (real conversation, no cron/system)
      None / other   — no additional conditions (all messages)

    Returns plain lists so callers can append to their existing conditions/params.
    This is a pure function with no side effects.
    """
    if not sender_type or sender_type == "all":
        return [], []

    # Build the IN-list for user types once, using positional parameters.
    user_types = sorted(_INBOX_USER_TYPES)  # sorted for deterministic SQL
    placeholders = ", ".join("?" * len(user_types))
    user_clause = f"(direction = 'in' AND type IN ({placeholders}))"

    if sender_type == "user":
        return [user_clause], list(user_types)

    if sender_type == "lobster":
        return ["direction = 'out'"], []

    if sender_type == "conversation":
        # outbound OR inbound-user-type
        return [f"(direction = 'out' OR {user_clause})"], list(user_types)

    # Unknown value — treat as no filter (safe degradation)
    return [], []


# ---------------------------------------------------------------------------
# Table detection
# ---------------------------------------------------------------------------

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if *table* exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

_HISTORY_COLUMNS = (
    "id, direction, source, type, chat_id, user_id, username, user_name, "
    "text, reply_to, reply_to_message_id, "
    "image_file, image_width, image_height, audio_file, audio_duration, "
    "audio_mime_type, transcription, transcribed_at, transcription_model, "
    "file_path, file_name, mime_type, file_size, "
    "telegram_message_id, callback_data, callback_query_id, "
    "original_message_id, original_message_text, media_group_id, "
    "timestamp, extra"
)

# Table-qualified version for use in JOINs (e.g. FTS5 queries against messages_fts)
# where column names like 'source', 'type', 'text' exist in multiple tables and
# SQLite raises "ambiguous column name" without explicit qualification.
_HISTORY_COLUMNS_M = ", ".join(
    f"m.{col.strip()}" for col in _HISTORY_COLUMNS.split(",")
)


def get_conversation_history(
    conn: sqlite3.Connection,
    *,
    chat_id: str | int | None = None,
    source: str | None = None,
    search: str | None = None,
    direction: str = "all",
    sender_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[Row]:
    """
    Fetch conversation history from the messages table.

    Applies optional filters for chat_id, source, full-text search,
    direction (received/sent/all), and sender_type.  Results are ordered
    newest-first.

    Args:
        conn:        Open sqlite3.Connection.
        chat_id:     Filter to a specific conversation (compared as string).
        source:      Filter by message source (e.g. 'telegram', 'bisque').
        search:      Keyword search over message text (uses FTS5 when available).
        direction:   'received' | 'sent' | 'all' — maps to direction IN/OUT.
        sender_type: 'user' | 'lobster' | 'conversation' | None
                     'user'         — inbound user-type messages only (no system/cron)
                     'lobster'      — outbound messages only
                     'conversation' — both user and lobster (excludes system noise)
                     None / omitted — all messages (current default behaviour)
        limit:       Maximum number of rows to return.
        offset:      Rows to skip for pagination.

    Returns:
        List of plain dicts with all message columns plus '_direction'.
        Extra overflow fields are merged into the top-level dict.
    """
    conditions: list[str] = []
    params: list[Any] = []

    # sender_type takes precedence over direction when set, because it encodes
    # its own directionality.  When sender_type is given, the direction param
    # is ignored to avoid conflicting SQL conditions.
    if sender_type and sender_type != "all":
        st_conds, st_params = _sender_type_sql(sender_type)
        conditions.extend(st_conds)
        params.extend(st_params)
    else:
        # Direction filter (legacy path — only applied when sender_type is absent)
        if direction == "received":
            conditions.append("direction = 'in'")
        elif direction == "sent":
            conditions.append("direction = 'out'")

    # chat_id filter (coerce to string for uniform comparison)
    if chat_id is not None:
        conditions.append("chat_id = ?")
        params.append(str(chat_id))

    # source filter (case-insensitive)
    if source:
        conditions.append("LOWER(source) = LOWER(?)")
        params.append(source)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    if search:
        # Prefer FTS5 — fast and ranking-aware
        use_fts = _table_exists(conn, "messages_fts")
        if use_fts:
            # Re-qualify conditions for the FTS JOIN context.  The messages_fts
            # virtual table exposes: text, transcription, user_name, source, type —
            # the same names as columns in messages.  Without table prefixes SQLite
            # raises "ambiguous column name".  We rebuild the condition list with
            # bare column references replaced by m.<col>.
            fts_conditions = [
                c.replace("LOWER(source)", "LOWER(m.source)")
                 .replace("LOWER(type)", "LOWER(m.type)")
                 .replace("LOWER(text)", "LOWER(m.text)")
                 .replace("LOWER(transcription)", "LOWER(m.transcription)")
                 .replace("LOWER(user_name)", "LOWER(m.user_name)")
                 .replace("direction =", "m.direction =")
                 .replace("chat_id =", "m.chat_id =")
                 .replace(" type IN", " m.type IN")
                for c in conditions
            ]
            fts_where_extra = ("AND " + " AND ".join(fts_conditions)) if fts_conditions else ""
            fts_query = (
                f"SELECT {_HISTORY_COLUMNS_M} "
                f"FROM messages m "
                f"JOIN messages_fts f ON f.rowid = m.rowid "
                f"WHERE messages_fts MATCH ? "
                f"{fts_where_extra} "
                f"ORDER BY m.timestamp DESC "
                f"LIMIT ? OFFSET ?"
            )
            rows = conn.execute(
                fts_query, [search] + params + [limit, offset]
            ).fetchall()
        else:
            # LIKE fallback — slower but always works
            if conditions:
                where_clause += " AND LOWER(text) LIKE LOWER(?)"
            else:
                where_clause = "WHERE LOWER(text) LIKE LOWER(?)"
            params.append(f"%{search}%")
            rows = conn.execute(
                f"SELECT {_HISTORY_COLUMNS} FROM messages {where_clause}"
                f" ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_HISTORY_COLUMNS} FROM messages {where_clause}"
            f" ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    return [_normalize_direction(_merge_extra(_row_to_dict(r))) for r in rows]


def count_conversation_history(
    conn: sqlite3.Connection,
    *,
    chat_id: str | int | None = None,
    source: str | None = None,
    search: str | None = None,
    direction: str = "all",
    sender_type: str | None = None,
) -> int:
    """
    Return the total number of messages matching the given filters (for pagination).

    Uses the same filter logic as get_conversation_history but returns only
    the row count, avoiding the overhead of fetching full rows.

    sender_type: see get_conversation_history for semantics.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if sender_type and sender_type != "all":
        st_conds, st_params = _sender_type_sql(sender_type)
        conditions.extend(st_conds)
        params.extend(st_params)
    else:
        if direction == "received":
            conditions.append("direction = 'in'")
        elif direction == "sent":
            conditions.append("direction = 'out'")

    if chat_id is not None:
        conditions.append("chat_id = ?")
        params.append(str(chat_id))

    if source:
        conditions.append("LOWER(source) = LOWER(?)")
        params.append(source)

    if search:
        use_fts = _table_exists(conn, "messages_fts")
        if use_fts:
            # Re-qualify conditions for the FTS JOIN context (same fix as
            # get_conversation_history — messages_fts shares column names with messages).
            fts_conditions = [
                c.replace("LOWER(source)", "LOWER(m.source)")
                 .replace("LOWER(type)", "LOWER(m.type)")
                 .replace("LOWER(text)", "LOWER(m.text)")
                 .replace("LOWER(transcription)", "LOWER(m.transcription)")
                 .replace("LOWER(user_name)", "LOWER(m.user_name)")
                 .replace("direction =", "m.direction =")
                 .replace("chat_id =", "m.chat_id =")
                 .replace(" type IN", " m.type IN")
                for c in conditions
            ]
            fts_where_extra = ("AND " + " AND ".join(fts_conditions)) if fts_conditions else ""
            fts_query = (
                f"SELECT COUNT(*) "
                f"FROM messages m "
                f"JOIN messages_fts f ON f.rowid = m.rowid "
                f"WHERE messages_fts MATCH ? "
                f"{fts_where_extra}"
            )
            (count,) = conn.execute(fts_query, [search] + params).fetchone()
            return count
        else:
            if conditions:
                conditions.append("LOWER(text) LIKE LOWER(?)")
            else:
                conditions = ["LOWER(text) LIKE LOWER(?)"]
            params.append(f"%{search}%")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    (count,) = conn.execute(
        f"SELECT COUNT(*) FROM messages {where_clause}", params
    ).fetchone()
    return count


# ---------------------------------------------------------------------------
# Message lookup by ID
# ---------------------------------------------------------------------------

def get_message_by_id(
    conn: sqlite3.Connection,
    message_id: str,
) -> Row | None:
    """
    Look up a single message from the messages table by its primary key.

    Returns the row as a plain dict (extra fields merged), or None if not found.
    """
    row = conn.execute(
        f"SELECT {_HISTORY_COLUMNS} FROM messages WHERE id = ? LIMIT 1",
        (message_id,),
    ).fetchone()
    if row is not None:
        return _normalize_direction(_merge_extra(_row_to_dict(row)))
    return None


def get_message_by_telegram_id(
    conn: sqlite3.Connection,
    telegram_message_id: int,
    chat_id: str | int | None = None,
) -> Row | None:
    """
    Look up a message by its Telegram message ID.

    Args:
        conn:               Open sqlite3.Connection.
        telegram_message_id: Telegram's numeric message identifier.
        chat_id:            Optional — narrow the search to a specific chat.

    Returns:
        The first matching row as a plain dict, or None.
    """
    conditions = ["telegram_message_id = ?"]
    params: list[Any] = [str(telegram_message_id)]

    if chat_id is not None:
        conditions.append("chat_id = ?")
        params.append(str(chat_id))

    where = " AND ".join(conditions)
    row = conn.execute(
        f"SELECT {_HISTORY_COLUMNS} FROM messages WHERE {where} LIMIT 1",
        params,
    ).fetchone()
    if row is not None:
        return _normalize_direction(_merge_extra(_row_to_dict(row)))
    return None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def get_message_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    Return aggregate statistics from the messages, bisque_events, and
    agent_events tables.

    Returns a dict with keys:
        - total_messages:     int  total rows in messages table
        - inbound_count:      int  direction='in' rows
        - outbound_count:     int  direction='out' rows
        - by_source:          dict[str, int]  count per source
        - agent_events_count: int  rows in agent_events table
        - bisque_events_count: int rows in bisque_events table
    """
    stats: dict[str, Any] = {}

    if _table_exists(conn, "messages"):
        (total,) = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        stats["total_messages"] = total

        (inbound,) = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'in'"
        ).fetchone()
        stats["inbound_count"] = inbound
        stats["outbound_count"] = total - inbound

        source_rows = conn.execute(
            "SELECT source, COUNT(*) FROM messages GROUP BY source ORDER BY COUNT(*) DESC"
        ).fetchall()
        stats["by_source"] = {r[0] or "unknown": r[1] for r in source_rows}
    else:
        stats["total_messages"] = 0
        stats["inbound_count"] = 0
        stats["outbound_count"] = 0
        stats["by_source"] = {}

    if _table_exists(conn, "agent_events"):
        (agent_count,) = conn.execute("SELECT COUNT(*) FROM agent_events").fetchone()
        stats["agent_events_count"] = agent_count
    else:
        stats["agent_events_count"] = 0

    if _table_exists(conn, "bisque_events"):
        (bisque_count,) = conn.execute("SELECT COUNT(*) FROM bisque_events").fetchone()
        stats["bisque_events_count"] = bisque_count
    else:
        stats["bisque_events_count"] = 0

    return stats


def get_recent_messages(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    since_ts: str | None = None,
    limit: int = 20,
) -> list[Row]:
    """
    Fetch recent inbound messages from messages.db, ordered newest-first.

    This is the DB-backed complement to the filesystem inbox scan used by
    handle_check_inbox's since_ts (catch-up) mode.

    Args:
        conn:     Open sqlite3.Connection.
        source:   Optional source filter (e.g. 'telegram').
        since_ts: ISO-8601 timestamp — only messages at or after this time.
        limit:    Maximum rows to return.

    Returns:
        List of plain dicts with all message columns.
    """
    conditions: list[str] = ["direction = 'in'"]
    params: list[Any] = []

    if source:
        conditions.append("LOWER(source) = LOWER(?)")
        params.append(source)

    if since_ts:
        conditions.append("timestamp >= ?")
        params.append(since_ts)

    where = "WHERE " + " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT {_HISTORY_COLUMNS} FROM messages {where}"
        f" ORDER BY timestamp DESC LIMIT ?",
        params + [limit],
    ).fetchall()

    return [_merge_extra(_row_to_dict(r)) for r in rows]
