#!/usr/bin/env python3
"""
scripts/migrate_json_to_db.py — Migrate processed/ and sent/ JSON files into messages.db

Reads every *.json file from:
  - ~/messages/processed/   → inbound messages (direction='in')
  - ~/messages/sent/        → outbound replies  (direction='out')

Classifies each message into one of three tables:
  - agent_events   — subagent_result | agent_failed | subagent_notification |
                     subagent_error | task-notification
  - bisque_events  — source == 'bisque'
  - messages       — everything else (both inbound and outbound)

Design:
  - Purely functional transforms: JSON dict → row dict, no mutation of inputs
  - Idempotent: INSERT OR IGNORE so re-runs are safe
  - Progress printed to stdout; errors to stderr
  - Configurable via CLI flags (see --help)

Usage:
    python scripts/migrate_json_to_db.py
    python scripts/migrate_json_to_db.py --db ~/messages/messages.db
    python scripts/migrate_json_to_db.py --processed ~/messages/processed \
                                          --sent ~/messages/sent \
                                          --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_SRC_DB = _REPO_ROOT / "src" / "db"

# Allow running the script from anywhere on the server
_DEFAULT_DB = Path.home() / "messages" / "messages.db"
_DEFAULT_PROCESSED = Path.home() / "messages" / "processed"
_DEFAULT_SENT = Path.home() / "messages" / "sent"

# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------

_AGENT_EVENT_TYPES = frozenset(
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

    Pure function — does not modify *record*.

    Args:
        record:    Parsed JSON dict from a message file.
        direction: 'in' (processed/) or 'out' (sent/).

    Returns:
        One of 'messages', 'bisque_events', 'agent_events'.
    """
    msg_type = record.get("type", "")
    source = record.get("source", "")

    if msg_type in _AGENT_EVENT_TYPES:
        return "agent_events"
    if source == "bisque":
        return "bisque_events"
    return "messages"


# ---------------------------------------------------------------------------
# Row builders — pure functions, JSON dict → sqlite-ready dict
# ---------------------------------------------------------------------------


def _str(v: object) -> str | None:
    """Coerce *v* to str, or None if falsy."""
    return str(v) if v is not None else None


def _int(v: object) -> int | None:
    """Coerce *v* to int, or None."""
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _bool_int(v: object) -> int | None:
    """Coerce *v* to 0/1 integer for SQLite BOOLEAN storage, or None."""
    if v is None:
        return None
    return 1 if v else 0


def _json_str(v: object) -> str | None:
    """Serialise *v* to a compact JSON string, or None if falsy."""
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


# Known fields on the messages table — used to extract 'extra' overflow
_MESSAGES_KNOWN_FIELDS = frozenset(
    {
        "id",
        "direction",
        "source",
        "type",
        "chat_id",
        "user_id",
        "username",
        "user_name",
        "text",
        "reply_to",
        "reply_to_message_id",
        "image_file",
        "image_width",
        "image_height",
        "audio_file",
        "audio_duration",
        "audio_mime_type",
        "transcription",
        "transcribed_at",
        "transcription_model",
        "file_path",
        "file_name",
        "mime_type",
        "file_size",
        "telegram_message_id",
        "callback_data",
        "callback_query_id",
        "original_message_id",
        "original_message_text",
        "media_group_id",
        "timestamp",
        "imported_at",
        "extra",
        # raw JSON fields that are pre-processed
        "_processing_started_at",
        "forward",
        "attachments",
        "buttons",
        "photo_url",
        "caption",
    }
)


def build_message_row(record: dict, direction: str) -> dict:
    """
    Build a dict suitable for INSERT INTO messages from a raw JSON record.

    Extra/unknown fields are folded into the 'extra' JSON column.

    Args:
        record:    Raw JSON dict from a processed or sent message file.
        direction: 'in' or 'out'.

    Returns:
        A dict whose keys match the messages table columns.
    """
    # Gather overflow fields before building the row
    overflow = {
        k: v
        for k, v in record.items()
        if k not in _MESSAGES_KNOWN_FIELDS and v is not None
    }

    return {
        "id": _str(record.get("id")),
        "direction": direction,
        "source": _str(record.get("source")),
        "type": _str(record.get("type")),
        "chat_id": _str(record.get("chat_id")),
        "user_id": _str(record.get("user_id")),
        "username": _str(record.get("username")),
        "user_name": _str(record.get("user_name")),
        "text": _str(record.get("text")),
        "reply_to": _str(record.get("reply_to")),
        "reply_to_message_id": _str(record.get("reply_to_message_id")),
        "image_file": _str(record.get("image_file")),
        "image_width": _int(record.get("image_width")),
        "image_height": _int(record.get("image_height")),
        "audio_file": _str(record.get("audio_file")),
        "audio_duration": _int(record.get("audio_duration")),
        "audio_mime_type": _str(record.get("audio_mime_type")),
        "transcription": _str(record.get("transcription")),
        "transcribed_at": _str(record.get("transcribed_at")),
        "transcription_model": _str(record.get("transcription_model")),
        "file_path": _str(record.get("file_path")),
        "file_name": _str(record.get("file_name")),
        "mime_type": _str(record.get("mime_type")),
        "file_size": _int(record.get("file_size")),
        "telegram_message_id": _str(record.get("telegram_message_id")),
        "callback_data": _str(record.get("callback_data")),
        "callback_query_id": _str(record.get("callback_query_id")),
        "original_message_id": _str(record.get("original_message_id")),
        "original_message_text": _str(record.get("original_message_text")),
        "media_group_id": _str(record.get("media_group_id")),
        "timestamp": _str(record.get("timestamp")),
        "extra": _json_str(overflow) if overflow else None,
    }


def build_bisque_event_row(record: dict) -> dict:
    """
    Build a dict for INSERT INTO bisque_events from a raw JSON record.

    Args:
        record: Raw JSON dict from a processed bisque message file.

    Returns:
        A dict whose keys match the bisque_events table columns.
    """
    return {
        "id": _str(record.get("id")),
        "chat_id": _str(record.get("chat_id")),
        "type": _str(record.get("type")),
        "text": _str(record.get("text")),
        "reply_to_id": _str(record.get("reply_to_id")),
        "reply_to": _str(record.get("reply_to")),
        "audio_file": _str(record.get("audio_file")),
        "transcription": _str(record.get("transcription")),
        "transcribed_at": _str(record.get("transcribed_at")),
        "transcription_model": _str(record.get("transcription_model")),
        "task_id": _str(record.get("task_id")),
        "agent_id": _str(record.get("agent_id")),
        "status": _str(record.get("status")),
        "sent_reply_to_user": _bool_int(record.get("sent_reply_to_user")),
        "attachments": _json_str(record.get("attachments")),
        "warning": _str(record.get("warning")),
        "timestamp": _str(record.get("timestamp")),
    }


def build_agent_event_row(record: dict) -> dict:
    """
    Build a dict for INSERT INTO agent_events from a raw JSON record.

    Args:
        record: Raw JSON dict from a processed agent-event message file.

    Returns:
        A dict whose keys match the agent_events table columns.
    """
    return {
        "id": _str(record.get("id")),
        "type": _str(record.get("type")),
        "source": _str(record.get("source")),
        "chat_id": _str(record.get("chat_id")),
        "task_id": _str(record.get("task_id")),
        "agent_id": _str(record.get("agent_id")),
        "status": _str(record.get("status")),
        "text": _str(record.get("text")),
        "sent_reply_to_user": _bool_int(record.get("sent_reply_to_user")),
        "warning": _str(record.get("warning")),
        "original_chat_id": _str(record.get("original_chat_id")),
        "original_prompt": _str(record.get("original_prompt")),
        "last_output": _str(record.get("last_output")),
        "artifacts": _json_str(record.get("artifacts")),
        "forward": _json_str(record.get("forward")),
        "timestamp": _str(record.get("timestamp")),
    }


# ---------------------------------------------------------------------------
# File iteration
# ---------------------------------------------------------------------------


def iter_json_files(directory: Path) -> Iterator[tuple[Path, dict]]:
    """
    Yield (path, parsed_dict) for every valid *.json file in *directory*.

    Parsing is attempted in two passes:
      1. Strict mode (json.loads default) — fastest path.
      2. Non-strict mode (json.loads strict=False) — handles embedded control
         characters (U+0000–U+001F) that some messages contain in their text
         field.  These arise when a task result included raw terminal output.

    Files that cannot be parsed in either pass are skipped with a warning.

    Args:
        directory: Directory to scan (non-recursive).

    Yields:
        Tuples of (Path, dict).
    """
    for path in sorted(directory.glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                yield path, json.loads(text)
            except json.JSONDecodeError:
                # Retry with strict=False to accept embedded control chars
                yield path, json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            print(f"WARN  skip {path.name}: JSON error — {exc}", file=sys.stderr)
        except OSError as exc:
            print(f"WARN  skip {path.name}: I/O error — {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# INSERT helpers
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

_INSERT_AGENT = """
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
# Core migration logic
# ---------------------------------------------------------------------------


def migrate_directory(
    conn: sqlite3.Connection,
    directory: Path,
    direction: str,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Migrate all JSON files in *directory* into the appropriate tables.

    Args:
        conn:       Open sqlite3 connection with schema already applied.
        directory:  Folder containing *.json message files.
        direction:  'in' for processed/, 'out' for sent/.
        batch_size: Commit every N rows to keep memory usage bounded.
        dry_run:    If True, parse and classify but do not INSERT.

    Returns:
        Dict with counts: {'messages': N, 'bisque_events': N, 'agent_events': N,
                           'skipped': N, 'errors': N}
    """
    counts: dict[str, int] = {
        "messages": 0,
        "bisque_events": 0,
        "agent_events": 0,
        "skipped": 0,
        "errors": 0,
    }

    pending = 0

    for path, record in iter_json_files(directory):
        table = classify(record, direction)

        try:
            if not dry_run:
                if table == "agent_events":
                    row = build_agent_event_row(record)
                    conn.execute(_INSERT_AGENT, row)
                elif table == "bisque_events":
                    row = build_bisque_event_row(record)
                    conn.execute(_INSERT_BISQUE, row)
                else:
                    row = build_message_row(record, direction)
                    conn.execute(_INSERT_MESSAGE, row)

            counts[table] += 1
            pending += 1

            if pending >= batch_size:
                if not dry_run:
                    conn.commit()
                pending = 0

        except sqlite3.Error as exc:
            counts["errors"] += 1
            print(
                f"ERROR {path.name}: DB error — {exc}",
                file=sys.stderr,
            )

    if not dry_run and pending > 0:
        conn.commit()

    return counts


def migrate_all(
    db_path: Path,
    processed_dir: Path,
    sent_dir: Path,
    batch_size: int = 500,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """
    Full migration: open the DB, apply schema, then migrate processed/ and sent/.

    Args:
        db_path:       Destination SQLite database file path.
        processed_dir: ~/messages/processed/ directory.
        sent_dir:      ~/messages/sent/ directory.
        batch_size:    Rows per commit.
        dry_run:       Parse + classify but do not write to DB.
        verbose:       Print per-directory progress.
    """
    # Import here to avoid circular import if running standalone
    import importlib.util
    import os

    # Locate src/db/connection.py relative to this script
    connection_path = Path(__file__).parent.parent / "src" / "db" / "connection.py"
    schema_path = Path(__file__).parent.parent / "src" / "db" / "schema.sql"

    if connection_path.exists():
        spec = importlib.util.spec_from_file_location("connection", connection_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        open_db = mod.open_messages_db
    else:
        # Fallback: inline open without the module
        def open_db(path: Path) -> sqlite3.Connection:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            if schema_path.exists():
                conn.executescript(schema_path.read_text(encoding="utf-8"))
            return conn

    if dry_run:
        print("DRY RUN — no data will be written to the database.\n")
        # Use in-memory DB for validation
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        if schema_path.exists():
            conn.executescript(schema_path.read_text(encoding="utf-8"))
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(db_path)
        print(f"Database: {db_path}\n")

    total: dict[str, int] = {
        "messages": 0,
        "bisque_events": 0,
        "agent_events": 0,
        "skipped": 0,
        "errors": 0,
    }

    for directory, direction, label in [
        (processed_dir, "in", "processed/"),
        (sent_dir, "out", "sent/"),
    ]:
        if not directory.exists():
            print(f"WARN  {label} directory not found: {directory}", file=sys.stderr)
            continue

        file_count = sum(1 for _ in directory.glob("*.json"))
        if verbose:
            print(f"Migrating {label} ({file_count} files, direction='{direction}') ...")

        counts = migrate_directory(
            conn,
            directory,
            direction,
            batch_size=batch_size,
            dry_run=dry_run,
        )

        for k, v in counts.items():
            total[k] = total.get(k, 0) + v

        if verbose:
            print(
                f"  -> messages={counts['messages']}  "
                f"bisque_events={counts['bisque_events']}  "
                f"agent_events={counts['agent_events']}  "
                f"errors={counts['errors']}"
            )

    conn.close()

    # Summary
    grand = total["messages"] + total["bisque_events"] + total["agent_events"]
    print(
        f"\nMigration complete.\n"
        f"  messages:      {total['messages']:>7,}\n"
        f"  bisque_events: {total['bisque_events']:>7,}\n"
        f"  agent_events:  {total['agent_events']:>7,}\n"
        f"  errors:        {total['errors']:>7,}\n"
        f"  total rows:    {grand:>7,}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Lobster JSON message files into messages.db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=str(_DEFAULT_DB),
        help=f"Path to the SQLite database file (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--processed",
        metavar="DIR",
        default=str(_DEFAULT_PROCESSED),
        help=f"Directory containing processed inbound JSON files (default: {_DEFAULT_PROCESSED})",
    )
    parser.add_argument(
        "--sent",
        metavar="DIR",
        default=str(_DEFAULT_SENT),
        help=f"Directory containing sent outbound JSON files (default: {_DEFAULT_SENT})",
    )
    parser.add_argument(
        "--batch-size",
        metavar="N",
        type=int,
        default=500,
        help="Number of rows per transaction commit (default: 500)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and classify files without writing to the database",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-directory progress",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    migrate_all(
        db_path=Path(args.db),
        processed_dir=Path(args.processed),
        sent_dir=Path(args.sent),
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
