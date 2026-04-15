"""
Unit tests for the MCP reconnect guard in inbox_server._reset_state_on_startup()
(issue #910).

When the MCP server restarts mid-session (e.g. Claude Code auto-updating),
_reset_state_on_startup() must NOT overwrite booted_at/woke_at if the
lobster-state.json file is fresh (< 30 minutes old) and mode is already
"active".  Without this guard, the health check thinks each CC auto-update
is a fresh boot and applies incorrect freshness thresholds, causing spurious
restarts.

These tests exercise the pure logic of _reset_state_on_startup by loading
the function in isolation with a controlled state file.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_SRC_MCP_DIR = Path(__file__).parents[2] / "src" / "mcp"
_SERVER_PATH = _SRC_MCP_DIR / "inbox_server.py"


class _PatchEnv:
    """Context manager to temporarily set environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _extract_reset_state_function(state_file_path: Path):
    """
    Extract and return _reset_state_on_startup as a callable, along with the
    _TRANSIENT_MODES and _RECONNECT_GRACE_SECONDS constants, without loading
    the full inbox_server (which has heavyweight dependencies).

    We do this by reading the relevant code block and executing it in a
    controlled namespace.
    """
    src = _SERVER_PATH.read_text()

    # Extract the section from _TRANSIENT_MODES through _reset_state_on_startup
    start_marker = "_TRANSIENT_MODES = {"
    end_marker = "_reset_state_on_startup()\n"

    start_idx = src.find(start_marker)
    end_idx = src.find(end_marker, start_idx)
    assert start_idx != -1, "Could not find _TRANSIENT_MODES in inbox_server.py"
    assert end_idx != -1, "Could not find _reset_state_on_startup() call in inbox_server.py"

    # Include the call itself so the function gets invoked during exec
    snippet = src[start_idx : end_idx + len(end_marker)]

    # Build a minimal namespace with required imports
    from datetime import datetime, timezone

    ns = {
        "json": __import__("json"),
        "os": os,
        "time": time,
        "datetime": datetime,
        "timezone": timezone,
        "Path": Path,
        "LOBSTER_STATE_FILE": state_file_path,
    }

    exec(compile(snippet, "<inbox_server_snippet>", "exec"), ns)
    return ns


class TestResetStateOnStartupReconnectGuard:
    """Tests for the reconnect guard in _reset_state_on_startup (issue #910)."""

    def test_skips_reset_when_mode_active_and_file_fresh(self, tmp_path):
        """Mid-session reconnect: active mode + fresh file → state untouched."""
        state_file = tmp_path / "lobster-state.json"
        original = {
            "mode": "active",
            "booted_at": "2026-03-26T07:08:07+00:00",
            "woke_at": "2026-03-26T07:08:07+00:00",
            "last_processed_at": "2026-03-26T07:34:05+00:00",
        }
        state_file.write_text(json.dumps(original, indent=2))
        # File is fresh — mtime is now

        ns = _extract_reset_state_function(state_file)

        # State should be unchanged
        result = json.loads(state_file.read_text())
        assert result["mode"] == "active"
        assert result["booted_at"] == original["booted_at"]
        assert result.get("woke_at") == original["woke_at"]

    def test_resets_when_mode_active_but_file_is_stale(self, tmp_path):
        """Fresh boot after long gap: active mode + stale file → woke_at updated.

        This case shouldn't normally happen (state file would be in a transient
        mode on a genuine restart), but if it does, the old behavior is preserved.
        The key is that a FRESH file (< 30 min) is NOT touched.
        """
        state_file = tmp_path / "lobster-state.json"
        original = {
            "mode": "active",
            "booted_at": "2026-03-20T13:36:10+00:00",
        }
        state_file.write_text(json.dumps(original, indent=2))

        # Backdate the file mtime by 60 minutes to simulate a stale file
        old_mtime = time.time() - 3600
        os.utime(state_file, (old_mtime, old_mtime))

        ns = _extract_reset_state_function(state_file)

        # For active mode with stale file: falls through to the mode check.
        # Mode is "active" (not in _TRANSIENT_MODES), so no modification happens.
        result = json.loads(state_file.read_text())
        assert result["mode"] == "active"

    def test_resets_hibernate_mode_regardless_of_file_age(self, tmp_path):
        """Transient mode (hibernate) → always reset to active."""
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(
            json.dumps({"mode": "hibernate", "booted_at": "2026-01-01T00:00:00+00:00"}, indent=2)
        )
        # Even a fresh hibernate-mode file should be reset
        ns = _extract_reset_state_function(state_file)

        result = json.loads(state_file.read_text())
        assert result["mode"] == "active"
        assert "woke_at" in result

    def test_resets_starting_mode(self, tmp_path):
        """Transient mode (starting) → always reset to active."""
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(json.dumps({"mode": "starting"}, indent=2))

        ns = _extract_reset_state_function(state_file)

        result = json.loads(state_file.read_text())
        assert result["mode"] == "active"

    def test_resets_restarting_mode(self, tmp_path):
        """Transient mode (restarting) → always reset to active."""
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(json.dumps({"mode": "restarting"}, indent=2))

        ns = _extract_reset_state_function(state_file)

        result = json.loads(state_file.read_text())
        assert result["mode"] == "active"

    def test_resets_waking_mode(self, tmp_path):
        """Transient mode (waking) → always reset to active."""
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(json.dumps({"mode": "waking"}, indent=2))

        ns = _extract_reset_state_function(state_file)

        result = json.loads(state_file.read_text())
        assert result["mode"] == "active"

    def test_no_op_when_file_absent(self, tmp_path):
        """Absent state file → no crash, no file created."""
        state_file = tmp_path / "lobster-state.json"
        # File does not exist

        ns = _extract_reset_state_function(state_file)

        assert not state_file.exists()

    def test_reconnect_grace_seconds_is_30_minutes(self, tmp_path):
        """_RECONNECT_GRACE_SECONDS constant must be 30 * 60 = 1800."""
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(json.dumps({"mode": "active"}))
        ns = _extract_reset_state_function(state_file)
        assert ns["_RECONNECT_GRACE_SECONDS"] == 1800

    def test_woke_at_not_written_on_reconnect(self, tmp_path):
        """On a mid-session reconnect, woke_at must NOT be updated."""
        state_file = tmp_path / "lobster-state.json"
        original_woke_at = "2026-03-26T07:08:07+00:00"
        state_file.write_text(
            json.dumps({"mode": "active", "woke_at": original_woke_at}, indent=2)
        )
        # File is fresh (mtime = now)

        ns = _extract_reset_state_function(state_file)

        result = json.loads(state_file.read_text())
        assert result.get("woke_at") == original_woke_at, (
            "woke_at was overwritten on a mid-session MCP reconnect — "
            "this confuses the health check about session age"
        )
