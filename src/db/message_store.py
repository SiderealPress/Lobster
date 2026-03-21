"""
src/db/message_store.py — Live DB write path for Lobster messages (BIS-163 Slice 2)

Provides pure-functional helpers for persisting messages to messages.db at the
moment they are received or sent — as opposed to the batch migration in
scripts/migrate_json_to_db.py which back-fills historical JSON files.

Design:
  - All public functions are pure transforms (dict -> None) with isolated side
    effects in _write_to_db.
  - Connections are short-lived: open -> execute -> commit -> close to keep the
    write path safe for multi-threaded callers (each call gets its own conn).
  - INSERT OR IGNORE semantics everywhere: idempotent by message id.
  - Failures are logged at WARNING level and swallowed — the JSON file path
    remains the source of truth; the DB write is additive.
  - The DB path defaults to ~/messages/messages.db but can be overridden via
    the LOBSTER_MESSAGES_DB env var.

Public API:
    persist_inbound(record: dict) -> None
    persist_outbound(record: dict) -> None
    persist_agent_event(record: dict) -> None
    persist_message(record: dict, direction: str) -> None

Classification rules mirror scripts/migrate_json_to_db.py:
    agent_events  <- types: subagent_result | subagent_notification |
                            subagent_error | agent_failed | task-notification
    bisque_events <- source == 'bisque'
    messages      <- everything else
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Final

log = logging.getLogger("lobster-mcp")

# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

_DEFAULT_DB: Final[Path] = Path.home() / "messages" / "messages.db"


def _db_path() -> Path:
    """Return the messages.db path, honouring LOBSTER_MESSAGES_DB env var."""
    env = os.environ.get("LOBSTER_MESSAGES_DB", "")
    return Path(env) if env else _DEFAULT_DB


# ---------------------------------------------------------------------------
# Lazy schema initialisation
#
# The schema is applied once per process lifetime.  A threading.Lock guards
# against concurrent first-write races in multi-threaded contexts.
# ---------------------------------------------------------------------------

_schema_applied: bool = False
_schema_lock: threading.Lock = threading.Lock()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the bundled schema.sql to *conn* if not yet applied this process."""
    global _schema_applied
    if _schema_applied:
        return
    with _schema_lock:
        if _schema_applied:
            return  # double-checked locking
        try:
            from db.connection import apply_schema as _apply_schema
            _apply_schema(conn)
            _schema_applied = True
        except Exception as exc:
            log.warning(f"[message_store] schema apply failed: {exc}")


# ---------------------------------------------------------------------------
# Connection factory — short-lived, one per write
# ---------------------------------------------------------------------------


def _open_conn() -> sqlite3.Connection:
    """Open a messages.db connection with the required PRAGMAs."""
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# ---------------------------------------------------------------------------
# Classification — pure function, no I/O
# ---------------------------------------------------------------------------

_AGENT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "subagent_result",
        "subagent_notification",
        "subagent_error",
        "agent_failed",
        "task-notification",
    }
)


def classify(record: dict, direction: str) -> str:
    """
    Return the destination table name for *record*.

    Pure function — mirrors scripts/migrate_json_to_db.py classify() exactly
    so both code paths produce identical table routing.

    Args:
        record:    Message dict (the JSON body written to disk).
        direction: 'in' for inbound messages, 'out' for outbound replies.

    Returns:
        One of 'messages', 'bisque_events', 'agent_events'.
    """
    msg_type: str = record.get("type") or ""
    source: str = record.get("source") or ""
    if msg_type in _AGENT_EVENT_TYPES:
        return "agent_events"
    if source == "bisque":
        return "bisque_events"
    return "messages"


# ---------------------------------------------------------------------------
# Field coercers — pure, no I/O
# ---------------------------------------------------------------------------


def _str(v: object) -> str | None:
    return str(v) if v is not None else None


def _int(v: object) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _bool_int(v: object) -> int | None:
    if v is None:
        return None
    return 1 if v else 0


def _json_str(v: object) -> str | None:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Known field sets — used for overflow/extra detection in messages table
# ---------------------------------------------------------------------------

_MESSAGES_KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id", "direction", "source", "type",
        "chat_id", "user_id", "username", "user_name",
        "text", "reply_to", "reply_to_message_id",
        "image_file", "image_width", "image_height",
        "audio_file", "audio_duration", "audio_mime_type",
        "transcription", "transcribed_at", "transcription_model",
        "file_path", "file_name", "mime_type", "file_size",
        "telegram_message_id", "callback_data", "callback_query_id",
        "original_message_id", "original_message_text", "media_group_id",
        "timestamp", "imported_at", "extra",
        # pre-processed or internal-only fields that don't go to extra
        "_processing_started_at",
        "forward", "attachments", "buttons", "photo_url", "caption",
    }
)

# ---------------------------------------------------------------------------
# Row builders — pure functions, dict -> dict
# ---------------------------------------------------------------------------


def build_message_row(record: dict, direction: str) -> dict:
    """
    Build a sqlite-ready dict for the *messages* table from a raw message record.

    Unknown/overflow fields are folded into the JSON 'extra' column so no data
    is silently discarded.

    Args:
        record:    Raw message dict.
        direction: 'in' or 'out'.

    Returns:
        Dict keyed by messages table column names.
    """
    overflow = {
        k: v
        for k, v in record.items()
        if k not in _MESSAGES_KNOWN_FIELDS and v is not None
    }
    return {
        "id":                    _str(record.get("id")),
        "direction":             direction,
        "source":                _str(record.get("source")),
        "type":                  _str(record.get("type")),
        "chat_id":               _str(record.get("chat_id")),
        "user_id":               _str(record.get("user_id")),
        "username":              _str(record.get("username")),
        "user_name":             _str(record.get("user_name")),
        "text":                  _str(record.get("text")),
        "reply_to":              _str(record.get("reply_to")),
        "reply_to_message_id":   _str(record.get("reply_to_message_id")),
        "image_file":            _str(record.get("image_file")),
        "image_width":           _int(record.get("image_width")),
        "image_height":          _int(record.get("image_height")),
        "audio_file":            _str(record.get("audio_file")),
        "audio_duration":        _int(record.get("audio_duration")),
        "audio_mime_type":       _str(record.get("audio_mime_type")),
        "transcription":         _str(record.get("transcription")),
        "transcribed_at":        _str(record.get("transcribed_at")),
        "transcription_model":   _str(record.get("transcription_model")),
        "file_path":             _str(record.get("file_path")),
        "file_name":             _str(record.get("file_name")),
        "mime_type":             _str(record.get("mime_type")),
        "file_size":             _int(record.get("file_size")),
        "telegram_message_id":   _str(record.get("telegram_message_id")),
        "callback_data":         _str(record.get("callback_data")),
        "callback_query_id":     _str(record.get("callback_query_id")),
        "original_message_id":   _str(record.get("original_message_id")),
        "original_message_text": _str(record.get("original_message_text")),
        "media_group_id":        _str(record.get("media_group_id")),
        "timestamp":             _str(record.get("timestamp")),
        "extra":                 _json_str(overflow) if overflow else None,
    }


def build_bisque_event_row(record: dict) -> dict:
    """
    Build a sqlite-ready dict for the *bisque_events* table.

    Args:
        record: Raw bisque message dict.

    Returns:
        Dict keyed by bisque_events table column names.
    """
    return {
        "id":                  _str(record.get("id")),
        "chat_id":             _str(record.get("chat_id")),
        "type":                _str(record.get("type")),
        "text":                _str(record.get("text")),
        "reply_to_id":         _str(record.get("reply_to_id")),
        "reply_to":            _str(record.get("reply_to")),
        "audio_file":          _str(record.get("audio_file")),
        "transcription":       _str(record.get("transcription")),
        "transcribed_at":      _str(record.get("transcribed_at")),
        "transcription_model": _str(record.get("transcription_model")),
        "task_id":             _str(record.get("task_id")),
        "agent_id":            _str(record.get("agent_id")),
        "status":              _str(record.get("status")),
        "sent_reply_to_user":  _bool_int(record.get("sent_reply_to_user")),
        "attachments":         _json_str(record.get("attachments")),
        "warning":             _str(record.get("warning")),
        "timestamp":           _str(record.get("timestamp")),
    }


def build_agent_event_row(record: dict) -> dict:
    """
    Build a sqlite-ready dict for the *agent_events* table.

    Args:
        record: Raw agent event dict.

    Returns:
        Dict keyed by agent_events table column names.
    """
    return {
        "id":                 _str(record.get("id")),
        "type":               _str(record.get("type")),
        "source":             _str(record.get("source")),
        "chat_id":            _str(record.get("chat_id")),
        "task_id":            _str(record.get("task_id")),
        "agent_id":           _str(record.get("agent_id")),
        "status":             _str(record.get("status")),
        "text":               _str(record.get("text")),
        "sent_reply_to_user": _bool_int(record.get("sent_reply_to_user")),
        "warning":            _str(record.get("warning")),
        "original_chat_id":   _str(record.get("original_chat_id")),
        "original_prompt":    _str(record.get("original_prompt")),
        "last_output":        _str(record.get("last_output")),
        "artifacts":          _json_str(record.get("artifacts")),
        "forward":            _json_str(record.get("forward")),
        "timestamp":          _str(record.get("timestamp")),
    }


# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

_INSERT_MESSAGE: Final[str] = """
INSERT OR IGNORE INTO messages (
    id, direction, source, type,
    chat_id, user_id, username, user_name,
    text, reply_to, reply_to_message_id,
    image_file, image_width, image_height,
    audio_file, audio_duration, audio_mime_type,
    transcription, transcribed_at, transcription_model,
    file_path, file_name, mime_type, file_size,
    telegram_message_id, callback_data, callback_query_id,
    original_message_id, original_message_text, media_group_id,
    timestamp, extra
) VALUES (
    :id, :direction, :source, :type,
    :chat_id, :user_id, :username, :user_name,
    :text, :reply_to, :reply_to_message_id,
    :image_file, :image_width, :image_height,
    :audio_file, :audio_duration, :audio_mime_type,
    :transcription, :transcribed_at, :transcription_model,
    :file_path, :file_name, :mime_type, :file_size,
    :telegram_message_id, :callback_data, :callback_query_id,
    :original_message_id, :original_message_text, :media_group_id,
    :timestamp, :extra
)
""".strip()

_INSERT_BISQUE: Final[str] = """
INSERT OR IGNORE INTO bisque_events (
    id, chat_id, type, text, reply_to_id, reply_to,
    audio_file, transcription, transcribed_at, transcription_model,
    task_id, agent_id, status, sent_reply_to_user,
    attachments, warning, timestamp
) VALUES (
    :id, :chat_id, :type, :text, :reply_to_id, :reply_to,
    :audio_file, :transcription, :transcribed_at, :transcription_model,
    :task_id, :agent_id, :status, :sent_reply_to_user,
    :attachments, :warning, :timestamp
)
""".strip()

_INSERT_AGENT: Final[str] = """
INSERT OR IGNORE INTO agent_events (
    id, type, source, chat_id,
    task_id, agent_id, status, text,
    sent_reply_to_user, warning,
    original_chat_id, original_prompt, last_output,
    artifacts, forward, timestamp
) VALUES (
    :id, :type, :source, :chat_id,
    :task_id, :agent_id, :status, :text,
    :sent_reply_to_user, :warning,
    :original_chat_id, :original_prompt, :last_output,
    :artifacts, :forward, :timestamp
)
""".strip()


# ---------------------------------------------------------------------------
# Low-level write helper — side effects isolated here
# ---------------------------------------------------------------------------


def _write_to_db(sql: str, row: dict) -> None:
    """
    Execute *sql* with *row* bindings against messages.db.

    Opens a fresh connection, applies schema on first call (process-lifetime),
    commits, then closes.  Swallows all exceptions after logging — the DB write
    path must never break the caller's primary JSON write.

    Args:
        sql: Parameterised INSERT statement (named :param style).
        row: Dict of column values to bind.
    """
    try:
        conn = _open_conn()
        try:
            _ensure_schema(conn)
            conn.execute(sql, row)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning(f"[message_store] DB write failed (id={row.get('id')!r}): {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def persist_message(record: dict, direction: str) -> None:
    """
    Classify *record* and persist it to the appropriate messages.db table.

    This is the primary entry point for the live write path.  It mirrors the
    classification logic in scripts/migrate_json_to_db.py so both code paths
    produce identical table routing.

    Args:
        record:    Raw message dict (the same data written to the JSON file).
        direction: 'in' for inbound messages, 'out' for outbound replies.
    """
    table = classify(record, direction)
    if table == "agent_events":
        row = build_agent_event_row(record)
        _write_to_db(_INSERT_AGENT, row)
    elif table == "bisque_events":
        row = build_bisque_event_row(record)
        _write_to_db(_INSERT_BISQUE, row)
    else:
        row = build_message_row(record, direction)
        _write_to_db(_INSERT_MESSAGE, row)


def persist_inbound(record: dict) -> None:
    """
    Persist an inbound message (direction='in') to messages.db.

    Convenience wrapper around persist_message.  Call this after the inbound
    JSON file is fully processed (i.e. at mark_processed time or via the
    atomic mark_processed path in send_reply).

    Args:
        record: Inbound message dict.
    """
    persist_message(record, "in")


def persist_outbound(record: dict) -> None:
    """
    Persist an outbound reply (direction='out') to messages.db.

    Convenience wrapper around persist_message.  Call this after the JSON is
    written to sent/ so the DB mirrors the filesystem state.

    Args:
        record: Outbound reply dict (as written to SENT_DIR).
    """
    persist_message(record, "out")


def persist_agent_event(record: dict) -> None:
    """
    Persist an agent event directly to the agent_events table.

    Bypasses classification since the caller (handle_write_result) already
    knows this is an agent event.  Persisting at write_result time — before
    the dispatcher's mark_processed — ensures the DB is populated even if
    the dispatcher crashes mid-flight.  INSERT OR IGNORE makes the subsequent
    mark_processed DB persist a no-op (idempotent).

    Args:
        record: Agent event dict (subagent_result, subagent_error, etc.).
    """
    row = build_agent_event_row(record)
    _write_to_db(_INSERT_AGENT, row)
