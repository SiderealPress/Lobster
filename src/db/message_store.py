"""
src/db/message_store.py — Live DB write path for Lobster messages (BIS-163 Slice 2)

Updated in BIS-167 Slice 6 to add the LOBSTER_USE_DB feature flag that controls
whether live writes are active.  The flag is off by default so the cutover is
safe to deploy before the operator is ready to enable it.

Provides pure-functional helpers for persisting messages to messages.db at the
moment they are received or sent — as opposed to the batch migration in
scripts/migrate_json_to_db.py which back-fills historical JSON files.

Feature flag:
  LOBSTER_USE_DB=1   — enable live DB writes (Slice 6 cutover active)
  LOBSTER_USE_DB=0   — disable (default; JSON files remain source of truth)

Design:
  - All public functions are pure transforms (dict -> None) with isolated side
    effects in _write_to_db.
  - Connections are short-lived: open -> execute -> commit -> close to keep the
    write path safe for multi-threaded callers (each call gets its own conn).
  - INSERT OR IGNORE semantics everywhere: idempotent by message id.
  - Failures are logged at WARNING level and swallowed — the JSON file path
    remains the source of truth; the DB write is additive.
  - The DB path defaults to ~/messages/messages.db; override via
    LOBSTER_MESSAGES_DB env var.

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
# Feature flag — BIS-167 Slice 6
# ---------------------------------------------------------------------------
# Set LOBSTER_USE_DB=1 in the environment to enable live DB writes.
# Any other value (including unset) leaves all public functions as no-ops
# so the cutover can be enabled per-instance without a code deploy.

_DB_ENABLED: bool = os.environ.get("LOBSTER_USE_DB", "0").strip() == "1"


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
            # Import here to avoid circular imports and allow the module to load
            # even when src/db/ is not on sys.path at import time.
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
# Field-set definitions (mirrors schema.sql)
# ---------------------------------------------------------------------------

_AGENT_EVENT_TYPES: frozenset[str] = frozenset({
    "subagent_result", "subagent_notification", "subagent_error",
    "agent_failed", "task-notification",
})

_MESSAGE_FIELDS: frozenset[str] = frozenset({
    "id", "direction", "source", "type",
    "chat_id", "user_id", "username", "user_name",
    "text", "reply_to", "reply_to_message_id",
    "image_file", "image_width", "image_height",
    "audio_file", "audio_duration", "audio_mime_type",
    "transcription", "transcribed_at", "transcription_model",
    "file_path", "file_name", "mime_type", "file_size",
    "telegram_message_id", "callback_data", "callback_query_id",
    "original_message_id", "original_message_text", "media_group_id",
    "timestamp",
})

_BISQUE_FIELDS: frozenset[str] = frozenset({
    "id", "chat_id", "type", "text",
    "reply_to_id", "reply_to",
    "audio_file", "transcription", "transcribed_at", "transcription_model",
    "task_id", "agent_id", "status", "sent_reply_to_user",
    "attachments", "warning", "timestamp",
})

_AGENT_FIELDS: frozenset[str] = frozenset({
    "id", "type", "source", "chat_id",
    "task_id", "agent_id",
    "status", "text", "sent_reply_to_user", "warning",
    "original_chat_id", "original_prompt", "last_output",
    "artifacts", "forward", "timestamp",
})

# ---------------------------------------------------------------------------
# Row builders — pure functions (dict -> dict)
# ---------------------------------------------------------------------------

_SKIP_INTERNAL: frozenset[str] = frozenset({
    "_processing_started_at", "_permanently_failed", "_retry_at", "_retry_count",
})


def _coerce(v: object) -> object:
    """Coerce Python values to SQLite-safe types: bool->int, list/dict->JSON."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


def _split(msg: dict, known: frozenset[str]) -> tuple[dict, dict]:
    """Split *msg* into (known_fields, overflow_fields).

    Fields in *_SKIP_INTERNAL* are excluded entirely.
    Pure function — does not mutate *msg*.
    """
    row: dict = {}
    overflow: dict = {}
    for k, v in msg.items():
        if k in _SKIP_INTERNAL:
            continue
        if k in known:
            row[k] = _coerce(v)
        else:
            overflow[k] = v
    return row, overflow


def classify(record: dict, direction: str) -> str:
    """Return the target table name for *record*.

    Pure function — no side effects.
    """
    msg_type = record.get("type", "")
    source = record.get("source", "")
    if msg_type in _AGENT_EVENT_TYPES:
        return "agent_events"
    if source == "bisque":
        return "bisque_events"
    return "messages"


def build_message_row(record: dict, direction: str) -> dict:
    """Build an INSERT row for the *messages* table.

    Pure function: returns a new dict, does not mutate *record*.
    """
    row, overflow = _split(record, _MESSAGE_FIELDS)
    row.setdefault("id", record.get("id", ""))
    row["direction"] = direction
    row.setdefault("source", record.get("source", "unknown"))
    row.setdefault("timestamp", record.get("timestamp", ""))
    if overflow:
        row["extra"] = json.dumps(overflow, ensure_ascii=False, default=str)
    return row


def build_bisque_event_row(record: dict) -> dict:
    """Build an INSERT row for the *bisque_events* table.

    Pure function.
    """
    row, _ = _split(record, _BISQUE_FIELDS)
    row.setdefault("id", record.get("id", ""))
    row.setdefault("timestamp", record.get("timestamp", ""))
    return row


def build_agent_event_row(record: dict) -> dict:
    """Build an INSERT row for the *agent_events* table.

    Pure function.
    """
    row, _ = _split(record, _AGENT_FIELDS)
    row.setdefault("id", record.get("id", ""))
    row.setdefault("type", record.get("type", "unknown"))
    row.setdefault("timestamp", record.get("timestamp", ""))
    # Serialise list/dict fields
    for field in ("artifacts", "forward"):
        if field in row and isinstance(row[field], str):
            try:
                json.loads(row[field])  # already JSON string
            except (json.JSONDecodeError, TypeError):
                row[field] = json.dumps(row[field], ensure_ascii=False, default=str)
    return row


# ---------------------------------------------------------------------------
# INSERT templates
# ---------------------------------------------------------------------------

_INSERT_MESSAGE = """
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

_INSERT_BISQUE = """
INSERT OR IGNORE INTO bisque_events (
    id, chat_id, type, text,
    reply_to_id, reply_to,
    audio_file, transcription, transcribed_at, transcription_model,
    task_id, agent_id, status, sent_reply_to_user,
    attachments, warning, timestamp
) VALUES (
    :id, :chat_id, :type, :text,
    :reply_to_id, :reply_to,
    :audio_file, :transcription, :transcribed_at, :transcription_model,
    :task_id, :agent_id, :status, :sent_reply_to_user,
    :attachments, :warning, :timestamp
)
""".strip()

_INSERT_AGENT = """
INSERT OR IGNORE INTO agent_events (
    id, type, source, chat_id,
    task_id, agent_id,
    status, text, sent_reply_to_user, warning,
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

    No-op when LOBSTER_USE_DB != '1' (BIS-167 Slice 6 feature flag).

    This is the primary entry point for the live write path.  It mirrors the
    classification logic in scripts/migrate_json_to_db.py so both code paths
    produce identical table routing.

    Args:
        record:    Raw message dict (the same data written to the JSON file).
        direction: 'in' for inbound messages, 'out' for outbound replies.
    """
    if not _DB_ENABLED:
        return
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

    No-op when LOBSTER_USE_DB != '1'.

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

    No-op when LOBSTER_USE_DB != '1'.

    Convenience wrapper around persist_message.  Call this after the JSON is
    written to sent/ so the DB mirrors the filesystem state.

    Args:
        record: Outbound reply dict (as written to SENT_DIR).
    """
    persist_message(record, "out")


def persist_agent_event(record: dict) -> None:
    """
    Persist an agent event directly to the agent_events table.

    No-op when LOBSTER_USE_DB != '1'.

    Bypasses classification since the caller (handle_write_result) already
    knows this is an agent event.  Persisting at write_result time — before
    the dispatcher's mark_processed — ensures the DB is populated even if
    the dispatcher crashes mid-flight.  INSERT OR IGNORE makes the subsequent
    mark_processed DB persist a no-op (idempotent).

    Args:
        record: Agent event dict (subagent_result, subagent_error, etc.).
    """
    if not _DB_ENABLED:
        return
    row = build_agent_event_row(record)
    _write_to_db(_INSERT_AGENT, row)
