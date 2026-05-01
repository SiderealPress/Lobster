"""
Unit tests for hooks/pre-tool-heartbeat.py

Tests cover the PreToolUse heartbeat hook (issue #1786) and the
dispatcher-only guard (issue #1897):
- write_heartbeat() writes a Unix epoch integer to the heartbeat file
- Atomic write: uses .tmp then rename, no .tmp left behind
- Creates parent directory if absent
- Overwrites existing content on each call
- Timestamp is within a small window of time.time()
- main() exits 0 on success (dispatcher session)
- main() exits 0 without writing when called from a subagent session
- main() exits 0 even when write fails (silent failure — never block tool use)
- LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE env var is respected
- Written to a DIFFERENT file than the PostToolUse heartbeat
- Subagent tool calls do NOT update the pre-tool heartbeat (issue #1897)

The hook is intentionally symmetric with thinking-heartbeat.py (PostToolUse)
but writes to dispatcher-pre-tool-heartbeat instead of dispatcher-heartbeat.

Dispatcher-only guard (issue #1897):
- Hooks now read stdin and call session_role.is_dispatcher_session() before writing.
- Tests exercise: dispatcher writes, subagent skips, unknown-session skips.
- agent_id fast path: subagents always have agent_id in PreToolUse payloads;
  the hook exits 0 immediately without file I/O.
"""

import importlib.util
import json
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

# Fake session IDs used in tests.
DISPATCHER_SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SUBAGENT_SESSION_ID = "11111111-2222-3333-4444-555555555555"
SUBAGENT_AGENT_ID = "agent-xyz-123"


# ---------------------------------------------------------------------------
# Module loader helpers
# ---------------------------------------------------------------------------

def _load_raw():
    """Load module without any env override (uses default paths internally)."""
    spec = importlib.util.spec_from_file_location("pre_tool_heartbeat_raw", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hook_with_input(
    monkeypatch,
    heartbeat_file: Path,
    hook_input: dict,
    is_dispatcher_session_return: bool = True,
) -> tuple[int, str, str]:
    """Execute the hook's main() with a given hook input dict and mocked is_dispatcher_session.

    Mocks session_role.is_dispatcher_session to return is_dispatcher_session_return
    so tests don't depend on filesystem state (dispatcher-session-id files).
    """
    monkeypatch.setenv("LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE", str(heartbeat_file))

    stdin_content = json.dumps(hook_input)

    spec = importlib.util.spec_from_file_location("pre_tool_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)

    stdout_cap = StringIO()
    stderr_cap = StringIO()
    exit_code = None

    with (
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch("sys.stdin", StringIO(stdin_content)),
    ):
        try:
            spec.loader.exec_module(mod)
            # Patch is_dispatcher_session on the loaded module's session_role reference.
            mod.session_role.is_dispatcher_session = lambda _: is_dispatcher_session_return
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _run_hook(monkeypatch, heartbeat_file: Path) -> tuple[int, str, str]:
    """Run hook as the dispatcher (is_dispatcher_session returns True)."""
    return _run_hook_with_input(
        monkeypatch,
        heartbeat_file,
        hook_input={"session_id": DISPATCHER_SESSION_ID},
        is_dispatcher_session_return=True,
    )


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

class TestHookMain:
    def test_exits_zero_on_success(self, monkeypatch, tmp_path):
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        code, _, _ = _run_hook(monkeypatch, hb)
        assert code == 0

    def test_writes_heartbeat_when_dispatcher(self, monkeypatch, tmp_path):
        """Heartbeat is written when the session is the dispatcher."""
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


# ---------------------------------------------------------------------------
# Dispatcher-only guard: subagent calls must NOT update the heartbeat
# ---------------------------------------------------------------------------

# Named constant from the spec (issue #1897): subagent activity masks dispatcher death.
# The fix: heartbeat is only written when is_dispatcher_session() returns True.
SUBAGENT_MUST_NOT_WRITE_HEARTBEAT = True


class TestDispatcherOnlyGuard:
    """Issue #1897: subagent tool calls must NOT update the pre-tool dispatcher heartbeat."""

    def test_subagent_session_does_not_write_heartbeat(self, monkeypatch, tmp_path):
        """When is_dispatcher_session returns False, heartbeat file is not created."""
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        code, _, _ = _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={"session_id": SUBAGENT_SESSION_ID},
            is_dispatcher_session_return=False,
        )
        assert code == 0
        assert not hb.exists(), (
            "Subagent tool calls must not update the pre-tool heartbeat (issue #1897)"
        )

    def test_subagent_with_agent_id_does_not_write_heartbeat(self, monkeypatch, tmp_path):
        """Subagent payloads include agent_id — hook must skip without file I/O.

        The agent_id fast path in is_dispatcher_session() handles this, but
        we verify the end-to-end behavior: any PreToolUse payload with agent_id
        must not produce a heartbeat write.
        """
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        code, _, _ = _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={
                "session_id": SUBAGENT_SESSION_ID,
                "agent_id": SUBAGENT_AGENT_ID,
                "tool_name": "Bash",
            },
            is_dispatcher_session_return=False,
        )
        assert code == 0
        assert not hb.exists(), (
            "Subagent PreToolUse with agent_id must not write pre-tool heartbeat"
        )

    def test_subagent_does_not_overwrite_existing_heartbeat(self, monkeypatch, tmp_path):
        """An existing heartbeat written by the dispatcher is not touched by subagent calls."""
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        old_ts = 1700000000
        hb.write_text(str(old_ts) + "\n")

        code, _, _ = _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={"session_id": SUBAGENT_SESSION_ID},
            is_dispatcher_session_return=False,
        )
        assert code == 0
        # File content must be unchanged.
        assert int(hb.read_text().strip()) == old_ts, (
            "Subagent call must not overwrite existing pre-tool heartbeat"
        )

    def test_dispatcher_session_writes_heartbeat(self, monkeypatch, tmp_path):
        """When is_dispatcher_session returns True, the heartbeat IS written."""
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        before = int(time.time())
        _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={"session_id": DISPATCHER_SESSION_ID},
            is_dispatcher_session_return=True,
        )
        after = int(time.time())
        assert hb.exists()
        ts = int(hb.read_text().strip())
        assert before <= ts <= after + 1

    def test_empty_stdin_does_not_write_heartbeat(self, monkeypatch, tmp_path):
        """When stdin is empty (unparseable JSON), no heartbeat is written (conservative)."""
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        monkeypatch.setenv("LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE", str(hb))

        spec = importlib.util.spec_from_file_location("pre_tool_heartbeat", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        exit_code = None

        with patch("sys.stdin", StringIO("")):
            try:
                spec.loader.exec_module(mod)
                mod.main()
            except SystemExit as e:
                exit_code = e.code

        assert exit_code == 0
        assert not hb.exists(), (
            "Empty stdin must not write the heartbeat (conservative: unknown session = skip)"
        )

    def test_invalid_json_stdin_does_not_write_heartbeat(self, monkeypatch, tmp_path):
        """When stdin contains invalid JSON, no heartbeat is written."""
        hb = tmp_path / "dispatcher-pre-tool-heartbeat"
        monkeypatch.setenv("LOBSTER_PRE_TOOL_HEARTBEAT_OVERRIDE", str(hb))

        spec = importlib.util.spec_from_file_location("pre_tool_heartbeat", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        exit_code = None

        with patch("sys.stdin", StringIO("not valid json {")):
            try:
                spec.loader.exec_module(mod)
                mod.main()
            except SystemExit as e:
                exit_code = e.code

        assert exit_code == 0
        assert not hb.exists()

    def test_exits_zero_regardless_of_session_type(self, monkeypatch, tmp_path):
        """Hook always exits 0, whether dispatcher or subagent (never blocks tool execution)."""
        for is_disp in (True, False):
            hb_i = tmp_path / f"heartbeat-{is_disp}"
            code, _, _ = _run_hook_with_input(
                monkeypatch,
                hb_i,
                hook_input={"session_id": DISPATCHER_SESSION_ID},
                is_dispatcher_session_return=is_disp,
            )
            assert code == 0, f"Hook must exit 0 for is_dispatcher_session={is_disp}"
