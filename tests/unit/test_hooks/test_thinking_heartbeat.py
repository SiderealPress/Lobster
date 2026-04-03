"""
Unit tests for hooks/thinking-heartbeat.py

Tests cover:
- Normal write: last_thinking_at written and ISO UTC formatted
- Merge semantics: existing fields in lobster-state.json are preserved
- Creates state file if absent (state dir exists)
- Atomic write: uses .tmp then rename
- Env override: LOBSTER_STATE_FILE_OVERRIDE is respected
- Malformed existing JSON is treated as empty (overwritten cleanly)
- Exceptions during write do not propagate (hook exits 0 silently)
- Hook exits 0 in all cases
"""

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "thinking-heartbeat.py"


# ---------------------------------------------------------------------------
# Module loader (fresh import each call to avoid state pollution)
# ---------------------------------------------------------------------------

def _load_module(monkeypatch, state_file: Path):
    """Load thinking-heartbeat as a fresh module with state file override."""
    monkeypatch.setenv("LOBSTER_STATE_FILE_OVERRIDE", str(state_file))
    spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Pure function tests (no subprocess)
# ---------------------------------------------------------------------------

class TestReadState:
    def test_returns_empty_dict_when_file_absent(self, tmp_path):
        mod = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("th", HOOK_PATH)
        )
        importlib.util.spec_from_file_location("th", HOOK_PATH).loader.exec_module(mod)
        result = mod._read_state(tmp_path / "nonexistent.json")
        assert result == {}

    def test_returns_parsed_dict(self, tmp_path):
        mod = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("th", HOOK_PATH)
        )
        importlib.util.spec_from_file_location("th", HOOK_PATH).loader.exec_module(mod)
        f = tmp_path / "state.json"
        f.write_text('{"mode": "active", "booted_at": "2024-01-01T00:00:00Z"}')
        result = mod._read_state(f)
        assert result == {"mode": "active", "booted_at": "2024-01-01T00:00:00Z"}

    def test_returns_empty_dict_on_malformed_json(self, tmp_path):
        mod = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("th", HOOK_PATH)
        )
        importlib.util.spec_from_file_location("th", HOOK_PATH).loader.exec_module(mod)
        f = tmp_path / "state.json"
        f.write_text("not valid json {{{")
        result = mod._read_state(f)
        assert result == {}


class TestWriteStateAtomic:
    def test_writes_json_to_path(self, tmp_path):
        mod = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("th", HOOK_PATH)
        )
        importlib.util.spec_from_file_location("th", HOOK_PATH).loader.exec_module(mod)
        target = tmp_path / "state.json"
        mod._write_state_atomic(target, {"foo": "bar"})
        assert target.exists()
        data = json.loads(target.read_text())
        assert data == {"foo": "bar"}

    def test_no_tmp_file_left_behind(self, tmp_path):
        mod = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("th", HOOK_PATH)
        )
        importlib.util.spec_from_file_location("th", HOOK_PATH).loader.exec_module(mod)
        target = tmp_path / "state.json"
        mod._write_state_atomic(target, {"x": 1})
        tmp = Path(str(target) + ".tmp")
        assert not tmp.exists()


class TestWriteThinkingHeartbeat:
    def _load(self):
        mod = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("th", HOOK_PATH)
        )
        importlib.util.spec_from_file_location("th", HOOK_PATH).loader.exec_module(mod)
        return mod

    def test_writes_last_thinking_at(self, tmp_path):
        mod = self._load()
        state_file = tmp_path / "lobster-state.json"
        mod.write_thinking_heartbeat(state_file)
        data = json.loads(state_file.read_text())
        assert "last_thinking_at" in data
        # Parseable ISO timestamp
        ts = datetime.fromisoformat(data["last_thinking_at"])
        assert ts.tzinfo is not None

    def test_preserves_existing_fields(self, tmp_path):
        mod = self._load()
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(json.dumps({
            "mode": "active",
            "booted_at": "2024-01-01T00:00:00Z",
            "last_processed_at": "2024-01-01T01:00:00Z",
        }))
        mod.write_thinking_heartbeat(state_file)
        data = json.loads(state_file.read_text())
        assert data["mode"] == "active"
        assert data["booted_at"] == "2024-01-01T00:00:00Z"
        assert data["last_processed_at"] == "2024-01-01T01:00:00Z"
        assert "last_thinking_at" in data

    def test_creates_file_when_absent(self, tmp_path):
        mod = self._load()
        state_file = tmp_path / "subdir" / "lobster-state.json"
        state_file.parent.mkdir(parents=True)
        mod.write_thinking_heartbeat(state_file)
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert "last_thinking_at" in data

    def test_overwrites_malformed_json_cleanly(self, tmp_path):
        mod = self._load()
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text("not json at all")
        mod.write_thinking_heartbeat(state_file)
        data = json.loads(state_file.read_text())
        assert "last_thinking_at" in data

    def test_timestamp_is_utc(self, tmp_path):
        mod = self._load()
        state_file = tmp_path / "lobster-state.json"
        mod.write_thinking_heartbeat(state_file)
        data = json.loads(state_file.read_text())
        ts = datetime.fromisoformat(data["last_thinking_at"])
        # UTC offset should be +00:00
        assert ts.utcoffset().total_seconds() == 0


# ---------------------------------------------------------------------------
# Hook main() integration tests
# ---------------------------------------------------------------------------

def _run_hook(monkeypatch, state_file: Path) -> tuple[int, str, str]:
    """Execute the hook's main() capturing exit code and stdio."""
    monkeypatch.setenv("LOBSTER_STATE_FILE_OVERRIDE", str(state_file))

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
        state_file = tmp_path / "lobster-state.json"
        code, _, _ = _run_hook(monkeypatch, state_file)
        assert code == 0

    def test_writes_heartbeat_on_success(self, monkeypatch, tmp_path):
        state_file = tmp_path / "lobster-state.json"
        _run_hook(monkeypatch, state_file)
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert "last_thinking_at" in data

    def test_exits_zero_even_when_write_fails(self, monkeypatch, tmp_path):
        # Point state file at a non-writable path to simulate write failure
        state_file = tmp_path / "readonly_dir" / "lobster-state.json"
        readonly_dir = tmp_path / "readonly_dir"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)  # read-only directory

        try:
            code, _, _ = _run_hook(monkeypatch, state_file)
            assert code == 0
        finally:
            readonly_dir.chmod(0o755)  # restore for cleanup
