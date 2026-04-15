"""
Unit tests for hooks/session_role.py — state-file-based dispatcher detection.

Covers the three-layer detection strategy:

1. Claude UUID state file (primary):  $LOBSTER_WORKSPACE/data/dispatcher-claude-session-id
   Written by MCP server when session_start(agent_type='dispatcher',
   claude_session_id=<uuid>) is called.  This UUID matches hook_input["session_id"].

2. MCP HTTP session state file (secondary):  $LOBSTER_WORKSPACE/data/dispatcher-session-id
   Written by _tag_dispatcher_session() with the HTTP transport session ID (32-char hex).
   Different format from Claude UUID — present file conclusively identifies subagents
   (mismatch), absent file falls through.

3. Hook marker file (tertiary): ~/messages/config/dispatcher-session-id
   Written by write-dispatcher-session-id.py SessionStart hook.

4. Default: False (conservative/subagent)

Also covers:
- Fail-open: OSError on file read → True (dispatcher)
- _read_session_id_from_file() contract
- _check_state_file() contract
- _read_dispatcher_session_id() backwards-compat shim
- write_dispatcher_session_id() atomic write
- The core bug fix: Claude UUID (hook_input) vs HTTP session ID mismatch
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make hooks/ importable
_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import session_role as _sr_module  # noqa: E402 — path insert must precede

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_session_role(monkeypatch, workspace: Path) -> object:
    """Reload session_role with LOBSTER_WORKSPACE pointing to workspace."""
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))
    # Force reimport so _get_mcp_session_state_file() picks up the new env.
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
# is_dispatcher — Claude UUID state file (PRIMARY, the bug fix)
# ---------------------------------------------------------------------------


class TestIsDispatcherClaudeUUIDFile:
    """Tests for the primary detection path: the Claude UUID state file.

    This is the fix for the session ID space mismatch bug (#1253).
    The hook receives a Claude UUID (hook_input["session_id"]) but the old
    primary check compared against the HTTP session ID — they never matched.
    The new primary file stores the Claude UUID and comparison works correctly.
    """

    def test_claude_uuid_match_returns_true(self, monkeypatch, tmp_path):
        """Primary: Claude UUID file matches → dispatcher detected."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        claude_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"
        (data_dir / "dispatcher-claude-session-id").write_text(claude_uuid)

        assert sr.is_dispatcher(_hook_input(claude_uuid)) is True

    def test_claude_uuid_mismatch_returns_false(self, monkeypatch, tmp_path):
        """Primary: Claude UUID file present, different session → subagent."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        dispatcher_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"
        subagent_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        (data_dir / "dispatcher-claude-session-id").write_text(dispatcher_uuid)

        assert sr.is_dispatcher(_hook_input(subagent_uuid)) is False

    def test_bug_1253_http_session_id_does_not_match_claude_uuid(
        self, monkeypatch, tmp_path
    ):
        """Regression test for the core bug: HTTP session ID vs Claude UUID mismatch.

        Before the fix, the MCP server wrote the HTTP session ID (32-char hex,
        e.g. '43e178fa975741eb9f6c1cb9f328d52b') to the state file, but the hook
        received the Claude UUID (e.g. '756633a5-4802-4327-ab98-684243d5fc2a').
        These IDs are different formats and NEVER match.

        After the fix: the HTTP session ID is still written to dispatcher-session-id
        (secondary) but the Claude UUID is written to dispatcher-claude-session-id
        (primary).  The hook compares the Claude UUID file first and gets a match.
        """
        sr = _reload_session_role(monkeypatch, tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Simulate the MCP server state: HTTP session ID in the old file.
        http_session_id = "43e178fa975741eb9f6c1cb9f328d52b"
        (data_dir / "dispatcher-session-id").write_text(http_session_id)

        # Simulate what the hook receives: a Claude UUID.
        claude_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"

        # Without the fix (only checking dispatcher-session-id): mismatch → subagent.
        # Verify the old file does NOT match the Claude UUID (confirming the bug was real).
        assert http_session_id != claude_uuid, "IDs should be different formats"

        # Now write the Claude UUID to the new primary file (what the fix does).
        (data_dir / "dispatcher-claude-session-id").write_text(claude_uuid)

        # With the fix: primary Claude UUID file matches → dispatcher correctly identified.
        assert sr.is_dispatcher(_hook_input(claude_uuid)) is True

    def test_claude_uuid_file_absent_with_only_http_file_returns_false(
        self, monkeypatch, tmp_path
    ):
        """Primary absent, only HTTP session file present → False (secondary skipped, tertiary absent)."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Only the HTTP session file is present (older MCP version or race window).
        # The secondary check is intentionally skipped (format mismatch — see
        # session_role.py), and the tertiary hook marker file is also absent,
        # so is_dispatcher() falls through to the default → False.
        http_session_id = "43e178fa975741eb9f6c1cb9f328d52b"
        claude_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"
        (data_dir / "dispatcher-session-id").write_text(http_session_id)

        assert sr.is_dispatcher(_hook_input(claude_uuid)) is False

    def test_claude_uuid_file_absent_falls_through_to_hook_marker(
        self, monkeypatch, tmp_path
    ):
        """When both MCP state files are absent, the hook marker file is used."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        # Only the tertiary hook marker file is present.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        claude_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"
        (config_dir / "dispatcher-session-id").write_text(claude_uuid)

        assert sr.is_dispatcher(_hook_input(claude_uuid)) is True


# ---------------------------------------------------------------------------
# is_dispatcher — MCP HTTP session state file (secondary, skipped)
#
# The secondary file (dispatcher-session-id) stores the HTTP transport session
# ID (32-char hex), NOT the Claude UUID. The secondary check is intentionally
# skipped in is_dispatcher() because it would always return False for both
# dispatcher and subagents (format mismatch), blocking the tertiary check.
# These tests verify that the secondary file alone never causes a True return,
# and that the function falls through correctly to the tertiary check.
# ---------------------------------------------------------------------------


class TestIsDispatcherMCPStateFile:
    def test_mcp_file_alone_never_returns_true(self, monkeypatch, tmp_path):
        """Secondary-only: MCP HTTP session file is skipped; returns False (no tertiary)."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)
        mcp_dir = tmp_path / "data"
        mcp_dir.mkdir()
        # Write the secondary file with a value that matches the hook input.
        # Under the old code this would return True; under the fix it is skipped
        # and the tertiary (hook marker) file is absent → False.
        (mcp_dir / "dispatcher-session-id").write_text("sess-mcp-001")

        assert sr.is_dispatcher(_hook_input("sess-mcp-001")) is False

    def test_mcp_file_present_falls_through_to_tertiary(self, monkeypatch, tmp_path):
        """Secondary file present but skipped → tertiary hook marker file decides."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)
        mcp_dir = tmp_path / "data"
        mcp_dir.mkdir()
        # Write secondary with a hex value (typical real-world content).
        http_session_id = "43e178fa975741eb9f6c1cb9f328d52b"
        (mcp_dir / "dispatcher-session-id").write_text(http_session_id)

        # Write tertiary with the matching Claude UUID.
        claude_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text(claude_uuid)

        # Secondary is skipped; tertiary matches → dispatcher.
        assert sr.is_dispatcher(_hook_input(claude_uuid)) is True

    def test_mcp_file_absent_falls_through_to_tertiary(self, monkeypatch, tmp_path):
        """Both MCP files absent → falls through to hook marker file."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        # Set HOME so tertiary hook marker file resolves under tmp_path.
        monkeypatch.setenv("HOME", str(tmp_path))
        # Reload to pick up new HOME for DISPATCHER_SESSION_FILE.
        sr = importlib.reload(sr)

        # Write only the tertiary (hook marker) file.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text("sess-hook-001")

        assert sr.is_dispatcher(_hook_input("sess-hook-001")) is True

    def test_mcp_file_absent_no_secondary_returns_false(self, monkeypatch, tmp_path):
        """All files absent → default False."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        assert sr.is_dispatcher(_hook_input("any-session")) is False


# ---------------------------------------------------------------------------
# is_dispatcher — hook marker file (tertiary)
# ---------------------------------------------------------------------------


class TestIsDispatcherHookMarkerFile:
    def test_hook_file_match_when_mcp_absent(self, monkeypatch, tmp_path):
        """Tertiary hook marker file used when both MCP files are absent."""
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
        """No session_id in hook input → all file checks return None → False."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        # Write a file to ensure it's the missing session_id causing the result.
        mcp_dir = tmp_path / "data"
        mcp_dir.mkdir()
        (mcp_dir / "dispatcher-claude-session-id").write_text("some-dispatcher-uuid")

        assert sr.is_dispatcher({}) is False  # no session_id key


# ---------------------------------------------------------------------------
# Regression: secondary check format mismatch blocked tertiary fallback
#
# Bug: when the secondary file (dispatcher-session-id) existed with a 32-char
# hex HTTP session ID, _check_state_file() returned False (mismatch).  The
# early-exit `if secondary_result is not None: return secondary_result` then
# returned False immediately, preventing the tertiary check from running.
# The tertiary file (~/messages/config/dispatcher-session-id) stores a Claude
# UUID and WOULD have returned True — but was never reached.
#
# Fix: remove the secondary early-exit entirely.  The secondary file stores an
# incompatible format and is not a reliable discriminator for either session
# type.  The function falls through to the tertiary check in all cases.
# ---------------------------------------------------------------------------


class TestIsDispatcherSecondaryCheckSkipped:
    def test_secondary_hex_does_not_block_tertiary_uuid_match(
        self, monkeypatch, tmp_path
    ):
        """Core regression: primary absent, secondary has hex, tertiary has UUID → True.

        This is the exact failure mode that triggered the bug report.  After
        compaction, the Claude UUID state file (primary) may be absent.  The
        secondary file exists with a 32-char hex HTTP session ID.  The tertiary
        hook marker file has the correct Claude UUID.

        Before the fix: secondary returned False (mismatch) → early exit →
        is_dispatcher() returned False for the dispatcher.

        After the fix: secondary is skipped → tertiary check runs → True.
        """
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        # Primary file absent (simulates post-compaction state).

        # Secondary: MCP HTTP session ID (32-char hex) — real content in production.
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        http_session_id = "43e178fa975741eb9f6c1cb9f328d52b"
        (data_dir / "dispatcher-session-id").write_text(http_session_id)

        # Tertiary: hook marker file with the correct Claude UUID.
        claude_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text(claude_uuid)

        # Must return True — the dispatcher is correctly identified via the
        # tertiary check, which is now reached because the secondary is skipped.
        assert sr.is_dispatcher(_hook_input(claude_uuid)) is True

    def test_secondary_hex_does_not_block_tertiary_uuid_mismatch(
        self, monkeypatch, tmp_path
    ):
        """Secondary skipped, tertiary has a different UUID → False (subagent)."""
        sr = _reload_session_role(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        sr = importlib.reload(sr)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        http_session_id = "43e178fa975741eb9f6c1cb9f328d52b"
        (data_dir / "dispatcher-session-id").write_text(http_session_id)

        # Tertiary has the dispatcher UUID.
        dispatcher_uuid = "756633a5-4802-4327-ab98-684243d5fc2a"
        subagent_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text(dispatcher_uuid)

        # Subagent session ID does not match tertiary → False.
        assert sr.is_dispatcher(_hook_input(subagent_uuid)) is False


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


# ---------------------------------------------------------------------------
# _get_mcp_claude_session_file — path resolution
# ---------------------------------------------------------------------------


class TestGetMCPClaudeSessionFile:
    def test_resolves_under_lobster_workspace(self, monkeypatch, tmp_path):
        """_get_mcp_claude_session_file() respects LOBSTER_WORKSPACE env var."""
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        sr = importlib.reload(_sr_module)
        expected = tmp_path / "data" / "dispatcher-claude-session-id"
        assert sr._get_mcp_claude_session_file() == expected

    def test_default_path_under_home(self, monkeypatch, tmp_path):
        """Without LOBSTER_WORKSPACE, defaults to ~/lobster-workspace/data/..."""
        monkeypatch.delenv("LOBSTER_WORKSPACE", raising=False)
        sr = importlib.reload(_sr_module)
        result = sr._get_mcp_claude_session_file()
        assert result.name == "dispatcher-claude-session-id"
        assert "lobster-workspace" in str(result)
        assert "data" in str(result)
