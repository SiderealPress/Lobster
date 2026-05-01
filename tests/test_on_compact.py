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
):
    """Load hooks/on-compact.py as a module without executing main().

    Accepts optional override paths for files written by the hook so tests
    can redirect output to temporary locations.
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

    def test_returns_false_when_hook_name_absent(self):
        """Absent hook_name means not a compact event (fresh start or subagent)."""
        mod = _load_hook()
        assert mod._is_compact_event({"session_id": "abc"}) is False

    def test_returns_false_when_hook_name_is_startup(self):
        """hook_name='startup' is a fresh-start event, not a compact event."""
        mod = _load_hook()
        assert mod._is_compact_event({"hook_name": "startup", "session_id": "abc"}) is False

    def test_returns_false_when_hook_name_is_empty_string(self):
        """Empty string hook_name is not a compact event."""
        mod = _load_hook()
        assert mod._is_compact_event({"hook_name": "", "session_id": "abc"}) is False

    def test_returns_false_on_empty_dict(self):
        """Empty input (failed JSON parse fallback) is not a compact event."""
        mod = _load_hook()
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

        mod = _load_hook(
            compact_log_file=str(log_file),
            restart_reason_file=str(reason_file),
            state_file=str(state_file),
            compaction_state_file=str(compaction_file),
            last_compact_ts_file=str(compact_ts_file),
        )

        with patch("sys.stdin", io.StringIO(json.dumps(data))):
            mod.main()

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
    """main() must write restart-reason and trigger Telegram notify on compact events."""

    def _run_main_compact(self, tmp_path: Path, telegram_ok: bool = True):
        """Run main() with a compact event, mock Telegram, return results."""
        import io
        log_file = tmp_path / "on-compact.log"
        reason_file = tmp_path / "reason.json"
        state_file = tmp_path / "lobster-state.json"
        compaction_file = tmp_path / "compaction-state.json"
        compact_ts_file = tmp_path / "last-compact.ts"
        sentinel_file = tmp_path / "compact-pending"

        mod = _load_hook(
            compact_log_file=str(log_file),
            restart_reason_file=str(reason_file),
            state_file=str(state_file),
            compaction_state_file=str(compaction_file),
            last_compact_ts_file=str(compact_ts_file),
        )
        # Override SENTINEL_FILE path
        mod.SENTINEL_FILE = sentinel_file

        # Mock _send_telegram_notify to avoid real HTTP calls
        mock_send = MagicMock(return_value=telegram_ok)

        with patch("sys.stdin", io.StringIO(json.dumps(COMPACT_EVENT))), \
             patch.object(mod, "_send_telegram_notify", mock_send), \
             patch.object(mod, "_parse_config_env", return_value={
                 "TELEGRAM_BOT_TOKEN": "fake-token",
                 "TELEGRAM_ALLOWED_USERS": "12345",
             }), \
             patch.object(mod, "_is_dispatcher_compact", return_value=False):
            mod.main()

        return {
            "mock_send": mock_send,
            "log_file": log_file,
            "reason_file": reason_file,
            "state_file": state_file,
            "compaction_file": compaction_file,
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

    def test_sends_telegram_notification(self, tmp_path):
        """Compact event calls _send_telegram_notify once."""
        result = self._run_main_compact(tmp_path)
        result["mock_send"].assert_called_once()

    def test_logs_telegram_ok_on_success(self, tmp_path):
        """Successful Telegram send is logged as telegram_ok."""
        result = self._run_main_compact(tmp_path, telegram_ok=True)
        log_content = result["log_file"].read_text()
        assert "telegram_ok" in log_content

    def test_logs_telegram_failed_on_failure(self, tmp_path):
        """Failed Telegram send is logged as telegram_failed."""
        result = self._run_main_compact(tmp_path, telegram_ok=False)
        log_content = result["log_file"].read_text()
        assert "telegram_failed" in log_content

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
        log_file = tmp_path / "on-compact.log"
        return _load_hook(compact_log_file=str(log_file)), log_file

    def test_logs_telegram_skipped_when_bot_token_missing(self, tmp_path):
        """Missing bot_token causes telegram_skipped log entry."""
        mod, log_file = self._make_mod(tmp_path)
        with patch.object(mod, "_parse_config_env", return_value={"TELEGRAM_ALLOWED_USERS": "123"}):
            mod.send_compaction_notify()
        assert "telegram_skipped" in log_file.read_text()

    def test_logs_telegram_skipped_when_allowed_users_missing(self, tmp_path):
        """Missing allowed_users causes telegram_skipped log entry."""
        mod, log_file = self._make_mod(tmp_path)
        with patch.object(mod, "_parse_config_env", return_value={"TELEGRAM_BOT_TOKEN": "token"}):
            mod.send_compaction_notify()
        assert "telegram_skipped" in log_file.read_text()

    def test_logs_telegram_ok_when_send_succeeds(self, tmp_path):
        """Successful send produces telegram_ok in log."""
        mod, log_file = self._make_mod(tmp_path)
        with patch.object(mod, "_parse_config_env", return_value={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_USERS": "12345",
        }), patch.object(mod, "_send_telegram_notify", return_value=True):
            mod.send_compaction_notify()
        assert "telegram_ok" in log_file.read_text()

    def test_logs_telegram_failed_when_send_fails(self, tmp_path):
        """Failed send produces telegram_failed in log."""
        mod, log_file = self._make_mod(tmp_path)
        with patch.object(mod, "_parse_config_env", return_value={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_USERS": "12345",
        }), patch.object(mod, "_send_telegram_notify", return_value=False):
            mod.send_compaction_notify()
        assert "telegram_failed" in log_file.read_text()
