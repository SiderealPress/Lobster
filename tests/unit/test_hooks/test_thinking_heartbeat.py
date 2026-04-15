"""
Unit tests for hooks/thinking-heartbeat.py

Tests cover the simplified sentinel design (issue #1483):
- write_heartbeat() writes a Unix epoch integer to the heartbeat file
- Atomic write: uses .tmp then rename, no .tmp left behind
- Creates parent directory if absent
- Overwrites existing content on each call
- Timestamp is within a small window of time.time()
- main() exits 0 on success
- main() exits 0 even when write fails (silent failure — never block tool use)
- LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE env var is respected

Design change from the original (lobster-state.json merge):
- No JSON parsing or merging
- Single integer epoch value, not ISO timestamp
- Single file — no other state touched
"""

import importlib.util
import os
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "thinking-heartbeat.py"

# How close (in seconds) the written timestamp must be to now.
TIMESTAMP_TOLERANCE_SECONDS = 5

# The threshold documented in the hook (checked here to prevent silent drift).
EXPECTED_STALE_THRESHOLD = 1200  # 20 minutes


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_module(monkeypatch, heartbeat_file: Path):
    """Load thinking-heartbeat as a fresh module with heartbeat file override."""
    monkeypatch.setenv("LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE", str(heartbeat_file))
    spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestWriteHeartbeat:
    def _load_raw(self):
        """Load module without any env override (uses default paths internally)."""
        spec = importlib.util.spec_from_file_location("th", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_writes_integer_epoch_to_file(self, tmp_path):
        mod = self._load_raw()
        hb = tmp_path / "dispatcher-heartbeat"
        before = int(time.time())
        mod.write_heartbeat(hb)
        after = int(time.time())
        assert hb.exists()
        content = hb.read_text().strip()
        ts = int(content)
        assert before <= ts <= after + 1  # allow 1s rounding

    def test_content_is_pure_integer_no_json(self, tmp_path):
        mod = self._load_raw()
        hb = tmp_path / "dispatcher-heartbeat"
        mod.write_heartbeat(hb)
        content = hb.read_text().strip()
        # Must be parseable as int, not JSON
        ts = int(content)
        assert ts > 0

    def test_no_tmp_file_left_behind(self, tmp_path):
        mod = self._load_raw()
        hb = tmp_path / "dispatcher-heartbeat"
        mod.write_heartbeat(hb)
        tmp = hb.with_suffix(".tmp")
        assert not tmp.exists()

    def test_creates_parent_directory(self, tmp_path):
        mod = self._load_raw()
        nested = tmp_path / "nested" / "deep" / "dispatcher-heartbeat"
        mod.write_heartbeat(nested)
        assert nested.exists()

    def test_overwrites_previous_content(self, tmp_path):
        mod = self._load_raw()
        hb = tmp_path / "dispatcher-heartbeat"
        hb.write_text("99999\n")
        time.sleep(0.01)
        mod.write_heartbeat(hb)
        content = hb.read_text().strip()
        ts = int(content)
        # New timestamp should be recent (not the old 99999)
        assert ts > 1000000000  # sanity: real epoch, not legacy value

    def test_timestamp_within_tolerance_of_now(self, tmp_path):
        mod = self._load_raw()
        hb = tmp_path / "dispatcher-heartbeat"
        before = time.time()
        mod.write_heartbeat(hb)
        after = time.time()
        ts = int(hb.read_text().strip())
        assert before - TIMESTAMP_TOLERANCE_SECONDS <= ts <= after + TIMESTAMP_TOLERANCE_SECONDS


class TestStaleThresholdConstant:
    """Verify the documented threshold matches the expected value (prevents silent drift)."""

    def test_stale_threshold_is_1200_seconds(self):
        spec = importlib.util.spec_from_file_location("th", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.DISPATCHER_HEARTBEAT_STALE_SECONDS == EXPECTED_STALE_THRESHOLD


# ---------------------------------------------------------------------------
# Hook main() integration tests
# ---------------------------------------------------------------------------

def _run_hook(monkeypatch, heartbeat_file: Path) -> tuple[int, str, str]:
    """Execute the hook's main() capturing exit code and stdio."""
    monkeypatch.setenv("LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE", str(heartbeat_file))

    spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
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


class TestHookMain:
    def test_exits_zero_on_success(self, monkeypatch, tmp_path):
        hb = tmp_path / "dispatcher-heartbeat"
        code, _, _ = _run_hook(monkeypatch, hb)
        assert code == 0

    def test_writes_heartbeat_on_success(self, monkeypatch, tmp_path):
        hb = tmp_path / "dispatcher-heartbeat"
        _run_hook(monkeypatch, hb)
        assert hb.exists()
        ts = int(hb.read_text().strip())
        assert ts > 0

    def test_exits_zero_even_when_write_fails(self, monkeypatch, tmp_path):
        """Hook must never block tool execution even if write fails."""
        readonly_dir = tmp_path / "readonly_dir"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)  # read-only directory
        hb = readonly_dir / "dispatcher-heartbeat"

        try:
            code, _, _ = _run_hook(monkeypatch, hb)
            assert code == 0
        finally:
            readonly_dir.chmod(0o755)  # restore for cleanup

    def test_env_override_respected(self, monkeypatch, tmp_path):
        """LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE must be used when set."""
        custom = tmp_path / "custom-heartbeat"
        code, _, _ = _run_hook(monkeypatch, custom)
        assert code == 0
        assert custom.exists()


# ---------------------------------------------------------------------------
# Backward compatibility: the old lobster-state.json fields are NOT written
# ---------------------------------------------------------------------------

class TestNoLobsterStateWrites:
    """The new hook must NOT write last_thinking_at to lobster-state.json.

    The health check no longer reads lobster-state.json for liveness signals.
    Writing it would be harmless but signals incorrect design intent.
    """

    def test_does_not_create_state_json(self, monkeypatch, tmp_path):
        hb = tmp_path / "dispatcher-heartbeat"
        # Point state file override at a location we can check
        state_file = tmp_path / "lobster-state.json"
        monkeypatch.setenv("LOBSTER_STATE_FILE_OVERRIDE", str(state_file))
        _run_hook(monkeypatch, hb)
        # The new hook should NOT write lobster-state.json
        assert not state_file.exists(), "thinking-heartbeat.py must not write lobster-state.json"
