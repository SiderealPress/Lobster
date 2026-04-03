"""Tests for SlackLogStore and its pure helper functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.log_store import SlackLogStore, _date_range, _parse_jsonl_lines


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_log_root(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def store(tmp_log_root: Path) -> SlackLogStore:
    return SlackLogStore(log_root=tmp_log_root)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Helper to write JSONL records to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


@pytest.fixture
def populated_store(tmp_log_root: Path) -> SlackLogStore:
    """Store with pre-populated test data."""
    records_day1 = [
        {"schema": 1, "ts": "100.001", "channel_id": "C01", "text": "hello"},
        {"schema": 1, "ts": "100.002", "channel_id": "C01", "text": "world"},
    ]
    records_day2 = [
        {"schema": 1, "ts": "200.001", "channel_id": "C01", "text": "day two"},
    ]
    dm_records = [
        {"schema": 1, "ts": "300.001", "channel_id": "D01", "text": "dm msg"},
    ]

    _write_jsonl(tmp_log_root / "channels" / "C01" / "2026-04-01.jsonl", records_day1)
    _write_jsonl(tmp_log_root / "channels" / "C01" / "2026-04-02.jsonl", records_day2)
    _write_jsonl(tmp_log_root / "dms" / "D01" / "2026-04-01.jsonl", dm_records)

    return SlackLogStore(log_root=tmp_log_root)


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestDateRange:
    def test_single_day(self) -> None:
        assert _date_range("2026-04-01", "2026-04-01") == ["2026-04-01"]

    def test_multi_day(self) -> None:
        result = _date_range("2026-04-01", "2026-04-03")
        assert result == ["2026-04-01", "2026-04-02", "2026-04-03"]

    def test_reversed_range_returns_empty(self) -> None:
        assert _date_range("2026-04-03", "2026-04-01") == []


class TestParseJsonlLines:
    def test_parses_valid_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        path.write_text('{"a": 1}\n{"b": 2}\n')
        records = list(_parse_jsonl_lines(path))
        assert len(records) == 2
        assert records[0]["a"] == 1

    def test_skips_empty_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        path.write_text('{"a": 1}\n\n{"b": 2}\n')
        records = list(_parse_jsonl_lines(path))
        assert len(records) == 2

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        path.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
        records = list(_parse_jsonl_lines(path))
        assert len(records) == 2

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        records = list(_parse_jsonl_lines(path))
        assert records == []


# ---------------------------------------------------------------------------
# SlackLogStore tests
# ---------------------------------------------------------------------------


class TestSlackLogStoreQuery:
    def test_query_returns_records(self, populated_store: SlackLogStore) -> None:
        records = populated_store.query("C01", "2026-04-01")
        assert len(records) == 2
        assert records[0]["text"] == "hello"
        assert records[1]["text"] == "world"

    def test_query_nonexistent_date_returns_empty(self, populated_store: SlackLogStore) -> None:
        records = populated_store.query("C01", "2099-01-01")
        assert records == []

    def test_query_nonexistent_channel_returns_empty(self, populated_store: SlackLogStore) -> None:
        records = populated_store.query("C_NONEXISTENT", "2026-04-01")
        assert records == []


class TestSlackLogStoreQueryRange:
    def test_query_range_spans_days(self, populated_store: SlackLogStore) -> None:
        records = populated_store.query_range("C01", "2026-04-01", "2026-04-02")
        assert len(records) == 3  # 2 from day 1, 1 from day 2

    def test_query_range_single_day(self, populated_store: SlackLogStore) -> None:
        records = populated_store.query_range("C01", "2026-04-01", "2026-04-01")
        assert len(records) == 2


class TestSlackLogStoreListChannels:
    def test_lists_all_channels(self, populated_store: SlackLogStore) -> None:
        channels = populated_store.list_channels()
        assert "C01" in channels
        assert "D01" in channels

    def test_empty_store(self, store: SlackLogStore) -> None:
        assert store.list_channels() == []


class TestSlackLogStoreListDates:
    def test_lists_dates(self, populated_store: SlackLogStore) -> None:
        dates = populated_store.list_dates("C01")
        assert dates == ["2026-04-01", "2026-04-02"]

    def test_nonexistent_channel_returns_empty(self, populated_store: SlackLogStore) -> None:
        assert populated_store.list_dates("C_MISSING") == []


class TestSlackLogStoreDmRouting:
    def test_dm_records_accessible(self, populated_store: SlackLogStore) -> None:
        records = populated_store.query("D01", "2026-04-01")
        assert len(records) == 1
        assert records[0]["text"] == "dm msg"


class TestSlackLogStoreQueryIter:
    def test_lazy_iteration(self, populated_store: SlackLogStore) -> None:
        records = list(populated_store.query_iter("C01", "2026-04-01"))
        assert len(records) == 2
        assert records[0]["text"] == "hello"
