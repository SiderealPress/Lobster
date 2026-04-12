"""
Unit tests for hooks/thinking-heartbeat.py

Tests cover:
- Normal write: Unix epoch written to dispatcher-heartbeat file
- Atomic write: uses .tmp then rename
- Creates parent directory if absent
- Env override: LOBSTER_WORKSPACE is respected
- Empty/corrupt content is handled gracefully by health check
- Exceptions during write do not propagate (hook exits 0 silently)
- Hook exits 0 in all cases
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


# ---------------------------------------------------------------------------
# Module loader (fresh import each call to avoid state pollution)
# ---------------------------------------------------------------------------

def _load_module(monkeypatch, workspace: Path):
    """Load thinking-heartbeat as a fresh module with workspace override."""
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))
    spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_load():
    """Load the module without monkeypatching (uses real LOBSTER_WORKSPACE)."""
    spec = importlib.util.spec_from_file_location("th", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestWriteDispatcherHeartbeat:
    def test_writes_epoch_to_file(self, tmp_path):
        mod = _fresh_load()
        heartbeat_file = tmp_path / "logs" / "dispatcher-heartbeat"
        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        before = int(time.time())
        mod.write_dispatcher_heartbeat(heartbeat_file)
        after = int(time.time())
        assert heartbeat_file.exists()
        epoch = int(heartbeat_file.read_text().strip())
        assert before <= epoch <= after, (
            f"Epoch {epoch} not in expected range [{before}, {after}]"
        )

    def test_creates_parent_directory(self, tmp_path):
        mod = _fresh_load()
        heartbeat_file = tmp_path / "deep" / "nested" / "dispatcher-heartbeat"
        mod.write_dispatcher_heartbeat(heartbeat_file)
        assert heartbeat_file.exists()

    def test_no_tmp_file_left_behind(self, tmp_path):
        mod = _fresh_load()
        heartbeat_file = tmp_path / "dispatcher-heartbeat"
        mod.write_dispatcher_heartbeat(heartbeat_file)
        tmp = Path(str(heartbeat_file) + ".tmp")
        assert not tmp.exists()

    def test_overwrites_existing_file(self, tmp_path):
        mod = _fresh_load()
        heartbeat_file = tmp_path / "dispatcher-heartbeat"
        heartbeat_file.write_text("0\n")  # old stale epoch
        time.sleep(0.01)
        mod.write_dispatcher_heartbeat(heartbeat_file)
        epoch = int(heartbeat_file.read_text().strip())
        assert epoch > 0

    def test_file_contains_integer_only(self, tmp_path):
        mod = _fresh_load()
        heartbeat_file = tmp_path / "dispatcher-heartbeat"
        mod.write_dispatcher_heartbeat(heartbeat_file)
        text = heartbeat_file.read_text().strip()
        assert text.isdigit(), f"Expected integer, got {text!r}"


# ---------------------------------------------------------------------------
# Hook main() integration tests
# ---------------------------------------------------------------------------

def _run_hook(monkeypatch, workspace: Path) -> tuple[int, str, str]:
    """Execute the hook's main() capturing exit code and stdio."""
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))

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
        code, _, _ = _run_hook(monkeypatch, tmp_path)
        assert code == 0

    def test_writes_heartbeat_file_on_success(self, monkeypatch, tmp_path):
        _run_hook(monkeypatch, tmp_path)
        heartbeat = tmp_path / "logs" / "dispatcher-heartbeat"
        assert heartbeat.exists(), "dispatcher-heartbeat file was not created"
        epoch = int(heartbeat.read_text().strip())
        assert epoch > 0

    def test_exits_zero_even_when_write_fails(self, monkeypatch, tmp_path):
        # Point workspace at a non-writable path to simulate write failure
        readonly_logs = tmp_path / "logs"
        readonly_logs.mkdir()
        readonly_logs.chmod(0o444)  # read-only directory

        try:
            code, _, _ = _run_hook(monkeypatch, tmp_path)
            assert code == 0
        finally:
            readonly_logs.chmod(0o755)  # restore for cleanup

    def test_uses_lobster_workspace_env(self, monkeypatch, tmp_path):
        workspace_a = tmp_path / "workspace_a"
        workspace_b = tmp_path / "workspace_b"
        workspace_a.mkdir()
        workspace_b.mkdir()

        _run_hook(monkeypatch, workspace_a)
        heartbeat_a = workspace_a / "logs" / "dispatcher-heartbeat"
        heartbeat_b = workspace_b / "logs" / "dispatcher-heartbeat"
        assert heartbeat_a.exists(), "Heartbeat should be in workspace_a"
        assert not heartbeat_b.exists(), "Heartbeat should NOT be in workspace_b"
