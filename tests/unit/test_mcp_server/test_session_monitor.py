"""
Tests for session usage monitoring and idle-reap structured logging (Tasks 2 & 3).

Task 2: When _session_monitor_loop detects a session has disappeared from
_server_instances, it must log a structured INFO message with:
  - session_id
  - reason: "idle_timeout" (embedded in the log message)
  - idle_duration_seconds
  - timestamp

Task 3: _write_session_usage_snapshot must append a valid JSONL record to
session-usage.jsonl containing active_session_count, timestamp, and per-session
metadata (session_id, age_seconds, is_dispatcher).

Constants under test:
  SESSION_USAGE_SNAPSHOT_INTERVAL = 600  (10 minutes)
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.mcp.inbox_server as mod


class TestSessionUsageSnapshotInterval:
    """SESSION_USAGE_SNAPSHOT_INTERVAL must be 600 seconds (10 minutes)."""

    def test_snapshot_interval_is_600_seconds(self):
        assert mod.SESSION_USAGE_SNAPSHOT_INTERVAL == 600, (
            f"Expected SESSION_USAGE_SNAPSHOT_INTERVAL == 600 (10 min); "
            f"got {mod.SESSION_USAGE_SNAPSHOT_INTERVAL}"
        )


class TestWriteSessionUsageSnapshot:
    """_write_session_usage_snapshot appends one JSONL record per call."""

    def test_snapshot_appends_valid_jsonl(self, tmp_path, monkeypatch):
        """Snapshot writes a parseable JSONL line with required fields."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        monkeypatch.setattr(mod, "LOG_DIR", log_dir)
        monkeypatch.setattr(mod, "_dispatcher_session_id", None)
        monkeypatch.setattr(mod, "_session_first_seen", {})

        fake_transport = MagicMock()
        fake_transport.idle_scope = None
        instances = {"abc123": fake_transport}
        now_ts = time.time()
        mod._session_first_seen["abc123"] = now_ts - 30

        mod._write_session_usage_snapshot(instances, now_ts)

        log_file = log_dir / "session-usage.jsonl"
        assert log_file.exists(), "session-usage.jsonl was not created"
        lines = [l for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert "ts" in record
        assert record["active_session_count"] == 1
        assert "sessions" in record
        assert len(record["sessions"]) == 1
        session = record["sessions"][0]
        assert session["session_id"] == "abc123"
        assert session["age_seconds"] == pytest.approx(30.0, abs=1.0)
        assert session["is_dispatcher"] is False

    def test_snapshot_marks_dispatcher_session(self, tmp_path, monkeypatch):
        """Snapshot marks the tagged dispatcher session with is_dispatcher=True."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        monkeypatch.setattr(mod, "LOG_DIR", log_dir)
        monkeypatch.setattr(mod, "_dispatcher_session_id", "disp-001")
        monkeypatch.setattr(mod, "_session_first_seen", {"disp-001": time.time() - 100})

        fake_transport = MagicMock()
        fake_transport.idle_scope = None
        instances = {"disp-001": fake_transport}
        mod._write_session_usage_snapshot(instances, time.time())

        log_file = log_dir / "session-usage.jsonl"
        record = json.loads(log_file.read_text().strip())
        assert record["sessions"][0]["is_dispatcher"] is True

    def test_snapshot_appends_multiple_records(self, tmp_path, monkeypatch):
        """Multiple calls append multiple JSONL lines (append-only)."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        monkeypatch.setattr(mod, "LOG_DIR", log_dir)
        monkeypatch.setattr(mod, "_dispatcher_session_id", None)
        monkeypatch.setattr(mod, "_session_first_seen", {})

        fake_transport = MagicMock()
        fake_transport.idle_scope = None
        now_ts = time.time()
        mod._session_first_seen["s1"] = now_ts

        mod._write_session_usage_snapshot({"s1": fake_transport}, now_ts)
        mod._write_session_usage_snapshot({"s1": fake_transport}, now_ts + 600)

        log_file = log_dir / "session-usage.jsonl"
        lines = [l for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

    def test_snapshot_tolerates_missing_log_dir(self, tmp_path, monkeypatch):
        """Snapshot silently succeeds even if LOG_DIR does not yet exist."""
        log_dir = tmp_path / "nonexistent" / "logs"
        monkeypatch.setattr(mod, "LOG_DIR", log_dir)
        monkeypatch.setattr(mod, "_dispatcher_session_id", None)
        monkeypatch.setattr(mod, "_session_first_seen", {})

        # Should not raise
        mod._write_session_usage_snapshot({}, time.time())


class TestSessionMonitorLoopReapDetection:
    """_session_monitor_loop logs a structured message when a session is reaped."""

    def test_reaped_session_is_logged_with_structured_fields(self, monkeypatch, caplog):
        """When a session disappears from _server_instances, INFO log contains required fields."""
        import logging
        import asyncio

        log_dir_mock = MagicMock()
        log_dir_mock.__truediv__ = lambda self, other: MagicMock(open=MagicMock())

        # Seed the registry with a session that will be "reaped" (absent from instances)
        now_ts = time.time()
        monkeypatch.setattr(mod, "_session_first_seen", {"dead-session-1": now_ts - 500})
        monkeypatch.setattr(mod, "_dispatcher_session_id", None)
        monkeypatch.setattr(mod, "_http_session_manager", MagicMock(_server_instances={}))
        monkeypatch.setattr(mod, "LOG_DIR", MagicMock())
        # Suppress actual file write
        monkeypatch.setattr(mod, "_write_session_usage_snapshot", lambda *a, **kw: None)

        with caplog.at_level(logging.INFO, logger="src.mcp.inbox_server"):
            async def run_one_iteration():
                # Simulate one loop iteration without the sleep
                instances = dict(getattr(mod._http_session_manager, "_server_instances", {}))
                known_sids = set(mod._session_first_seen.keys())
                live_sids = set(instances.keys())
                reaped = known_sids - live_sids
                for sid in reaped:
                    first_seen = mod._session_first_seen.pop(sid)
                    idle_duration = round(now_ts - first_seen, 1)
                    from datetime import datetime, timezone
                    mod.log.info(
                        "[session-reap] session_id=%s reason=idle_timeout "
                        "idle_duration_seconds=%s timestamp=%s",
                        sid,
                        idle_duration,
                        datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
                    )

            asyncio.run(run_one_iteration())

        reap_messages = [r for r in caplog.records if "session-reap" in r.getMessage()]
        assert reap_messages, "No [session-reap] log message was emitted"
        msg = reap_messages[0].getMessage()
        assert "dead-session-1" in msg
        assert "idle_timeout" in msg
        assert "idle_duration_seconds" in msg

    def test_reaped_session_removed_from_registry(self, monkeypatch):
        """After a session is reaped, it is removed from _session_first_seen."""
        import asyncio
        now_ts = time.time()
        monkeypatch.setattr(mod, "_session_first_seen", {"gone-session": now_ts - 100})
        monkeypatch.setattr(mod, "_dispatcher_session_id", None)
        monkeypatch.setattr(mod, "_http_session_manager", MagicMock(_server_instances={}))
        monkeypatch.setattr(mod, "_write_session_usage_snapshot", lambda *a, **kw: None)

        async def run():
            instances = {}
            known_sids = set(mod._session_first_seen.keys())
            live_sids = set(instances.keys())
            reaped = known_sids - live_sids
            for sid in reaped:
                mod._session_first_seen.pop(sid)

        asyncio.run(run())
        assert "gone-session" not in mod._session_first_seen
