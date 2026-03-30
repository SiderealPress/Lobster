"""
Unit tests for hooks/session_role.py — state-file-based dispatcher detection.

Covers the new two-layer detection strategy introduced to replace JSONL
transcript scanning:

1. MCP state file (primary):  $LOBSTER_WORKSPACE/data/dispatcher-session-id
2. Hook marker file (secondary): ~/messages/config/dispatcher-session-id
3. Default: False (conservative/subagent)

Also covers:
- Fail-open: OSError on file read → True (dispatcher)
- _read_session_id_from_file() contract
- _check_state_file() contract
- _read_dispatcher_session_id() backwards-compat shim
- write_dispatcher_session_id() atomic write
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make hooks/ importable
_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import importlib
import session_role as _sr_module  # noqa: E402 — path insert must precede

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_session_role(monkeypatch, workspace: Path) -> object:
    """Reload session_role with LOBSTER_WORKSPACE pointing to workspace."""
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))
    # Force reimport so _get_mcp_session_state_file() picks up the new env.
    import importlib
    return importlib.reload(_sr_module)


def _hook_input(session_id: str) -> dict:
    return {"session_id": session_id}


# ---------------------------------------------------------------------------
# _read_session_id_from_file
# ---------------------------------------------------------------------------


class TestReadSessionIdFromFile:
    def test_absent_file_returns_none(self, tmp_path):
        path = tmp_path / "no-such-file"
        result = _sr_module._read_session_id_from_file(path)
        assert result is None

    def test_empty_file_returns_none(self, tmp_path):
        path = tmp_path / "empty"
        path.write_text("")
        result = _sr_module._read_session_id_from_file(path)
        assert result is None

    def test_whitespace_only_returns_none(self, tmp_path):
        path = tmp_path / "whitespace"
        path.write_text("   \n  ")
        result = _sr_module._read_session_id_from_file(path)
        assert result is None

    def test_valid_session_id_returned(self, tmp_path):
        path = tmp_path / "session-id"
        path.write_text("abc-123-def")
        result = _sr_module._read_session_id_from_file(path)
        assert result == "abc-123-def"

    def test_strips_trailing_newline(self, tmp_path):
        path = tmp_path / "session-id"
        path.write_text("abc-123\n")
        result = _sr_module._read_session_id_from_file(path)
        assert result == "abc-123"

    def test_os_error_returns_exception(self, tmp_path):
        path = tmp_path / "unreadable"
        path.write_text("something")
        path.chmod(0o000)
        try:
            result = _sr_module._read_session_id_from_file(path)
            assert isinstance(result, OSError)
        finally:
            path.chmod(0o644)


# ---------------------------------------------------------------------------
# _check_state_file
# ---------------------------------------------------------------------------


class TestCheckStateFile:
    def test_absent_file_returns_none(self, tmp_path):
        path = tmp_path / "no-such-file"
        result = _sr_module._check_state_file(path, "any-session")
        assert result is None

    def test_file_match_returns_true(self, tmp_path):
        path = tmp_path / "state"
        path.write_text("sess-001")
        result = _sr_module._check_state_file(path, "sess-001")
        assert result is True

    def test_file_mismatch_returns_false(self, tmp_path):
        path = tmp_path / "state"
        path.write_text("dispatcher-sess")
        result = _sr_module._check_state_file(path, "subagent-sess")
        assert result is False

    def test_none_session_id_returns_none(self, tmp_path):
        path = tmp_path / "state"
        path.write_text("dispatcher-sess")
        result = _sr_module._check_state_file(path, None)
        assert result is None

    def test_os_error_returns_true_fail_open(self, tmp_path):
        """If the file exists but is unreadable, fail open (return True)."""
        path = tmp_path / "unreadable"
        path.write_text("dispatcher-sess")
        path.chmod(0o000)
        try:
            result = _sr_module._check_state_file(path, "any-session")
            assert result is True
        finally:
            path.chmod(0o644)


# ---------------------------------------------------------------------------
# is_dispatcher — MCP state file (primary)
# ---------------------------------------------------------------------------


class TestIsDispatcherMCPStateFile:
    def test_mcp_file_match_returns_true(self, monkeypatch, tmp_path):
        """Primary MCP state file matches → dispatcher."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        mcp_dir = tmp_path / "data"
        mcp_dir.mkdir()
        (mcp_dir / "dispatcher-session-id").write_text("sess-mcp-001")

        assert sr.is_dispatcher(_hook_input("sess-mcp-001")) is True

    def test_mcp_file_mismatch_returns_false(self, monkeypatch, tmp_path):
        """Primary MCP state file contains a different session → subagent."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        mcp_dir = tmp_path / "data"
        mcp_dir.mkdir()
        (mcp_dir / "dispatcher-session-id").write_text("dispatcher-session")

        assert sr.is_dispatcher(_hook_input("subagent-session")) is False

    def test_mcp_file_absent_falls_through_to_secondary(self, monkeypatch, tmp_path):
        """Primary absent → falls through to hook marker file."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        # Set HOME so secondary hook marker file resolves under tmp_path.
        monkeypatch.setenv("HOME", str(tmp_path))
        # Reload to pick up new HOME for DISPATCHER_SESSION_FILE.
        sr = importlib.reload(sr)

        # Write only the secondary (hook marker) file.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text("sess-hook-001")

        assert sr.is_dispatcher(_hook_input("sess-hook-001")) is True

    def test_mcp_file_absent_no_secondary_returns_false(self, monkeypatch, tmp_path):
        """Both files absent → default False."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        assert sr.is_dispatcher(_hook_input("any-session")) is False


# ---------------------------------------------------------------------------
# is_dispatcher — hook marker file (secondary)
# ---------------------------------------------------------------------------


class TestIsDispatcherHookMarkerFile:
    def test_hook_file_match_when_mcp_absent(self, monkeypatch, tmp_path):
        """Secondary hook marker file used when primary MCP file is absent."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text("hook-dispatcher-sess")

        assert sr.is_dispatcher(_hook_input("hook-dispatcher-sess")) is True

    def test_hook_file_mismatch_when_mcp_absent(self, monkeypatch, tmp_path):
        """Hook file present but non-matching → subagent."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text("dispatcher-session")

        assert sr.is_dispatcher(_hook_input("subagent-session")) is False


# ---------------------------------------------------------------------------
# is_dispatcher — default (no files)
# ---------------------------------------------------------------------------


class TestIsDispatcherDefault:
    def test_no_files_returns_false(self, monkeypatch, tmp_path):
        """No state files present → default False (conservative)."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        assert sr.is_dispatcher(_hook_input("any-session")) is False

    def test_no_session_id_in_hook_input_returns_false(self, monkeypatch, tmp_path):
        """No session_id in hook input → both file checks return None → False."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        # Write both files to ensure it's the missing session_id causing the result.
        mcp_dir = tmp_path / "data"
        mcp_dir.mkdir()
        (mcp_dir / "dispatcher-session-id").write_text("some-dispatcher-sess")

        assert sr.is_dispatcher({}) is False  # no session_id key


# ---------------------------------------------------------------------------
# write_dispatcher_session_id
# ---------------------------------------------------------------------------


class TestWriteDispatcherSessionId:
    def test_writes_to_hook_marker_file(self, monkeypatch, tmp_path):
        """write_dispatcher_session_id writes the hook marker file."""
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr_module)

        sr.write_dispatcher_session_id("my-session-001")

        written = sr.DISPATCHER_SESSION_FILE.read_text().strip()
        assert written == "my-session-001"

    def test_strips_whitespace_on_write(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr_module)

        sr.write_dispatcher_session_id("  sess-with-spaces  ")

        written = sr.DISPATCHER_SESSION_FILE.read_text().strip()
        assert written == "sess-with-spaces"

    def test_silent_on_write_failure(self, monkeypatch, tmp_path):
        """Errors during write are silently swallowed."""
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr_module)

        # Point to an unwritable directory.
        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        unwritable.chmod(0o555)
        try:
            monkeypatch.setattr(sr, "DISPATCHER_SESSION_FILE",
                                unwritable / "dispatcher-session-id")
            # Should not raise.
            sr.write_dispatcher_session_id("sess-x")
        finally:
            unwritable.chmod(0o755)


# ---------------------------------------------------------------------------
# _read_dispatcher_session_id (backwards-compat shim)
# ---------------------------------------------------------------------------


class TestReadDispatcherSessionIdShim:
    def test_returns_none_when_file_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr_module)
        assert sr._read_dispatcher_session_id() is None

    def test_returns_session_id_when_file_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(_sr_module)

        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text("stored-sess-001")

        # Reload again to pick up new DISPATCHER_SESSION_FILE path.
        sr = importlib.reload(sr)
        assert sr._read_dispatcher_session_id() == "stored-sess-001"
