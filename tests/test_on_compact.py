"""
Unit tests for hooks/on-compact.py

Tests cover:
- _is_compact_event(): returns True when hook_name=="compact"
- _is_compact_event(): returns False when hook_name is absent
- _is_compact_event(): returns False when hook_name is a different value
- _log_compact_event(): writes a structured log line to the log file
- _log_compact_event(): is silent on failure (no crash when log dir absent)
- write_last_restart_reason(): writes reason and ts to the JSON file
- write_last_restart_reason(): is silent on failure (no crash)
- main(): exits 0 without side effects when hook_name is absent (non-compact)
- main(): exits 0 without side effects when hook_name="startup"
- main(): calls write_last_restart_reason("compaction") on compact events
- main(): calls send_compaction_notify() on compact events
- main(): skips all compact actions when not a compact event
- send_compaction_notify(): logs telegram_ok on success
- send_compaction_notify(): logs telegram_failed on HTTP error
- send_compaction_notify(): logs telegram_skipped when credentials missing
"""

import importlib.util
import json
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOKS_DIR = Path(__file__).parent.parent / "hooks"


def _load_hook(
    compact_log_file: str | None = None,
    restart_reason_file: str | None = None,
    compaction_state_file: str | None = None,
    state_file: str | None = None,
    last_compact_ts_file: str | None = None,
    heartbeat_file: str | None = None,
):
    """Load hooks/on-compact.py as a module without executing main().

    Accepts optional override paths for files written by the hook so tests
    can redirect output to temporary locations.

    heartbeat_file: override LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE so that
    _wfm_was_active() reads a controlled file instead of the live dispatcher
    heartbeat.  Pass a path to a non-existent file to force _wfm_was_active()
    to return False (needed for tests that assert _is_compact_event returns
    False when both source and hook_name are absent).
    """
    import os

    env_overrides = {}
    if compact_log_file:
        env_overrides["LOBSTER_COMPACT_LOG_FILE_OVERRIDE"] = compact_log_file
    if restart_reason_file:
        env_overrides["LOBSTER_LAST_RESTART_REASON_FILE_OVERRIDE"] = restart_reason_file
    if compaction_state_file:
        env_overrides["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_file
    if state_file:
        env_overrides["LOBSTER_STATE_FILE_OVERRIDE"] = state_file
    if last_compact_ts_file:
        env_overrides["LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE"] = last_compact_ts_file
    if heartbeat_file is not None:
        env_overrides["LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE"] = heartbeat_file

    with patch.dict(os.environ, env_overrides):
        spec = importlib.util.spec_from_file_location(
            "on_compact", HOOKS_DIR / "on-compact.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(HOOKS_DIR))
        try:
            spec.loader.exec_module(mod)
        finally:
            if str(HOOKS_DIR) in sys.path:
                sys.path.remove(str(HOOKS_DIR))
        return mod


# ---------------------------------------------------------------------------
# _is_compact_event() tests
# ---------------------------------------------------------------------------

class TestIsCompactEvent:
    def test_returns_true_when_hook_name_is_compact(self):
        """hook_name='compact' is the authoritative compact signal."""
        mod = _load_hook()
        assert mod._is_compact_event({"hook_name": "compact", "session_id": "abc"}) is True

    def test_returns_false_when_hook_name_absent(self, tmp_path):
        """Absent hook_name means not a compact event (fresh start or subagent).

        Uses a non-existent heartbeat override so _wfm_was_active() returns
        False (avoids test isolation issue when the live dispatcher is running).
        """
        absent_heartbeat = str(tmp_path / "no-heartbeat-file")
        mod = _load_hook(heartbeat_file=absent_heartbeat)
        assert mod._is_compact_event({"session_id": "abc"}) is False

    def test_returns_false_when_hook_name_is_startup(self):
        """hook_name='startup' is a fresh-start event, not a compact event."""
        mod = _load_hook()
        assert mod._is_compact_event({"hook_name": "startup", "session_id": "abc"}) is False

    def test_returns_false_when_hook_name_is_empty_string(self):
        """Empty string hook_name is not a compact event."""
        mod = _load_hook()
        assert mod._is_compact_event({"hook_name": "", "session_id": "abc"}) is False

    def test_returns_false_on_empty_dict(self, tmp_path):
        """Empty input (failed JSON parse fallback) is not a compact event.

        Uses a non-existent heartbeat override so _wfm_was_active() returns
        False (avoids test isolation issue when the live dispatcher is running).
        """
        absent_heartbeat = str(tmp_path / "no-heartbeat-file")
        mod = _load_hook(heartbeat_file=absent_heartbeat)
        assert mod._is_compact_event({}) is False


# ---------------------------------------------------------------------------
# _log_compact_event() tests
# ---------------------------------------------------------------------------

class TestLogCompactEvent:
    COMPACT_EVENT_TYPE = "compact_detected"
    DETAIL = "session_id='abcdef123456'"

    def test_appends_log_line_with_correct_fields(self, tmp_path):
        """Log line contains timestamp, event_type, and detail."""
        log_file = tmp_path / "on-compact.log"
        mod = _load_hook(compact_log_file=str(log_file))

        mod._log_compact_event(self.COMPACT_EVENT_TYPE, self.DETAIL)

        lines = log_file.read_text().splitlines()
        assert len(lines) == 1
        parts = lines[0].split(" | ")
        assert len(parts) == 3
        ts, event_type, detail = parts
        assert event_type == self.COMPACT_EVENT_TYPE
        assert detail == self.DETAIL
        # Timestamp must look like ISO UTC (starts with 20XX-...)
        assert ts.startswith("20") and ts.endswith("Z")

    def test_appends_multiple_lines_without_truncating(self, tmp_path):
        """Multiple calls append without overwriting prior content."""
        log_file = tmp_path / "on-compact.log"
        mod = _load_hook(compact_log_file=str(log_file))

        mod._log_compact_event("event_1", "first")
        mod._log_compact_event("event_2", "second")

        lines = log_file.read_text().splitlines()
        assert len(lines) == 2
        assert "event_1" in lines[0]
        assert "event_2" in lines[1]

    def test_silent_on_missing_parent_directory(self, tmp_path):
        """Logging to a path with non-existent parent does not raise."""
        nonexistent = tmp_path / "does_not_exist" / "subdir" / "on-compact.log"
        mod = _load_hook(compact_log_file=str(nonexistent))
        # mkdir(parents=True) is called inside _log_compact_event, so it should succeed
        mod._log_compact_event("test_event", "detail")
        assert nonexistent.exists()


# ---------------------------------------------------------------------------
# write_last_restart_reason() tests
# ---------------------------------------------------------------------------

class TestWriteLastRestartReason:
    def test_writes_reason_and_ts(self, tmp_path):
        """File contains 'reason' and 'ts' fields."""
        reason_file = tmp_path / "last-restart-reason.json"
        mod = _load_hook(restart_reason_file=str(reason_file))

        mod.write_last_restart_reason("compaction")

        data = json.loads(reason_file.read_text())
        assert data["reason"] == "compaction"
        assert data["ts"].endswith("Z")
        assert data["ts"].startswith("20")

    def test_writes_health_check_reason(self, tmp_path):
        """Reason field is preserved exactly as passed."""
        reason_file = tmp_path / "last-restart-reason.json"
        mod = _load_hook(restart_reason_file=str(reason_file))

        mod.write_last_restart_reason("health-check")

        data = json.loads(reason_file.read_text())
        assert data["reason"] == "health-check"

    def test_overwrites_previous_value(self, tmp_path):
        """Second write replaces the first."""
        reason_file = tmp_path / "last-restart-reason.json"
        mod = _load_hook(restart_reason_file=str(reason_file))

        mod.write_last_restart_reason("compaction")
        mod.write_last_restart_reason("health-check")

        data = json.loads(reason_file.read_text())
        assert data["reason"] == "health-check"

    def test_silent_when_write_fails(self, tmp_path):
        """No exception raised even if the path is unwritable."""
        # Point at a read-only directory to simulate failure
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        ro_dir.chmod(0o555)
        bad_file = ro_dir / "reason.json"

        mod = _load_hook(restart_reason_file=str(bad_file))
        # Should not raise
        mod.write_last_restart_reason("compaction")


# ---------------------------------------------------------------------------
# main() behavior tests
# ---------------------------------------------------------------------------

COMPACT_EVENT = {"hook_name": "compact", "session_id": "abc123def456"}
STARTUP_EVENT = {"hook_name": "startup", "session_id": "abc123def456"}
NO_HOOKNAME_EVENT = {"session_id": "abc123def456"}


class TestMainSkipsNonCompactEvents:
    """main() must be a no-op for non-compact SessionStart events."""

    def _run_main(self, data: dict, tmp_path: Path):
        """Run main() with mocked stdin and file overrides."""
        import io, os
        log_file = tmp_path / "on-compact.log"
        reason_file = tmp_path / "reason.json"
        state_file = tmp_path / "lobster-state.json"
        compaction_file = tmp_path / "compaction-state.json"
        compact_ts_file = tmp_path / "last-compact.ts"
        # Non-existent heartbeat file: forces _wfm_was_active() to return False
        # so tests that expect non-compact behavior are isolated from the live
        # dispatcher heartbeat (which would otherwise make _is_compact_event
        # return True via the tier-3 filesystem fallback).
        absent_heartbeat = str(tmp_path / "no-heartbeat")

        mod = _load_hook(
            compact_log_file=str(log_file),
            restart_reason_file=str(reason_file),
            state_file=str(state_file),
            compaction_state_file=str(compaction_file),
            last_compact_ts_file=str(compact_ts_file),
            heartbeat_file=absent_heartbeat,
        )

        with patch("sys.stdin", io.StringIO(json.dumps(data))):
            try:
                mod.main()
            except SystemExit:
                pass

        return {
            "log_file": log_file,
            "reason_file": reason_file,
            "state_file": state_file,
            "compaction_file": compaction_file,
            "compact_ts_file": compact_ts_file,
        }

    def test_exits_0_and_skips_all_writes_when_hook_name_absent(self, tmp_path):
        """Non-compact event: returns immediately, no state files written."""
        result = self._run_main(NO_HOOKNAME_EVENT, tmp_path)
        assert not result["reason_file"].exists()
        assert not result["state_file"].exists()
        assert not result["compaction_file"].exists()

    def test_logs_skipped_not_compact_when_hook_name_absent(self, tmp_path):
        """Skipped events must still log a 'skipped_not_compact' entry."""
        result = self._run_main(NO_HOOKNAME_EVENT, tmp_path)
        log_content = result["log_file"].read_text()
        assert "skipped_not_compact" in log_content

    def test_exits_0_and_skips_all_writes_when_hook_name_is_startup(self, tmp_path):
        """Startup event: same no-op behavior as absent hook_name."""
        result = self._run_main(STARTUP_EVENT, tmp_path)
        assert not result["reason_file"].exists()

    def test_logs_skipped_not_compact_when_hook_name_is_startup(self, tmp_path):
        result = self._run_main(STARTUP_EVENT, tmp_path)
        log_content = result["log_file"].read_text()
        assert "skipped_not_compact" in log_content


class TestMainOnCompactEvent:
    """main() must write restart-reason and state files on compact events."""

    def _run_main_compact(self, tmp_path: Path):
        """Run main() with a compact event, return results."""
        import io
        log_file = tmp_path / "on-compact.log"
        reason_file = tmp_path / "reason.json"
        state_file = tmp_path / "lobster-state.json"
        compaction_file = tmp_path / "compaction-state.json"
        compact_ts_file = tmp_path / "last-compact.ts"
        sentinel_file = tmp_path / "compact-pending"
        outbox_dir = tmp_path / "outbox"

        mod = _load_hook(
            compact_log_file=str(log_file),
            restart_reason_file=str(reason_file),
            state_file=str(state_file),
            compaction_state_file=str(compaction_file),
            last_compact_ts_file=str(compact_ts_file),
        )
        # Override SENTINEL_FILE and OUTBOX_DIR paths to temp locations
        mod.SENTINEL_FILE = sentinel_file
        mod.OUTBOX_DIR = outbox_dir

        with patch("sys.stdin", io.StringIO(json.dumps(COMPACT_EVENT))), \
             patch.object(mod, "_parse_config_env", return_value={
                 "TELEGRAM_ALLOWED_USERS": "12345",
             }), \
             patch.object(mod, "_is_dispatcher_compact", return_value=False):
            mod.main()

        return {
            "log_file": log_file,
            "reason_file": reason_file,
            "state_file": state_file,
            "compaction_file": compaction_file,
            "outbox_dir": outbox_dir,
        }

    def test_writes_compaction_reason_to_restart_reason_file(self, tmp_path):
        """Compact event writes reason=compaction to last-restart-reason.json."""
        result = self._run_main_compact(tmp_path)
        data = json.loads(result["reason_file"].read_text())
        assert data["reason"] == "compaction"

    def test_logs_compact_detected(self, tmp_path):
        """Compact event logs 'compact_detected' to on-compact.log."""
        result = self._run_main_compact(tmp_path)
        log_content = result["log_file"].read_text()
        assert "compact_detected" in log_content

    def test_sends_telegram_notification_via_outbox(self, tmp_path):
        """Compact event writes a Telegram notification file to the outbox."""
        result = self._run_main_compact(tmp_path)
        outbox_files = list(result["outbox_dir"].glob("*.json"))
        assert len(outbox_files) == 1
        payload = json.loads(outbox_files[0].read_text())
        assert payload["source"] == "telegram"
        assert payload["chat_id"] == "12345"

    def test_writes_compaction_state_files(self, tmp_path):
        """Compact event writes compaction-state.json and lobster-state.json."""
        result = self._run_main_compact(tmp_path)
        assert result["state_file"].exists()
        assert result["compaction_file"].exists()


# ---------------------------------------------------------------------------
# send_compaction_notify() Telegram credential tests
# ---------------------------------------------------------------------------

class TestSendCompactionNotifyCredentials:
    def _make_mod(self, tmp_path):
        outbox_dir = tmp_path / "outbox"
        mod = _load_hook()
        mod.OUTBOX_DIR = outbox_dir
        return mod, outbox_dir

    def test_skips_notify_when_allowed_users_missing(self, tmp_path):
        """Missing TELEGRAM_ALLOWED_USERS: no outbox file written."""
        mod, outbox_dir = self._make_mod(tmp_path)
        with patch.object(mod, "_parse_config_env", return_value={}):
            mod.send_compaction_notify()
        assert not outbox_dir.exists() or len(list(outbox_dir.glob("*.json"))) == 0

    def test_writes_outbox_file_when_credentials_present(self, tmp_path):
        """TELEGRAM_ALLOWED_USERS present: outbox file written with correct fields."""
        mod, outbox_dir = self._make_mod(tmp_path)
        with patch.object(mod, "_parse_config_env", return_value={
            "TELEGRAM_ALLOWED_USERS": "12345",
        }):
            mod.send_compaction_notify()
        outbox_files = list(outbox_dir.glob("*.json"))
        assert len(outbox_files) == 1
        payload = json.loads(outbox_files[0].read_text())
        assert payload["source"] == "telegram"
        assert payload["chat_id"] == "12345"
