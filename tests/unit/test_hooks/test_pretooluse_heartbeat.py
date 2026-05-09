"""
Unit tests for hooks/pretooluse-heartbeat.deprecated.py

This hook is deprecated (superseded by hooks/pre-tool-heartbeat.py, issue #1786).
The deprecated file is retained for audit purposes only — it should never be
registered in settings.json. If it fires, it writes a warning to a JSONL log
and also writes to /tmp/pretooluse-heartbeat-deprecated-fired.log.

Tests cover:
- Hook exits 0 even though it is deprecated (must not block tool execution)
- Firing writes a warning entry to deprecated-hook-fired.jsonl
- Log entry contains expected fields (timestamp, event, hook name, message)
- Log entry timestamp is parseable ISO UTC
- Failure to write to log does not propagate — still exits 0
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "pretooluse-heartbeat.deprecated.py"

# Named constants matching the spec (issue #2001)
DEPRECATED_HOOK_NAME = "pretooluse-heartbeat.deprecated.py"
DEPRECATED_LOG_FILENAME = "deprecated-hook-fired.jsonl"
DEPRECATED_EVENT_TYPE = "deprecated_hook_fired"


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _run_hook(monkeypatch, messages_dir: Path) -> tuple[int, str, str]:
    """Execute the hook's main() capturing exit code and stdio."""
    monkeypatch.setenv("LOBSTER_MESSAGES", str(messages_dir))

    spec = importlib.util.spec_from_file_location("pretooluse_heartbeat_deprecated", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)

    stdout_cap = StringIO()
    stderr_cap = StringIO()

    exit_code = None
    with (
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
    ):
        try:
            spec.loader.exec_module(mod)
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


# ---------------------------------------------------------------------------
# Behavior tests: deprecated hook must log and exit 0
# ---------------------------------------------------------------------------

class TestDeprecatedHookExitsZero:
    def test_exits_zero_on_successful_log(self, monkeypatch, tmp_path):
        """Deprecated hook must exit 0 — never block tool execution."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        code, _, _ = _run_hook(monkeypatch, messages_dir)
        assert code == 0

    def test_exits_zero_even_when_log_write_fails(self, monkeypatch, tmp_path):
        """Write failure must not block the tool call — degrades gracefully."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        # Make logs dir read-only so write fails
        logs_dir = messages_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.chmod(0o444)

        try:
            code, _, _ = _run_hook(monkeypatch, messages_dir)
            assert code == 0
        finally:
            logs_dir.chmod(0o755)


class TestDeprecatedHookLogsWarning:
    def test_writes_jsonl_entry_when_fired(self, monkeypatch, tmp_path):
        """Firing writes a structured warning entry to deprecated-hook-fired.jsonl."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        _run_hook(monkeypatch, messages_dir)

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        assert log_file.exists(), "deprecated-hook-fired.jsonl must be created when hook fires"

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1, "Exactly one JSONL entry per firing"

    def test_log_entry_has_required_fields(self, monkeypatch, tmp_path):
        """Log entry must contain timestamp, event, hook name, and message."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        _run_hook(monkeypatch, messages_dir)

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        entry = json.loads(log_file.read_text().strip().splitlines()[0])

        assert "timestamp" in entry
        assert "event" in entry
        assert "hook" in entry
        assert "message" in entry

    def test_log_entry_event_type_is_deprecated_hook_fired(self, monkeypatch, tmp_path):
        """Event type must be 'deprecated_hook_fired' for observability filters."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        _run_hook(monkeypatch, messages_dir)

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        entry = json.loads(log_file.read_text().strip().splitlines()[0])
        assert entry["event"] == DEPRECATED_EVENT_TYPE

    def test_log_entry_hook_name_matches_filename(self, monkeypatch, tmp_path):
        """Hook name in log entry identifies which file fired."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        _run_hook(monkeypatch, messages_dir)

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        entry = json.loads(log_file.read_text().strip().splitlines()[0])
        assert entry["hook"] == DEPRECATED_HOOK_NAME

    def test_log_entry_timestamp_is_utc_iso(self, monkeypatch, tmp_path):
        """Timestamp must be parseable ISO UTC so log scanners can sort by time."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        _run_hook(monkeypatch, messages_dir)

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        entry = json.loads(log_file.read_text().strip().splitlines()[0])
        ts = datetime.fromisoformat(entry["timestamp"])
        assert ts.tzinfo is not None
        assert ts.utcoffset().total_seconds() == 0

    def test_log_appends_on_repeated_firings(self, monkeypatch, tmp_path):
        """Each firing appends a new line — log accumulates for audit."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        _run_hook(monkeypatch, messages_dir)
        _run_hook(monkeypatch, messages_dir)

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2, "Two firings must produce two log entries"

    def test_creates_logs_dir_if_absent(self, monkeypatch, tmp_path):
        """Hook must create ~/messages/logs/ if it doesn't exist yet."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        # logs/ does not exist yet
        _run_hook(monkeypatch, messages_dir)

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        assert log_file.exists()


class TestTmpWriteFailureDegradesgracefully:
    def test_exits_zero_when_tmp_write_raises(self, monkeypatch, tmp_path):
        """/tmp write failure must not propagate — hook must still exit 0."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        monkeypatch.setenv("LOBSTER_MESSAGES", str(messages_dir))

        spec = importlib.util.spec_from_file_location(
            "pretooluse_heartbeat_deprecated_tmpfail", HOOK_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        original_write_text = Path.write_text

        def _raise_on_tmp(self, *args, **kwargs):
            if str(self).startswith("/tmp"):
                raise OSError("Simulated /tmp write failure")
            return original_write_text(self, *args, **kwargs)

        exit_code = None
        with patch.object(Path, "write_text", _raise_on_tmp):
            try:
                mod.main()
            except SystemExit as e:
                exit_code = e.code

        assert exit_code == 0, "/tmp write failure must not block tool execution"

    def test_jsonl_log_still_written_when_tmp_write_raises(self, monkeypatch, tmp_path):
        """JSONL log must still be written even if /tmp write raises."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir(exist_ok=True)
        monkeypatch.setenv("LOBSTER_MESSAGES", str(messages_dir))

        spec = importlib.util.spec_from_file_location(
            "pretooluse_heartbeat_deprecated_tmpfail2", HOOK_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        original_write_text = Path.write_text

        def _raise_on_tmp(self, *args, **kwargs):
            if str(self).startswith("/tmp"):
                raise OSError("Simulated /tmp write failure")
            return original_write_text(self, *args, **kwargs)

        with patch.object(Path, "write_text", _raise_on_tmp):
            try:
                mod.main()
            except SystemExit:
                pass

        log_file = messages_dir / "logs" / DEPRECATED_LOG_FILENAME
        assert log_file.exists(), "JSONL log must be written even when /tmp write fails"
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1, "Exactly one JSONL entry must be present"
