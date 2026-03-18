"""Smoke test: sqlite-vec extension loads and basic vector ops work."""
import sqlite3
import pytest


def test_sqlite_vec_loads():
    """sqlite-vec extension loads without error."""
    import sqlite_vec

    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)  # raises if extension fails to load


def test_sqlite_vec_basic_operation():
    """Basic vec0 virtual table creation and query works."""
    import sqlite_vec

    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.execute("CREATE VIRTUAL TABLE v USING vec0(a FLOAT[3])")
    db.execute("INSERT INTO v(rowid, a) VALUES (1, '[1.0, 2.0, 3.0]')")
    rows = db.execute(
        "SELECT rowid FROM v WHERE a MATCH '[1.0, 2.0, 3.0]' ORDER BY distance LIMIT 1"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
