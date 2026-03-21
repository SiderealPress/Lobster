"""
src/db/connection.py — SQLite connection factory for messages.db

Provides a pure-functional approach to database connections:
  - get_connection(path) returns a configured sqlite3.Connection
  - apply_schema(conn, schema_sql) applies DDL idempotently
  - Callers are responsible for committing and closing

WAL mode and foreign_keys are applied at connection time (not stored in schema)
because they are connection-level PRAGMAs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Reconnection config: row_factory + PRAGMA setup
_PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 134217728",  # 128 MB memory-mapped I/O
]


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """
    Open (or create) a messages.db SQLite database and return a configured
    connection.

    The connection uses sqlite3.Row as its row factory so callers can access
    columns by name.  WAL mode and foreign keys are enabled for every
    connection.

    Args:
        db_path: Filesystem path to the .db file.  The parent directory must
                 already exist.

    Returns:
        An open sqlite3.Connection.  Callers are responsible for closing it.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def apply_schema(conn: sqlite3.Connection, schema_sql: str | None = None) -> None:
    """
    Execute the schema DDL against *conn*.

    If *schema_sql* is None, the bundled schema.sql is read from disk.
    All statements are executed inside a single implicit transaction; if any
    statement fails the caller should handle the exception.

    Args:
        conn:       An open sqlite3.Connection.
        schema_sql: SQL text to execute.  Defaults to the bundled schema.sql.
    """
    if schema_sql is None:
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)


def open_messages_db(db_path: str | Path) -> sqlite3.Connection:
    """
    Convenience function: open the database *and* apply the schema.

    Equivalent to:
        conn = get_connection(db_path)
        apply_schema(conn)
        return conn

    Args:
        db_path: Path to the messages.db file.

    Returns:
        A fully-initialised, open sqlite3.Connection.
    """
    conn = get_connection(db_path)
    apply_schema(conn)
    return conn
