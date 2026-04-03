"""Tests for indexer.py — Slack indexing pipeline.

All Haiku calls are mocked. Tests verify:
- Pure helper functions (no mocking needed)
- Keyword indexing integration (cursor-based incremental)
- Thread summary pipeline (with mock Haiku)
- Nightly index pipeline (with mock Haiku)
- Cost guardrails (env flag, batch limits, channel filtering)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.indexer import (
    SlackIndexer,
    _build_nightly_context,
    _build_thread_context,
    _build_thread_summary_record,
    _extract_participants,
    _group_by_thread,
    _is_channel_ignored,
    _is_index_enabled,
    _is_thread_idle,
    _parse_haiku_json,
)
from src.keyword_index import KeywordIndex
from src.log_store import SlackLogStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_msg(
    ts: str = "1743724800.000000",
    channel_id: str = "C01ABC123",
    user_id: str = "U01",
    username: str = "alice",
    text: str = "test message",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Build a minimal test message record."""
    msg: dict[str, Any] = {
        "ts": ts,
        "channel_id": channel_id,
        "user_id": user_id,
        "username": username,
        "text": text,
    }
    if thread_ts:
        msg["thread_ts"] = thread_ts
    return msg


def _setup_log_store(tmp_path: Path, messages: list[dict], channel_id: str = "C01ABC123", date: str = "2026-04-03") -> SlackLogStore:
    """Create a log store with test data written to JSONL files."""
    log_dir = tmp_path / "logs" / "channels" / channel_id
    log_dir.mkdir(parents=True, exist_ok=True)

    jsonl_file = log_dir / f"{date}.jsonl"
    with open(jsonl_file, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    return SlackLogStore(log_root=tmp_path / "logs")


def _mock_haiku_thread_summary(prompt: str) -> str:
    """Mock Haiku response for thread summarization."""
    return json.dumps({
        "summary": "Team discussed the deploy outage. Root cause identified.",
        "action_items": ["alice: fix migration by EOD"],
        "sentiment": "neutral",
    })


def _mock_haiku_topics(prompt: str) -> str:
    """Mock Haiku response for topic clustering."""
    if "topic" in prompt.lower():
        return json.dumps({
            "topics": [
                {
                    "topic": "Deploy outage",
                    "message_count": 3,
                    "key_points": ["Root cause: stale migration"],
                    "participants": ["alice", "bob"],
                }
            ],
        })
    elif "action" in prompt.lower():
        return json.dumps({
            "action_items": [
                {
                    "assignee": "alice",
                    "action": "Fix stale migration",
                    "context": "deploy thread",
                    "urgency": "high",
                }
            ],
        })
    return "{}"


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestIsIndexEnabled:
    def test_default_true(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOBSTER_SLACK_INDEX_ENABLED", None)
            assert _is_index_enabled() is True

    def test_false(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_SLACK_INDEX_ENABLED": "false"}):
            assert _is_index_enabled() is False

    def test_true_explicit(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_SLACK_INDEX_ENABLED": "true"}):
            assert _is_index_enabled() is True


class TestIsChannelIgnored:
    def test_not_in_config_is_not_ignored(self) -> None:
        assert _is_channel_ignored("C01", {}) is False

    def test_mode_ignore(self) -> None:
        config = {"C01": {"id": "C01", "mode": "ignore"}}
        assert _is_channel_ignored("C01", config) is True

    def test_mode_monitor(self) -> None:
        config = {"C01": {"id": "C01", "mode": "monitor"}}
        assert _is_channel_ignored("C01", config) is False


class TestGroupByThread:
    def test_groups_by_thread_ts(self) -> None:
        msgs = [
            _make_msg(ts="1", thread_ts="T1"),
            _make_msg(ts="2", thread_ts="T1"),
            _make_msg(ts="3", thread_ts="T2"),
        ]
        groups = _group_by_thread(msgs)
        assert len(groups) == 2
        assert len(groups["T1"]) == 2
        assert len(groups["T2"]) == 1

    def test_non_threaded_messages_excluded(self) -> None:
        msgs = [_make_msg(ts="1"), _make_msg(ts="2")]
        groups = _group_by_thread(msgs)
        assert groups == {}


class TestIsThreadIdle:
    def test_idle_thread(self) -> None:
        old_ts = str(time.time() - 3 * 3600)  # 3 hours ago
        msgs = [_make_msg(ts=old_ts)]
        now = datetime.now(timezone.utc)
        assert _is_thread_idle(msgs, now) is True

    def test_active_thread(self) -> None:
        recent_ts = str(time.time() - 60)  # 1 minute ago
        msgs = [_make_msg(ts=recent_ts)]
        now = datetime.now(timezone.utc)
        assert _is_thread_idle(msgs, now) is False

    def test_empty_messages(self) -> None:
        now = datetime.now(timezone.utc)
        assert _is_thread_idle([], now) is False


class TestExtractParticipants:
    def test_unique_participants(self) -> None:
        msgs = [
            _make_msg(username="alice"),
            _make_msg(username="bob"),
            _make_msg(username="alice"),
        ]
        result = _extract_participants(msgs)
        assert result == ["alice", "bob"]

    def test_falls_back_to_user_id(self) -> None:
        msgs = [{"user_id": "U01"}, {"user_id": "U02"}]
        result = _extract_participants(msgs)
        assert result == ["U01", "U02"]


class TestBuildThreadContext:
    def test_builds_context_string(self) -> None:
        msgs = [
            _make_msg(ts="1", username="alice", text="hello"),
            _make_msg(ts="2", username="bob", text="world"),
        ]
        ctx = _build_thread_context(msgs)
        assert "alice: hello" in ctx
        assert "bob: world" in ctx

    def test_truncates_to_max(self) -> None:
        msgs = [_make_msg(ts=str(i), text=f"msg {i}") for i in range(100)]
        ctx = _build_thread_context(msgs, max_messages=5)
        lines = [l for l in ctx.strip().split("\n") if l]
        assert len(lines) == 5


class TestBuildNightlyContext:
    def test_includes_timestamps(self) -> None:
        msgs = [_make_msg(ts="12345", username="alice", text="hello")]
        ctx = _build_nightly_context(msgs)
        assert "[12345]" in ctx
        assert "alice: hello" in ctx


class TestBuildThreadSummaryRecord:
    def test_builds_record(self) -> None:
        msgs = [_make_msg(username="alice"), _make_msg(username="bob")]
        haiku_result = {
            "summary": "Team discussed X",
            "action_items": ["alice: do Y"],
            "sentiment": "neutral",
        }
        record = _build_thread_summary_record("C01", "T1", msgs, haiku_result)
        assert record["channel_id"] == "C01"
        assert record["thread_ts"] == "T1"
        assert record["message_count"] == 2
        assert record["participants"] == ["alice", "bob"]
        assert record["summary"] == "Team discussed X"
        assert "indexed_at" in record


class TestParseHaikuJson:
    def test_plain_json(self) -> None:
        raw = '{"summary": "test"}'
        assert _parse_haiku_json(raw) == {"summary": "test"}

    def test_json_in_fences(self) -> None:
        raw = '```json\n{"summary": "test"}\n```'
        assert _parse_haiku_json(raw) == {"summary": "test"}

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_haiku_json("not json at all") == {}

    def test_empty_string(self) -> None:
        assert _parse_haiku_json("") == {}


# ---------------------------------------------------------------------------
# Integration tests — keyword indexing
# ---------------------------------------------------------------------------


class TestBuildKeywordIndex:
    def test_indexes_messages_from_log_store(self, tmp_path: Path) -> None:
        messages = [
            _make_msg(ts=f"174372480{i}.000000", text=f"message about deploy {i}")
            for i in range(5)
        ]
        log_store = _setup_log_store(tmp_path, messages)
        keyword_index = KeywordIndex(state_dir=tmp_path / "state")

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            keyword_index=keyword_index,
        )
        count = indexer.build_keyword_index("C01ABC123", "2026-04-03")
        assert count == 5

        results = keyword_index.search("deploy")
        assert len(results) == 5
        keyword_index.close()

    def test_incremental_indexing_via_cursor(self, tmp_path: Path) -> None:
        """Re-running should not re-index already-processed messages."""
        messages = [
            _make_msg(ts="1743724800.000000", text="first deploy"),
            _make_msg(ts="1743724801.000000", text="second deploy"),
        ]
        log_store = _setup_log_store(tmp_path, messages)
        keyword_index = KeywordIndex(state_dir=tmp_path / "state")

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            keyword_index=keyword_index,
        )

        # First run indexes all
        count1 = indexer.build_keyword_index("C01ABC123", "2026-04-03")
        assert count1 == 2

        # Second run finds nothing new
        count2 = indexer.build_keyword_index("C01ABC123", "2026-04-03")
        assert count2 == 0

        keyword_index.close()

    def test_skips_ignored_channels(self, tmp_path: Path) -> None:
        messages = [_make_msg(text="should not index")]
        log_store = _setup_log_store(tmp_path, messages)
        keyword_index = KeywordIndex(state_dir=tmp_path / "state")

        # Write channels.yaml with mode: ignore
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "channels.yaml").write_text(
            "channels:\n  - id: C01ABC123\n    mode: ignore\n"
        )

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            keyword_index=keyword_index,
        )
        count = indexer.build_keyword_index("C01ABC123", "2026-04-03")
        assert count == 0
        keyword_index.close()


# ---------------------------------------------------------------------------
# Integration tests — thread summaries
# ---------------------------------------------------------------------------


class TestSummarizeIdleThreads:
    def test_summarizes_idle_thread(self, tmp_path: Path) -> None:
        # Thread with messages from 3 hours ago
        old_ts = str(time.time() - 3 * 3600)
        messages = [
            _make_msg(ts=old_ts, text="thread start", thread_ts=old_ts, username="alice"),
            _make_msg(ts=str(float(old_ts) + 60), text="reply", thread_ts=old_ts, username="bob"),
        ]
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_store = _setup_log_store(tmp_path, messages, date=date)

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            haiku_invoke=_mock_haiku_thread_summary,
        )
        count = indexer.summarize_idle_threads(date=date)
        assert count == 1

        # Verify output file
        summary_file = tmp_path / "index" / "thread-summaries" / "C01ABC123" / f"{date}.jsonl"
        assert summary_file.exists()
        with open(summary_file) as f:
            record = json.loads(f.readline())
        assert record["thread_ts"] == old_ts
        assert record["message_count"] == 2
        assert "deploy outage" in record["summary"]

    def test_skips_active_threads(self, tmp_path: Path) -> None:
        recent_ts = str(time.time() - 60)  # 1 minute ago
        messages = [
            _make_msg(ts=recent_ts, text="active thread", thread_ts=recent_ts),
        ]
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_store = _setup_log_store(tmp_path, messages, date=date)

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            haiku_invoke=_mock_haiku_thread_summary,
        )
        count = indexer.summarize_idle_threads(date=date)
        assert count == 0

    def test_disabled_via_env(self, tmp_path: Path) -> None:
        old_ts = str(time.time() - 3 * 3600)
        messages = [_make_msg(ts=old_ts, thread_ts=old_ts)]
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_store = _setup_log_store(tmp_path, messages, date=date)

        with patch.dict(os.environ, {"LOBSTER_SLACK_INDEX_ENABLED": "false"}):
            indexer = SlackIndexer(
                connector_root=tmp_path,
                log_store=log_store,
                haiku_invoke=_mock_haiku_thread_summary,
            )
            count = indexer.summarize_idle_threads(date=date)
            assert count == 0

    def test_deduplicates_already_summarized(self, tmp_path: Path) -> None:
        old_ts = str(time.time() - 3 * 3600)
        messages = [_make_msg(ts=old_ts, text="thread", thread_ts=old_ts)]
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_store = _setup_log_store(tmp_path, messages, date=date)

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            haiku_invoke=_mock_haiku_thread_summary,
        )

        # First run
        count1 = indexer.summarize_idle_threads(date=date)
        assert count1 == 1

        # Second run — should skip already-summarized thread
        count2 = indexer.summarize_idle_threads(date=date)
        assert count2 == 0


# ---------------------------------------------------------------------------
# Integration tests — nightly index
# ---------------------------------------------------------------------------


class TestRunNightlyIndex:
    def test_produces_topic_and_action_files(self, tmp_path: Path) -> None:
        messages = [
            _make_msg(ts=f"174372480{i}.000000", text=f"discussing deploy issue {i}")
            for i in range(5)
        ]
        log_store = _setup_log_store(tmp_path, messages)

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            haiku_invoke=_mock_haiku_topics,
        )
        indexer.run_nightly_index("C01ABC123", "2026-04-03")

        # Verify topic clusters file
        topic_file = tmp_path / "index" / "topic-clusters" / "C01ABC123" / "2026-04-03.json"
        assert topic_file.exists()
        data = json.loads(topic_file.read_text())
        assert "topics" in data
        assert data["topics"][0]["topic"] == "Deploy outage"

        # Verify action items file
        action_file = tmp_path / "index" / "action-items" / "C01ABC123" / "2026-04-03.json"
        assert action_file.exists()
        data = json.loads(action_file.read_text())
        assert "action_items" in data

    def test_disabled_via_env(self, tmp_path: Path) -> None:
        messages = [_make_msg(text="should not process")]
        log_store = _setup_log_store(tmp_path, messages)

        with patch.dict(os.environ, {"LOBSTER_SLACK_INDEX_ENABLED": "false"}):
            indexer = SlackIndexer(
                connector_root=tmp_path,
                log_store=log_store,
                haiku_invoke=_mock_haiku_topics,
            )
            indexer.run_nightly_index("C01ABC123", "2026-04-03")

        # No files should be created
        topic_file = tmp_path / "index" / "topic-clusters" / "C01ABC123" / "2026-04-03.json"
        assert not topic_file.exists()

    def test_skips_ignored_channel(self, tmp_path: Path) -> None:
        messages = [_make_msg(text="ignored channel")]
        log_store = _setup_log_store(tmp_path, messages)

        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "channels.yaml").write_text(
            "channels:\n  - id: C01ABC123\n    mode: ignore\n"
        )

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            haiku_invoke=_mock_haiku_topics,
        )
        indexer.run_nightly_index("C01ABC123", "2026-04-03")

        topic_file = tmp_path / "index" / "topic-clusters" / "C01ABC123" / "2026-04-03.json"
        assert not topic_file.exists()

    def test_logs_continuation_for_large_batches(self, tmp_path: Path, caplog) -> None:
        """More than 50 messages should log a continuation notice."""
        messages = [
            _make_msg(ts=f"17437248{i:04d}.000000", text=f"msg {i}")
            for i in range(60)
        ]
        log_store = _setup_log_store(tmp_path, messages)

        indexer = SlackIndexer(
            connector_root=tmp_path,
            log_store=log_store,
            haiku_invoke=_mock_haiku_topics,
        )

        import logging
        with caplog.at_level(logging.INFO, logger="slack-indexer"):
            indexer.run_nightly_index("C01ABC123", "2026-04-03")

        assert any("continuation" in r.message for r in caplog.records)
