"""Tests for SlackIngressLogger and its pure helper functions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingress_logger import (
    DedupStore,
    SlackIngressLogger,
    build_record,
    log_path_for_event,
    _extract_files,
    _extract_reactions,
    _extract_subtypes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_log_root(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def tmp_dedup_db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "dedup.db"


@pytest.fixture
def logger(tmp_log_root: Path, tmp_dedup_db: Path) -> SlackIngressLogger:
    lgr = SlackIngressLogger(log_root=tmp_log_root, dedup_db_path=tmp_dedup_db)
    yield lgr
    lgr.close()


@pytest.fixture
def sample_event() -> dict:
    return {
        "type": "message",
        "ts": "1743724800.123456",
        "user": "U01DEF456",
        "channel": "C01ABC123",
        "text": "Has anyone looked at the deploy issue?",
        "event_id": "Ev01ABC123",
        "thread_ts": None,
        "files": [],
        "reactions": [],
    }


@pytest.fixture
def sample_dm_event() -> dict:
    return {
        "type": "message",
        "ts": "1743724900.654321",
        "user": "U01DEF456",
        "channel": "D01XYZ789",
        "text": "Hey Lobster, can you help?",
        "event_id": "Ev01XYZ789",
    }


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestBuildRecord:
    def test_contains_all_required_fields(self, sample_event: dict) -> None:
        record = build_record(
            event=sample_event,
            channel_id="C01ABC123",
            channel_name="engineering",
            user_id="U01DEF456",
            username="alice",
            display_name="Alice Smith",
        )

        required_fields = {
            "schema", "event_id", "ts", "channel_id", "channel_name",
            "user_id", "username", "display_name", "text", "thread_ts",
            "parent_ts", "files", "reactions", "subtypes", "logged_at", "raw",
        }
        assert set(record.keys()) == required_fields

    def test_schema_version(self, sample_event: dict) -> None:
        record = build_record(event=sample_event, channel_id="C01ABC123")
        assert record["schema"] == 1

    def test_preserves_event_data(self, sample_event: dict) -> None:
        record = build_record(
            event=sample_event,
            channel_id="C01ABC123",
            channel_name="engineering",
            username="alice",
            display_name="Alice Smith",
        )
        assert record["ts"] == "1743724800.123456"
        assert record["text"] == "Has anyone looked at the deploy issue?"
        assert record["channel_name"] == "engineering"
        assert record["username"] == "alice"
        assert record["display_name"] == "Alice Smith"

    def test_raw_contains_original_event(self, sample_event: dict) -> None:
        record = build_record(event=sample_event, channel_id="C01ABC123")
        assert record["raw"] == sample_event

    def test_logged_at_is_utc_iso(self, sample_event: dict) -> None:
        record = build_record(event=sample_event, channel_id="C01ABC123")
        # Should parse without error
        parsed = datetime.fromisoformat(record["logged_at"])
        assert parsed.tzinfo is not None

    def test_user_id_falls_back_to_event(self, sample_event: dict) -> None:
        record = build_record(event=sample_event, channel_id="C01ABC123")
        assert record["user_id"] == "U01DEF456"


class TestExtractFiles:
    def test_empty_files(self) -> None:
        assert _extract_files({}) == []

    def test_extracts_file_metadata(self) -> None:
        event = {
            "files": [
                {
                    "id": "F01",
                    "name": "report.pdf",
                    "mimetype": "application/pdf",
                    "size": 1024,
                    "url_private": "https://files.slack.com/report.pdf",
                }
            ]
        }
        result = _extract_files(event)
        assert len(result) == 1
        assert result[0]["id"] == "F01"
        assert result[0]["name"] == "report.pdf"
        assert result[0]["url"] == "https://files.slack.com/report.pdf"


class TestExtractReactions:
    def test_empty_reactions(self) -> None:
        assert _extract_reactions({}) == []

    def test_extracts_reaction_data(self) -> None:
        event = {
            "reactions": [
                {"name": "thumbsup", "users": ["U01", "U02"], "count": 2}
            ]
        }
        result = _extract_reactions(event)
        assert len(result) == 1
        assert result[0]["name"] == "thumbsup"
        assert result[0]["count"] == 2


class TestExtractSubtypes:
    def test_no_subtype(self) -> None:
        assert _extract_subtypes({}) == []

    def test_with_subtype(self) -> None:
        assert _extract_subtypes({"subtype": "file_share"}) == ["file_share"]


class TestLogPathForEvent:
    def test_channel_path(self, tmp_log_root: Path) -> None:
        path = log_path_for_event(
            channel_id="C01ABC123",
            is_dm=False,
            log_root=tmp_log_root,
            date="2026-04-03",
        )
        assert path == tmp_log_root / "channels" / "C01ABC123" / "2026-04-03.jsonl"

    def test_dm_path(self, tmp_log_root: Path) -> None:
        path = log_path_for_event(
            channel_id="D01XYZ789",
            is_dm=True,
            log_root=tmp_log_root,
            date="2026-04-03",
        )
        assert path == tmp_log_root / "dms" / "D01XYZ789" / "2026-04-03.jsonl"


# ---------------------------------------------------------------------------
# DedupStore tests
# ---------------------------------------------------------------------------


class TestDedupStore:
    def test_new_event_is_not_duplicate(self, tmp_dedup_db: Path) -> None:
        store = DedupStore(tmp_dedup_db)
        assert not store.is_duplicate("123.456", "C01")
        store.close()

    def test_marked_event_is_duplicate(self, tmp_dedup_db: Path) -> None:
        store = DedupStore(tmp_dedup_db)
        store.mark_seen("123.456", "C01")
        assert store.is_duplicate("123.456", "C01")
        store.close()

    def test_check_and_mark_returns_true_for_new(self, tmp_dedup_db: Path) -> None:
        store = DedupStore(tmp_dedup_db)
        assert store.check_and_mark("123.456", "C01") is True
        store.close()

    def test_check_and_mark_returns_false_for_dupe(self, tmp_dedup_db: Path) -> None:
        store = DedupStore(tmp_dedup_db)
        store.check_and_mark("123.456", "C01")
        assert store.check_and_mark("123.456", "C01") is False
        store.close()

    def test_different_channels_not_duplicates(self, tmp_dedup_db: Path) -> None:
        store = DedupStore(tmp_dedup_db)
        store.mark_seen("123.456", "C01")
        assert not store.is_duplicate("123.456", "C02")
        store.close()

    def test_prune_removes_old_entries(self, tmp_dedup_db: Path) -> None:
        store = DedupStore(tmp_dedup_db)
        # Insert with an old logged_at
        store._conn.execute(
            "INSERT INTO seen_events (ts, channel_id, logged_at) VALUES (?, ?, ?)",
            ("old.001", "C01", "2020-01-01T00:00:00+00:00"),
        )
        store._conn.commit()
        store.mark_seen("new.001", "C01")

        pruned = store.prune(retention_days=7)
        assert pruned == 1
        assert not store.is_duplicate("old.001", "C01")
        assert store.is_duplicate("new.001", "C01")
        store.close()


# ---------------------------------------------------------------------------
# SlackIngressLogger integration tests
# ---------------------------------------------------------------------------


class TestSlackIngressLogger:
    def test_logs_message_to_jsonl(
        self, logger: SlackIngressLogger, tmp_log_root: Path, sample_event: dict
    ) -> None:
        logger.log_message(
            event=sample_event,
            channel_id="C01ABC123",
            channel_name="engineering",
            user_id="U01DEF456",
            username="alice",
            display_name="Alice Smith",
        )

        # Find the log file
        log_files = list(tmp_log_root.rglob("*.jsonl"))
        assert len(log_files) == 1

        records = [json.loads(line) for line in log_files[0].read_text().strip().split("\n")]
        assert len(records) == 1
        assert records[0]["channel_id"] == "C01ABC123"
        assert records[0]["text"] == "Has anyone looked at the deploy issue?"
        assert records[0]["schema"] == 1

    def test_deduplicates_same_event(
        self, logger: SlackIngressLogger, tmp_log_root: Path, sample_event: dict
    ) -> None:
        logger.log_message(event=sample_event, channel_id="C01ABC123")
        logger.log_message(event=sample_event, channel_id="C01ABC123")

        log_files = list(tmp_log_root.rglob("*.jsonl"))
        assert len(log_files) == 1

        lines = log_files[0].read_text().strip().split("\n")
        assert len(lines) == 1  # Only one record, not two

    def test_dm_routes_to_dms_directory(
        self, logger: SlackIngressLogger, tmp_log_root: Path, sample_dm_event: dict
    ) -> None:
        logger.log_message(
            event=sample_dm_event,
            channel_id="D01XYZ789",
            is_dm=True,
        )

        dm_dir = tmp_log_root / "dms" / "D01XYZ789"
        assert dm_dir.exists()
        log_files = list(dm_dir.glob("*.jsonl"))
        assert len(log_files) == 1

    def test_channel_routes_to_channels_directory(
        self, logger: SlackIngressLogger, tmp_log_root: Path, sample_event: dict
    ) -> None:
        logger.log_message(
            event=sample_event,
            channel_id="C01ABC123",
            is_dm=False,
        )

        ch_dir = tmp_log_root / "channels" / "C01ABC123"
        assert ch_dir.exists()

    def test_skips_event_without_ts(
        self, logger: SlackIngressLogger, tmp_log_root: Path
    ) -> None:
        event = {"text": "no timestamp", "user": "U01"}
        logger.log_message(event=event, channel_id="C01ABC123")

        log_files = list(tmp_log_root.rglob("*.jsonl"))
        assert len(log_files) == 0

    def test_skips_event_without_channel(
        self, logger: SlackIngressLogger, tmp_log_root: Path
    ) -> None:
        event = {"ts": "123.456", "text": "no channel"}
        logger.log_message(event=event, channel_id="")

        log_files = list(tmp_log_root.rglob("*.jsonl"))
        assert len(log_files) == 0

    def test_log_reaction(
        self, logger: SlackIngressLogger, tmp_log_root: Path
    ) -> None:
        event = {
            "type": "reaction_added",
            "ts": "1743724800.999999",
            "user": "U01DEF456",
            "reaction": "thumbsup",
            "item": {"channel": "C01ABC123", "ts": "1743724800.123456"},
        }
        logger.log_reaction(event=event, channel_id="C01ABC123")

        log_files = list(tmp_log_root.rglob("*.jsonl"))
        assert len(log_files) == 1

    def test_log_file_event(
        self, logger: SlackIngressLogger, tmp_log_root: Path
    ) -> None:
        event = {
            "type": "file_shared",
            "ts": "1743724801.000001",
            "user": "U01DEF456",
            "file_id": "F01ABC",
            "files": [
                {"id": "F01ABC", "name": "doc.pdf", "mimetype": "application/pdf", "size": 2048}
            ],
        }
        logger.log_file(event=event, channel_id="C01ABC123")

        log_files = list(tmp_log_root.rglob("*.jsonl"))
        assert len(log_files) == 1
        record = json.loads(log_files[0].read_text().strip())
        assert len(record["files"]) == 1
        assert record["files"][0]["name"] == "doc.pdf"

    def test_multiple_channels_separate_files(
        self, logger: SlackIngressLogger, tmp_log_root: Path
    ) -> None:
        event_a = {"ts": "100.001", "user": "U01", "text": "in channel A"}
        event_b = {"ts": "100.002", "user": "U01", "text": "in channel B"}

        logger.log_message(event=event_a, channel_id="C_A")
        logger.log_message(event=event_b, channel_id="C_B")

        log_files = list(tmp_log_root.rglob("*.jsonl"))
        assert len(log_files) == 2

    def test_record_is_valid_json(
        self, logger: SlackIngressLogger, tmp_log_root: Path, sample_event: dict
    ) -> None:
        logger.log_message(event=sample_event, channel_id="C01ABC123")

        log_files = list(tmp_log_root.rglob("*.jsonl"))
        content = log_files[0].read_text().strip()
        record = json.loads(content)  # Should not raise
        assert isinstance(record, dict)
