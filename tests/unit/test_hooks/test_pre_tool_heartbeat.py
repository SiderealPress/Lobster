"""
Unit tests for hooks/pre-tool-heartbeat.py

Tests cover the PreToolUse heartbeat hook (issue #1786):
- write_heartbeat() writes a Unix epoch integer to the heartbeat file
- Atomic write: uses .tmp then rename, no .tmp left behind
- Creates parent directory if absent
- Overwrites existing content on each call
- Timestamp is within a small window of time.time()
- main() exits 0 on success
- main() exits 0 even when write fails (silent failure — never block tool use)
- LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE env var is respected
- Written to a DIFFERENT file than the PostToolUse heartbeat

The hook is intentionally symmetric with thinking-heartbeat.py (PostToolUse)
but writes to dispatcher-pre-tool-heartbeat instead of dispatcher-heartbeat.
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
HOOK_PATH = _HOOKS_DIR / "pre-tool-heartbeat.py"

# How close (in seconds) the written timestamp must be to now.
TIMESTAMP_TOLERANCE_SECONDS = 5


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_module(monkeypatch, heartbeat_file: Path):
    """Load pre-tool-heartbeat as a fresh module with heartbeat file override."""
    monkeypatch.setenv("LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE", str(heartbeat_file))
    spec = importlib.util.spec_from_file_location("pre_tool_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_raw():
    """Load module without any env override (uses default paths internally)."""
    spec = importlib.util.spec_from_file_location("pre_tool_heartbeat_raw", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestWriteHeartbeat:
    def test_writes_integer_epoch_to_file(self, tmp_path):
        mod = _load_raw()
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        before = int(time.time())
        mod.write_heartbeat(hb)
        after = int(time.time())
        assert hb.exists()
        content = hb.read_text().strip()
        ts = int(content)
        assert before <= ts <= after + 1  # allow 1s rounding

    def test_content_is_pure_integer_no_json(self, tmp_path):
        mod = _load_raw()
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        mod.write_heartbeat(hb)
        content = hb.read_text().strip()
        # Must be parseable as int, not JSON
        ts = int(content)
        assert ts > 0

    def test_no_tmp_file_left_behind(self, tmp_path):
        mod = _load_raw()
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        mod.write_heartbeat(hb)
        tmp = hb.with_suffix(".tmp")
        assert not tmp.exists()

    def test_creates_parent_directory(self, tmp_path):
        mod = _load_raw()
        nested = tmp_path / "nested" / "deep" / "dispatcher-pre-tool-heartbeat"
        mod.write_heartbeat(nested)
        assert nested.exists()

    def test_overwrites_previous_content(self, tmp_path):
        mod = _load_raw()
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        hb.write_text("99999\n")
        time.sleep(0.01)
        mod.write_heartbeat(hb)
        content = hb.read_text().strip()
        ts = int(content)
        # New timestamp should be recent (not the old 99999)
        assert ts > 1000000000  # sanity: real epoch, not legacy value

    def test_timestamp_within_tolerance_of_now(self, tmp_path):
        mod = _load_raw()
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        before = time.time()
        mod.write_heartbeat(hb)
        after = time.time()
        ts = int(hb.read_text().strip())
        assert before - TIMESTAMP_TOLERANCE_SECONDS <= ts <= after + TIMESTAMP_TOLERANCE_SECONDS


# ---------------------------------------------------------------------------
# Separation from PostToolUse heartbeat file
# ---------------------------------------------------------------------------

class TestHeartbeatFileSeparation:
    """Pre-tool heartbeat must use a different filename than the PostToolUse heartbeat."""

    def test_default_filename_is_not_dispatcher_heartbeat(self):
        """Default file must be dispatcher-pre-tool-heartbeat, not dispatcher-heartbeat."""
        mod = _load_raw()
        assert "pre-tool" in str(mod.HEARTBEAT_FILE), (
            f"Expected 'pre-tool' in heartbeat path, got: {mod.HEARTBEAT_FILE}"
        )

    def test_default_filename_differs_from_post_tool_heartbeat(self):
        """Pre-tool and post-tool heartbeat files must be different paths."""
        pre_spec = importlib.util.spec_from_file_location("pre_hb", HOOK_PATH)
        pre_mod = importlib.util.module_from_spec(pre_spec)
        pre_spec.loader.exec_module(pre_mod)

        post_path = _HOOKS_DIR / "thinking-heartbeat.py"
        post_spec = importlib.util.spec_from_file_location("post_hb", post_path)
        post_mod = importlib.util.module_from_spec(post_spec)
        post_spec.loader.exec_module(post_mod)

        assert pre_mod.HEARTBEAT_FILE != post_mod.HEARTBEAT_FILE, (
            "Pre-tool and post-tool heartbeat hooks must write to different files"
        )


# ---------------------------------------------------------------------------
# Hook main() integration tests
# ---------------------------------------------------------------------------

def _run_hook(monkeypatch, heartbeat_file: Path) -> tuple[int, str, str]:
    """Execute the hook's main() capturing exit code and stdio."""
    monkeypatch.setenv("LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE", str(heartbeat_file))

    spec = importlib.util.spec_from_file_location("pre_tool_heartbeat", HOOK_PATH)
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
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        code, _, _ = _run_hook(monkeypatch, hb)
        assert code == 0

    def test_writes_heartbeat_on_success(self, monkeypatch, tmp_path):
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        _run_hook(monkeypatch, hb)
        assert hb.exists()
        ts = int(hb.read_text().strip())
        assert ts > 0

    def test_exits_zero_even_when_write_fails(self, monkeypatch, tmp_path):
        """Hook must never block tool execution even if write fails."""
        readonly_dir = tmp_path / "readonly_dir"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)  # read-only directory
        hb = readonly_dir / "dispatcher-pre-tool-heartbeat"

        try:
            code, _, _ = _run_hook(monkeypatch, hb)
            assert code == 0
        finally:
            readonly_dir.chmod(0o755)  # restore for cleanup

    def test_env_override_respected(self, monkeypatch, tmp_path):
        """LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE must be used when set."""
        custom = tmp_path / "custom-pre-tool-heartbeat"
        code, _, _ = _run_hook(monkeypatch, custom)
        assert code == 0
        assert custom.exists()

    def test_no_stdout_output(self, monkeypatch, tmp_path):
        """Hook must produce no stdout output."""
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        _, stdout, _ = _run_hook(monkeypatch, hb)
        assert stdout == ""

    def test_no_stderr_output(self, monkeypatch, tmp_path):
        """Hook must produce no stderr output."""
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        _, _, stderr = _run_hook(monkeypatch, hb)
        assert stderr == ""
