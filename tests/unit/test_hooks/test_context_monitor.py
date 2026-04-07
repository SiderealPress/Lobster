"""
Unit tests for hooks/context-monitor.py (issue #1430).

Root cause: the hook silently returned when context_window was None (typical for
MCP tool PostToolUse payloads).  This made "hook fired, no data" indistinguishable
from "hook never fired."

Fixes verified:
1. When context_window is absent, the hook logs a WARN entry to context-monitor.log
   instead of silently returning.
2. The hook continues to log usage and write warnings when context_window is present.
3. The WARN log entry contains the tool name and indicates context_window was absent.
"""

import importlib.util
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "context-monitor.py"

# Named constant matching the spec — the log line prefix is load-bearing
# for any downstream log parser.
WARN_PREFIX_ABSENT_CONTEXT = "[WARN] context_window absent"


def _load_hook():
    """Load context-monitor as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("context_monitor", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_log(log_dir: Path) -> list[dict]:
    log_file = log_dir / "context-monitor.log"
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]


class TestContextMonitorAbsentContextWindow:
    """When context_window is absent, hook must log a WARN rather than silently return."""

    def test_logs_warn_when_context_window_is_none(self, tmp_path, monkeypatch):
        """Payload with no context_window key → WARN written to log."""
        mod = _load_hook()
        monkeypatch.setattr(mod.Path, "home", lambda: tmp_path)

        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True)

        payload = {"tool_name": "mcp__lobster-inbox__wait_for_messages"}
        mod._handle_payload(payload, log_dir=log_dir)

        entries = _read_log(log_dir)
        assert len(entries) == 1, f"Expected 1 log entry, got {len(entries)}: {entries}"
        entry = entries[0]
        assert "context_window_absent" in entry or WARN_PREFIX_ABSENT_CONTEXT in str(entry), (
            f"Expected warn entry for absent context_window, got: {entry}"
        )
        assert entry.get("tool") == "mcp__lobster-inbox__wait_for_messages"

    def test_warn_entry_includes_tool_name(self, tmp_path, monkeypatch):
        """WARN log entry must record which tool triggered the missing context_window."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True)

        payload = {"tool_name": "Bash"}
        mod._handle_payload(payload, log_dir=log_dir)

        entries = _read_log(log_dir)
        assert len(entries) >= 1
        entry = entries[0]
        assert entry.get("tool") == "Bash", (
            f"Expected tool='Bash' in warn entry, got: {entry}"
        )

    def test_no_inbox_message_written_when_context_absent(self, tmp_path, monkeypatch):
        """Missing context_window should never trigger a context_warning inbox message."""
        mod = _load_hook()
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True)

        payload = {"tool_name": "mcp__lobster-inbox__mark_processed"}
        mod._handle_payload(payload, log_dir=log_dir, inbox_dir=inbox_dir)

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 0, (
            f"No inbox message should be written for absent context_window, "
            f"but found: {inbox_files}"
        )


class TestContextMonitorNormalOperation:
    """When context_window is present, existing behavior must be preserved."""

    def test_logs_usage_below_threshold(self, tmp_path):
        """context_window present, below threshold → usage entry logged."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True)

        payload = {
            "tool_name": "Bash",
            "context_window": {
                "used_percentage": 45.0,
                "remaining_percentage": 55.0,
            },
        }
        mod._handle_payload(payload, log_dir=log_dir)

        entries = _read_log(log_dir)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["used_percentage"] == 45.0
        assert entry["tool"] == "Bash"
        # Should NOT have context_window_absent flag
        assert not entry.get("context_window_absent", False)

    def test_writes_inbox_warning_at_threshold(self, tmp_path):
        """At or above WARNING_THRESHOLD → context_warning written to inbox."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        dedup_flag = tmp_path / "lobster-context-warning-sent"

        # Patch paths
        original_dedup = mod.DEDUP_FLAG
        mod.DEDUP_FLAG = dedup_flag

        try:
            payload = {
                "tool_name": "Bash",
                "context_window": {
                    "used_percentage": 75.0,
                    "remaining_percentage": 25.0,
                },
            }
            mod._handle_payload(payload, log_dir=log_dir, inbox_dir=inbox_dir)
        finally:
            mod.DEDUP_FLAG = original_dedup

        inbox_files = list(inbox_dir.glob("context-warning-*.json"))
        assert len(inbox_files) == 1
        msg = json.loads(inbox_files[0].read_text())
        assert msg["type"] == "context_warning"
        assert msg["used_percentage"] == 75.0

    def test_dedup_suppresses_second_warning(self, tmp_path):
        """Dedup flag present → second warning is not written."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        dedup_flag = tmp_path / "lobster-context-warning-sent"
        dedup_flag.touch()  # Already flagged

        original_dedup = mod.DEDUP_FLAG
        mod.DEDUP_FLAG = dedup_flag
        try:
            payload = {
                "tool_name": "Bash",
                "context_window": {
                    "used_percentage": 80.0,
                    "remaining_percentage": 20.0,
                },
            }
            mod._handle_payload(payload, log_dir=log_dir, inbox_dir=inbox_dir)
        finally:
            mod.DEDUP_FLAG = original_dedup

        inbox_files = list(inbox_dir.glob("context-warning-*.json"))
        assert len(inbox_files) == 0, "Dedup flag should suppress second warning"


class TestContextMonitorHandlePayloadSignature:
    """_handle_payload() must accept log_dir and inbox_dir as parameters.

    This verifies the hook is refactored to accept injected paths (enabling
    the tests above) rather than hardcoding Path.home().
    """

    def test_handle_payload_accepts_log_dir_kwarg(self, tmp_path):
        """_handle_payload() must accept a log_dir keyword argument."""
        mod = _load_hook()
        import inspect
        sig = inspect.signature(mod._handle_payload)
        assert "log_dir" in sig.parameters, (
            "_handle_payload() must accept log_dir= for testability"
        )

    def test_handle_payload_accepts_inbox_dir_kwarg(self, tmp_path):
        """_handle_payload() must accept an inbox_dir keyword argument."""
        mod = _load_hook()
        import inspect
        sig = inspect.signature(mod._handle_payload)
        assert "inbox_dir" in sig.parameters, (
            "_handle_payload() must accept inbox_dir= for testability"
        )
