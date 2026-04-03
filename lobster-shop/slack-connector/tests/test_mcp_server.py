"""
Tests for Slack Connector MCP Server.

Tests the pure helper functions and tool handlers independently.
Uses temp directories to avoid touching real log files.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Add the skill src/ directory to path
_SKILL_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SKILL_SRC))

from mcp_server import (
    _build_channel_summary,
    _build_status,
    _build_thread_summary,
    _count_events_today,
    _count_trigger_rules,
    _date_n_days_ago,
    _filter_messages_by_query,
    _format_message_for_display,
    _group_by_thread,
    _last_event_timestamp,
    _log_size_mb,
    _parse_date_or_default,
    _handle_slack_log_search,
    _handle_slack_channel_summary,
    _handle_slack_thread_summary,
    _handle_slack_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_root(tmp_path):
    """Create a temporary log root with sample data."""
    channels_dir = tmp_path / "channels" / "C001"
    channels_dir.mkdir(parents=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = channels_dir / f"{today}.jsonl"

    messages = [
        {
            "schema": 1,
            "ts": "1743724800.001",
            "channel_id": "C001",
            "channel_name": "engineering",
            "user_id": "U001",
            "username": "alice",
            "display_name": "Alice Smith",
            "text": "Has anyone looked at the deploy issue?",
            "thread_ts": None,
            "logged_at": "2026-04-03T14:00:00Z",
        },
        {
            "schema": 1,
            "ts": "1743724801.002",
            "channel_id": "C001",
            "channel_name": "engineering",
            "user_id": "U002",
            "username": "bob",
            "display_name": "Bob Jones",
            "text": "Yes, the deploy pipeline is failing on staging",
            "thread_ts": "1743724800.001",
            "logged_at": "2026-04-03T14:01:00Z",
        },
        {
            "schema": 1,
            "ts": "1743724802.003",
            "channel_id": "C001",
            "channel_name": "engineering",
            "user_id": "U001",
            "username": "alice",
            "display_name": "Alice Smith",
            "text": "I'll fix the staging config by EOD",
            "thread_ts": "1743724800.001",
            "logged_at": "2026-04-03T14:02:00Z",
        },
        {
            "schema": 1,
            "ts": "1743724810.004",
            "channel_id": "C001",
            "channel_name": "engineering",
            "user_id": "U003",
            "username": "charlie",
            "display_name": "Charlie Davis",
            "text": "Anyone up for lunch?",
            "thread_ts": None,
            "logged_at": "2026-04-03T14:05:00Z",
        },
    ]

    with open(log_file, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    return tmp_path


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary config directory with sample rule files."""
    config = tmp_path / "config"
    rules_dir = config / "rules"
    rules_dir.mkdir(parents=True)

    # Create sample rule files
    (rules_dir / "keyword-alert.toml").write_text("[rule]\nname = 'test-rule'\n")
    (rules_dir / "mention-handler.toml").write_text("[rule]\nname = 'mention'\n")

    return config


@pytest.fixture
def state_dir(tmp_path):
    """Create a temporary state directory."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    return state


@pytest.fixture
def fts_index(state_dir):
    """Create a FTS5 keyword index with sample data."""
    db_path = state_dir / "keyword_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            ts, channel_id, user_id, text,
            tokenize='porter ascii'
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_cursors (
            channel_id TEXT PRIMARY KEY,
            last_ts TEXT NOT NULL
        );
    """)
    conn.executemany(
        "INSERT INTO messages_fts(ts, channel_id, user_id, text) VALUES (?, ?, ?, ?);",
        [
            ("1743724800.001", "C001", "U001", "Has anyone looked at the deploy issue?"),
            ("1743724801.002", "C001", "U002", "Yes, the deploy pipeline is failing on staging"),
            ("1743724802.003", "C001", "U001", "I'll fix the staging config by EOD"),
            ("1743724810.004", "C001", "U003", "Anyone up for lunch?"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestPureHelpers:
    """Tests for pure functions with no I/O."""

    def test_parse_date_or_default_valid(self):
        assert _parse_date_or_default("2026-04-03", "2026-01-01") == "2026-04-03"

    def test_parse_date_or_default_invalid(self):
        assert _parse_date_or_default("not-a-date", "2026-01-01") == "2026-01-01"

    def test_parse_date_or_default_none(self):
        assert _parse_date_or_default(None, "2026-01-01") == "2026-01-01"

    def test_parse_date_or_default_empty(self):
        assert _parse_date_or_default("", "2026-01-01") == "2026-01-01"

    def test_date_n_days_ago(self):
        result = _date_n_days_ago(0)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert result == today

    def test_filter_messages_single_term(self):
        messages = [
            {"text": "deploy issue found"},
            {"text": "lunch time!"},
            {"text": "deploy pipeline failing"},
        ]
        result = _filter_messages_by_query(messages, "deploy")
        assert len(result) == 2

    def test_filter_messages_multiple_terms(self):
        messages = [
            {"text": "deploy issue found"},
            {"text": "deploy pipeline failing"},
        ]
        result = _filter_messages_by_query(messages, "deploy pipeline")
        assert len(result) == 1
        assert "pipeline" in result[0]["text"]

    def test_filter_messages_case_insensitive(self):
        messages = [{"text": "DEPLOY ISSUE"}]
        result = _filter_messages_by_query(messages, "deploy")
        assert len(result) == 1

    def test_filter_messages_empty_text(self):
        messages = [{"text": ""}, {}]
        result = _filter_messages_by_query(messages, "deploy")
        assert len(result) == 0

    def test_format_message_for_display(self):
        msg = {
            "ts": "123.456",
            "channel_id": "C001",
            "channel_name": "eng",
            "user_id": "U001",
            "username": "alice",
            "display_name": "Alice",
            "text": "hello",
            "thread_ts": "100.001",
            "raw": {"some": "data"},
            "files": [],
        }
        result = _format_message_for_display(msg)
        assert "raw" not in result
        assert "files" not in result
        assert result["username"] == "alice"
        assert result["thread_ts"] == "100.001"

    def test_group_by_thread(self):
        messages = [
            {"text": "root", "thread_ts": None},
            {"text": "reply1", "thread_ts": "100.001"},
            {"text": "reply2", "thread_ts": "100.001"},
            {"text": "standalone", "thread_ts": None},
        ]
        groups = _group_by_thread(messages)
        assert len(groups[None]) == 2
        assert len(groups["100.001"]) == 2

    def test_build_channel_summary_empty(self):
        result = _build_channel_summary([], "C001", "2026-04-03")
        assert result["message_count"] == 0
        assert result["participants"] == []
        assert "No messages" in result["summary"]

    def test_build_channel_summary_with_messages(self):
        messages = [
            {"username": "alice", "text": "hello", "thread_ts": "100.001", "ts": "100.001"},
            {"username": "bob", "text": "hi", "thread_ts": "100.001", "ts": "100.002"},
            {"username": "alice", "text": "standalone", "thread_ts": None, "ts": "100.003"},
        ]
        result = _build_channel_summary(messages, "C001", "2026-04-03")
        assert result["message_count"] == 3
        assert "alice" in result["participants"]
        assert "bob" in result["participants"]
        assert result["threads"] == 1  # one thread with 2+ messages

    def test_build_thread_summary(self):
        messages = [
            {"ts": "100.001", "thread_ts": None, "username": "alice", "text": "root"},
            {"ts": "100.002", "thread_ts": "100.001", "username": "bob", "text": "reply"},
            {"ts": "200.001", "thread_ts": None, "username": "charlie", "text": "other"},
        ]
        result = _build_thread_summary(messages, "C001", "100.001")
        assert result["message_count"] == 2  # root + reply
        assert "alice" in result["participants"]
        assert "bob" in result["participants"]
        assert "charlie" not in result["participants"]

    def test_build_thread_summary_empty(self):
        result = _build_thread_summary([], "C001", "999.999")
        assert result["message_count"] == 0
        assert result["participants"] == []


# ---------------------------------------------------------------------------
# I/O helper tests (read-only, use temp dirs)
# ---------------------------------------------------------------------------


class TestIOHelpers:
    """Tests for read-only I/O helpers."""

    def test_count_events_today(self, log_root):
        count = _count_events_today(log_root)
        assert count == 4

    def test_count_events_today_no_dir(self, tmp_path):
        count = _count_events_today(tmp_path / "nonexistent")
        assert count == 0

    def test_log_size_mb(self, log_root):
        size = _log_size_mb(log_root)
        # Files are small but should register as non-negative
        assert size >= 0.0
        # Verify there are actual files to measure
        jsonl_files = list(log_root.rglob("*.jsonl"))
        assert len(jsonl_files) > 0

    def test_log_size_mb_no_dir(self, tmp_path):
        size = _log_size_mb(tmp_path / "nonexistent")
        assert size == 0.0

    def test_last_event_timestamp(self, log_root):
        ts = _last_event_timestamp(log_root)
        assert ts is not None
        assert "2026-04-03" in ts

    def test_last_event_timestamp_empty(self, tmp_path):
        ts = _last_event_timestamp(tmp_path)
        assert ts is None

    def test_count_trigger_rules(self, config_dir):
        count = _count_trigger_rules(config_dir)
        assert count == 2

    def test_count_trigger_rules_no_dir(self, tmp_path):
        count = _count_trigger_rules(tmp_path / "nonexistent")
        assert count == 0


class TestBuildStatus:
    """Tests for the status builder."""

    def test_build_status(self, log_root, config_dir, state_dir):
        channels = ["C001", "C002"]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"stdout": "inactive"})()
            status = _build_status(log_root, config_dir, state_dir, channels)

        assert status["connected"] is False
        assert status["channels_monitored"] == 2
        assert status["channel_ids"] == ["C001", "C002"]
        assert status["events_logged_today"] == 4
        assert status["log_size_mb"] >= 0.0
        assert status["trigger_rules_loaded"] == 2

    def test_build_status_connected(self, log_root, config_dir, state_dir):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"stdout": "active"})()
            status = _build_status(log_root, config_dir, state_dir, [])

        assert status["connected"] is True


# ---------------------------------------------------------------------------
# Tool handler tests (integration-level, use temp dirs)
# ---------------------------------------------------------------------------


class TestToolHandlers:
    """Tests for MCP tool handler functions."""

    def test_slack_log_search_empty_query(self):
        result = json.loads(_handle_slack_log_search({"query": ""}))
        assert "error" in result

    def test_slack_log_search_jsonl_fallback(self, log_root):
        """Test JSONL scan fallback when FTS5 is unavailable."""
        with (
            patch("mcp_server._get_keyword_index", return_value=None),
            patch("mcp_server._LOG_ROOT", log_root),
        ):
            result = json.loads(_handle_slack_log_search({
                "query": "deploy",
                "channel_id": "C001",
            }))
            assert result["source"] == "jsonl_scan"
            assert result["result_count"] >= 1

    def test_slack_log_search_fts5(self, log_root, fts_index, state_dir):
        """Test FTS5 search when index is available."""
        with (
            patch("mcp_server._LOG_ROOT", log_root),
            patch("mcp_server._STATE_DIR", state_dir),
        ):
            from keyword_index import KeywordIndex
            idx = KeywordIndex(state_dir=state_dir)
            with patch("mcp_server._get_keyword_index", return_value=idx):
                result = json.loads(_handle_slack_log_search({
                    "query": "deploy",
                }))
                assert result["source"] == "fts5"
                assert result["result_count"] >= 1

    def test_slack_channel_summary_missing_channel(self):
        result = json.loads(_handle_slack_channel_summary({"channel_id": ""}))
        assert "error" in result

    def test_slack_channel_summary(self, log_root):
        with patch("mcp_server._LOG_ROOT", log_root):
            result = json.loads(_handle_slack_channel_summary({
                "channel_id": "C001",
            }))
            assert result["message_count"] == 4
            assert "alice" in result["participants"]

    def test_slack_thread_summary_missing_params(self):
        result = json.loads(_handle_slack_thread_summary({}))
        assert "error" in result

    def test_slack_thread_summary(self, log_root):
        """Thread summary uses ts to derive date, so create logs at that date too."""
        # The fixture logs are at today's date. Create a ts that maps to today.
        today_epoch = datetime.now(timezone.utc).replace(
            hour=14, minute=0, second=0, microsecond=0
        ).timestamp()
        thread_ts = f"{today_epoch:.3f}"

        # Create messages with this thread_ts in the fixture
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = log_root / "channels" / "C001" / f"{today}.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps({
                "ts": thread_ts,
                "channel_id": "C001",
                "username": "alice",
                "text": "thread root",
                "thread_ts": None,
            }) + "\n")
            f.write(json.dumps({
                "ts": f"{today_epoch + 1:.3f}",
                "channel_id": "C001",
                "username": "bob",
                "text": "reply in thread",
                "thread_ts": thread_ts,
            }) + "\n")

        with patch("mcp_server._LOG_ROOT", log_root):
            result = json.loads(_handle_slack_thread_summary({
                "channel_id": "C001",
                "thread_ts": thread_ts,
            }))
            assert result["message_count"] >= 1

    def test_slack_status(self, log_root, config_dir, state_dir):
        with (
            patch("mcp_server._LOG_ROOT", log_root),
            patch("mcp_server._CONFIG_DIR", config_dir),
            patch("mcp_server._STATE_DIR", state_dir),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = type("R", (), {"stdout": "active"})()
            result = json.loads(_handle_slack_status({}))
            assert result["connected"] is True
            assert result["events_logged_today"] == 4


class TestSearchLimitCap:
    """Verify the limit parameter is capped at 200."""

    def test_limit_capped(self, log_root):
        with (
            patch("mcp_server._get_keyword_index", return_value=None),
            patch("mcp_server._LOG_ROOT", log_root),
        ):
            result = json.loads(_handle_slack_log_search({
                "query": "deploy",
                "limit": 500,
            }))
            # The result count should be limited even if 500 was requested
            assert result["result_count"] <= 200
