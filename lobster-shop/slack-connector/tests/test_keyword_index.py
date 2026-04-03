"""Tests for keyword_index.py — SQLite FTS5 keyword search."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.keyword_index import (
    KeywordIndex,
    _build_fts_row,
    _filter_after_cursor,
    _max_ts,
)


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestBuildFtsRow:
    """Tests for _build_fts_row — pure extraction of FTS-indexable fields."""

    def test_valid_message(self) -> None:
        msg = {
            "ts": "1743724800.123456",
            "channel_id": "C01ABC123",
            "user_id": "U01DEF456",
            "text": "deploy outage in production",
        }
        result = _build_fts_row(msg)
        assert result == ("1743724800.123456", "C01ABC123", "U01DEF456", "deploy outage in production")

    def test_missing_ts_returns_none(self) -> None:
        msg = {"channel_id": "C01", "text": "hello", "user_id": "U01"}
        assert _build_fts_row(msg) is None

    def test_missing_channel_id_returns_none(self) -> None:
        msg = {"ts": "123", "text": "hello", "user_id": "U01"}
        assert _build_fts_row(msg) is None

    def test_missing_text_returns_none(self) -> None:
        msg = {"ts": "123", "channel_id": "C01", "user_id": "U01"}
        assert _build_fts_row(msg) is None

    def test_empty_text_returns_none(self) -> None:
        msg = {"ts": "123", "channel_id": "C01", "user_id": "U01", "text": ""}
        assert _build_fts_row(msg) is None

    def test_missing_user_id_defaults_empty(self) -> None:
        msg = {"ts": "123", "channel_id": "C01", "text": "hello"}
        result = _build_fts_row(msg)
        assert result is not None
        assert result[2] == ""  # user_id defaults to ""


class TestFilterAfterCursor:
    """Tests for _filter_after_cursor — pure cursor-based filtering."""

    def test_none_cursor_returns_all(self) -> None:
        msgs = [{"ts": "1"}, {"ts": "2"}, {"ts": "3"}]
        assert _filter_after_cursor(msgs, None) == msgs

    def test_filters_messages_before_cursor(self) -> None:
        msgs = [{"ts": "1"}, {"ts": "2"}, {"ts": "3"}]
        result = _filter_after_cursor(msgs, "2")
        assert len(result) == 1
        assert result[0]["ts"] == "3"

    def test_cursor_at_max_returns_empty(self) -> None:
        msgs = [{"ts": "1"}, {"ts": "2"}]
        assert _filter_after_cursor(msgs, "2") == []

    def test_empty_messages_returns_empty(self) -> None:
        assert _filter_after_cursor([], "1") == []


class TestMaxTs:
    """Tests for _max_ts — pure max timestamp extraction."""

    def test_returns_max(self) -> None:
        msgs = [{"ts": "1"}, {"ts": "3"}, {"ts": "2"}]
        assert _max_ts(msgs) == "3"

    def test_empty_returns_none(self) -> None:
        assert _max_ts([]) is None

    def test_missing_ts_fields_skipped(self) -> None:
        msgs = [{"ts": "1"}, {}, {"ts": "2"}]
        assert _max_ts(msgs) == "2"


# ---------------------------------------------------------------------------
# KeywordIndex integration tests (uses temp SQLite DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def keyword_index(tmp_path: Path) -> KeywordIndex:
    """Create a KeywordIndex backed by a temp directory."""
    idx = KeywordIndex(state_dir=tmp_path)
    yield idx
    idx.close()


def _make_messages(count: int, channel_id: str = "C01") -> list[dict]:
    """Generate a batch of test messages."""
    return [
        {
            "ts": f"174372480{i}.000000",
            "channel_id": channel_id,
            "user_id": f"U0{i}",
            "text": f"test message number {i} about deploy",
        }
        for i in range(count)
    ]


class TestKeywordIndexBasic:
    """Tests for KeywordIndex core operations."""

    def test_index_and_search(self, keyword_index: KeywordIndex) -> None:
        messages = _make_messages(5)
        count = keyword_index.index_messages(messages)
        assert count == 5

        results = keyword_index.search("deploy")
        assert len(results) == 5
        assert all("deploy" in r["text"] for r in results)

    def test_search_with_channel_filter(self, keyword_index: KeywordIndex) -> None:
        msgs_a = _make_messages(3, channel_id="CA")
        msgs_b = _make_messages(2, channel_id="CB")
        keyword_index.index_messages(msgs_a + msgs_b)

        results_a = keyword_index.search("deploy", channel_id="CA")
        results_b = keyword_index.search("deploy", channel_id="CB")
        assert len(results_a) == 3
        assert len(results_b) == 2

    def test_search_no_results(self, keyword_index: KeywordIndex) -> None:
        keyword_index.index_messages(_make_messages(3))
        results = keyword_index.search("nonexistent_xyz")
        assert results == []

    def test_search_limit(self, keyword_index: KeywordIndex) -> None:
        keyword_index.index_messages(_make_messages(10))
        results = keyword_index.search("deploy", limit=3)
        assert len(results) == 3

    def test_index_skips_invalid_messages(self, keyword_index: KeywordIndex) -> None:
        messages = [
            {"ts": "1", "channel_id": "C01", "user_id": "U01", "text": "valid"},
            {"channel_id": "C01", "text": "missing ts"},
            {"ts": "2", "text": "missing channel"},
            {},
        ]
        count = keyword_index.index_messages(messages)
        assert count == 1

    def test_index_empty_list(self, keyword_index: KeywordIndex) -> None:
        count = keyword_index.index_messages([])
        assert count == 0


class TestKeywordIndexCursors:
    """Tests for cursor tracking — incremental indexing support."""

    def test_cursor_initially_none(self, keyword_index: KeywordIndex) -> None:
        assert keyword_index.get_cursor("C01") is None

    def test_set_and_get_cursor(self, keyword_index: KeywordIndex) -> None:
        keyword_index.set_cursor("C01", "1743724800.000000")
        assert keyword_index.get_cursor("C01") == "1743724800.000000"

    def test_cursor_per_channel(self, keyword_index: KeywordIndex) -> None:
        keyword_index.set_cursor("CA", "100")
        keyword_index.set_cursor("CB", "200")
        assert keyword_index.get_cursor("CA") == "100"
        assert keyword_index.get_cursor("CB") == "200"

    def test_cursor_upsert(self, keyword_index: KeywordIndex) -> None:
        keyword_index.set_cursor("C01", "100")
        keyword_index.set_cursor("C01", "200")
        assert keyword_index.get_cursor("C01") == "200"


class TestKeywordIndexSchema:
    """Tests for schema creation — idempotent and correct."""

    def test_db_created_on_first_access(self, tmp_path: Path) -> None:
        idx = KeywordIndex(state_dir=tmp_path)
        idx.index_messages([])  # triggers lazy init
        assert (tmp_path / "keyword_index.db").exists()
        idx.close()

    def test_reopening_preserves_data(self, tmp_path: Path) -> None:
        idx1 = KeywordIndex(state_dir=tmp_path)
        idx1.index_messages(_make_messages(3))
        idx1.set_cursor("C01", "999")
        idx1.close()

        idx2 = KeywordIndex(state_dir=tmp_path)
        results = idx2.search("deploy")
        assert len(results) == 3
        assert idx2.get_cursor("C01") == "999"
        idx2.close()
