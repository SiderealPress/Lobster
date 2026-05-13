"""
Unit tests for hooks/thinking-heartbeat.py

Tests cover two concerns:

1. The sentinel design (issue #1483):
   - write_heartbeat() writes a Unix epoch integer to the heartbeat file
   - Atomic write: uses .tmp then rename, no .tmp left behind
   - Creates parent directory if absent
   - Overwrites existing content on each call
   - Timestamp is within a small window of time.time()
   - main() exits 0 on success
   - main() exits 0 even when write fails (silent failure — never block tool use)
   - LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE env var is respected

2. The dispatcher-only guard (issue #1897):
   - Subagent payloads carry an agent_id field; dispatcher payloads do not
   - When agent_id is present (non-empty), the heartbeat is NOT written
   - When agent_id is absent, the heartbeat IS written
   - When stdin is empty or invalid JSON, the heartbeat IS written (fail-open:
     preserves liveness signal; the dispatcher cannot set agent_id, so absence
     of parseable input is the same as absence of agent_id)

Guard design:
- The agent_id field is injected by Claude Code only into subagent hook payloads.
  The dispatcher session never has agent_id.
- This is the simplest possible guard: a single field check, no state file I/O,
  no process tree walk, no imported helpers.
- Named constant AGENT_ID_SUBAGENT_FIELD documents the field name.
"""

import importlib.util
import json
import os
import sys
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
# Named constant from spec — see issue #1897 and health-check-v3.sh.
EXPECTED_STALE_THRESHOLD = 1200  # 20 minutes — matches health-check-v3.sh

# Field name injected by Claude Code into subagent payloads (absent for dispatcher).
# Named constant from spec (issue #1897): this is the guard field.
AGENT_ID_SUBAGENT_FIELD = "agent_id"

# Representative values used in tests.
SAMPLE_SUBAGENT_AGENT_ID = "lobster-dispatcher"  # any non-empty string = subagent
SAMPLE_SUBAGENT_SESSION_ID = "subagent-session-aabbcc"
SAMPLE_DISPATCHER_SESSION_ID = "dispatcher-session-ddeeff"


# ---------------------------------------------------------------------------
# Module loader helpers
# ---------------------------------------------------------------------------

def _load_module(monkeypatch, heartbeat_file: Path):
    """Load thinking-heartbeat as a fresh module with heartbeat file override."""
    monkeypatch.setenv("LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE", str(heartbeat_file))
    spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hook(monkeypatch, heartbeat_file: Path) -> tuple[int, str, str]:
    """Execute the hook's main() with no stdin input (empty = no agent_id = dispatcher)."""
    monkeypatch.setenv("LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE", str(heartbeat_file))

    spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)

    stdout_cap = StringIO()
    stderr_cap = StringIO()
    exit_code = None

    with (
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch("sys.stdin", StringIO("")),
    ):
        try:
            spec.loader.exec_module(mod)
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _run_hook_with_input(
    monkeypatch,
    heartbeat_file: Path,
    hook_input: dict,
) -> tuple[int, str, str]:
    """Execute the hook's main() with the given JSON payload on stdin."""
    monkeypatch.setenv("LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE", str(heartbeat_file))

    spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)

    stdout_cap = StringIO()
    stderr_cap = StringIO()
    exit_code = None

    with (
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch("sys.stdin", StringIO(json.dumps(hook_input))),
    ):
        try:
            spec.loader.exec_module(mod)
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


# ---------------------------------------------------------------------------
# Pure function tests: write_heartbeat()
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


# ---------------------------------------------------------------------------
# Threshold constant guard (prevents silent drift)
# ---------------------------------------------------------------------------

class TestStaleThresholdConstant:
    """Verify the documented threshold matches the expected value (prevents silent drift)."""

    def test_stale_threshold_is_1200_seconds(self):
        spec = importlib.util.spec_from_file_location("th", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.DISPATCHER_HEARTBEAT_STALE_SECONDS == EXPECTED_STALE_THRESHOLD


# ---------------------------------------------------------------------------
# Dispatcher-only guard: agent_id field check (issue #1897)
# ---------------------------------------------------------------------------

# Named constant from spec: subagent tool calls must NOT update the heartbeat.
# The fix: check agent_id field — present = subagent, absent = dispatcher.
SUBAGENT_MUST_NOT_WRITE_HEARTBEAT = True


class TestAgentIdGuard:
    """Issue #1897: subagent tool calls must NOT update the dispatcher heartbeat.

    Agent_id guard: CC injects agent_id only into subagent hook payloads.
    The dispatcher session never carries agent_id. A single field check is
    sufficient — no state file I/O, no process tree walk.
    """

    def test_subagent_payload_does_not_write_heartbeat(self, monkeypatch, tmp_path):
        """When agent_id is present in the payload, heartbeat is NOT written."""
        hb = tmp_path / "dispatcher-heartbeat"
        code, _, _ = _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={
                AGENT_ID_SUBAGENT_FIELD: SAMPLE_SUBAGENT_AGENT_ID,
                "session_id": SAMPLE_SUBAGENT_SESSION_ID,
            },
        )
        assert code == 0
        assert not hb.exists(), (
            "Subagent tool calls (agent_id present) must not update the dispatcher "
            "heartbeat (issue #1897)"
        )

    def test_subagent_does_not_overwrite_existing_heartbeat(self, monkeypatch, tmp_path):
        """An existing heartbeat written by the dispatcher is not touched by subagent calls."""
        hb = tmp_path / "dispatcher-heartbeat"
        old_ts = 1700000000
        hb.write_text(str(old_ts) + "\n")

        code, _, _ = _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={
                AGENT_ID_SUBAGENT_FIELD: SAMPLE_SUBAGENT_AGENT_ID,
                "session_id": SAMPLE_SUBAGENT_SESSION_ID,
            },
        )
        assert code == 0
        assert int(hb.read_text().strip()) == old_ts, (
            "Subagent call must not overwrite existing dispatcher heartbeat (issue #1897)"
        )

    def test_dispatcher_payload_no_agent_id_writes_heartbeat(self, monkeypatch, tmp_path):
        """When agent_id is absent (dispatcher payload), the heartbeat IS written."""
        hb = tmp_path / "dispatcher-heartbeat"
        before = int(time.time())
        _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={"session_id": SAMPLE_DISPATCHER_SESSION_ID},
        )
        after = int(time.time())
        assert hb.exists()
        ts = int(hb.read_text().strip())
        assert before <= ts <= after + 1

    def test_empty_agent_id_string_writes_heartbeat(self, monkeypatch, tmp_path):
        """An empty string for agent_id is treated as absent (write heartbeat)."""
        hb = tmp_path / "dispatcher-heartbeat"
        _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={AGENT_ID_SUBAGENT_FIELD: "", "session_id": SAMPLE_DISPATCHER_SESSION_ID},
        )
        assert hb.exists(), (
            "Empty agent_id string must be treated as absent (dispatcher path)"
        )

    def test_null_agent_id_writes_heartbeat(self, monkeypatch, tmp_path):
        """A null/None value for agent_id is treated as absent (write heartbeat)."""
        hb = tmp_path / "dispatcher-heartbeat"
        _run_hook_with_input(
            monkeypatch,
            hb,
            hook_input={AGENT_ID_SUBAGENT_FIELD: None, "session_id": SAMPLE_DISPATCHER_SESSION_ID},
        )
        assert hb.exists(), (
            "Null agent_id must be treated as absent (dispatcher path)"
        )

    def test_empty_stdin_writes_heartbeat(self, monkeypatch, tmp_path):
        """When stdin is empty (no JSON), heartbeat IS written (fail-open).

        The dispatcher never sends agent_id, so absence of parseable stdin is
        equivalent to absence of agent_id. Fail open to preserve liveness signal.
        """
        hb = tmp_path / "dispatcher-heartbeat"
        code, _, _ = _run_hook(monkeypatch, hb)  # _run_hook uses empty stdin
        assert code == 0
        assert hb.exists(), (
            "Empty stdin must write the heartbeat (fail-open: dispatcher cannot set agent_id)"
        )

    def test_invalid_json_stdin_writes_heartbeat(self, monkeypatch, tmp_path):
        """When stdin contains invalid JSON, heartbeat IS written (fail-open)."""
        hb = tmp_path / "dispatcher-heartbeat"
        monkeypatch.setenv("LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE", str(hb))

        spec = importlib.util.spec_from_file_location("thinking_heartbeat", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        exit_code = None

        with patch("sys.stdin", StringIO("not valid json {")):
            try:
                spec.loader.exec_module(mod)
                mod.main()
            except SystemExit as e:
                exit_code = e.code

        assert exit_code == 0
        assert hb.exists(), (
            "Invalid JSON stdin must write the heartbeat (fail-open: cannot confirm subagent)"
        )

    def test_exits_zero_for_both_dispatcher_and_subagent(self, monkeypatch, tmp_path):
        """Hook always exits 0, whether dispatcher or subagent (never blocks tool execution)."""
        for payload in [
            {"session_id": SAMPLE_DISPATCHER_SESSION_ID},  # dispatcher
            {AGENT_ID_SUBAGENT_FIELD: SAMPLE_SUBAGENT_AGENT_ID, "session_id": SAMPLE_SUBAGENT_SESSION_ID},  # subagent
        ]:
            hb = tmp_path / f"heartbeat-{payload.get(AGENT_ID_SUBAGENT_FIELD, 'dispatcher')}"
            code, _, _ = _run_hook_with_input(monkeypatch, hb, hook_input=payload)
            assert code == 0, f"Hook must exit 0 for payload={payload!r}"

    def test_guard_uses_agent_id_not_session_role_import(self):
        """Guard must use agent_id field check, not session_role.is_dispatcher_session().

        The agent_id guard is self-contained: no state file I/O, no process tree,
        no is_dispatcher_session() which requires importing session_role and may
        fall through to process-tree walks with tmux timeouts.
        """
        source = HOOK_PATH.read_text()

        # The guard reads hook_input["agent_id"] (or .get("agent_id")).
        assert "agent_id" in source, (
            "thinking-heartbeat.py must reference 'agent_id' to implement the guard"
        )

        # Must NOT call is_dispatcher_session() in executable code — that would
        # re-introduce state file I/O and process-tree walks. Parse the AST to
        # check only actual function calls, not docstring references.
        import ast
        tree = ast.parse(source)
        dispatcher_session_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "is_dispatcher_session")
                or (isinstance(node.func, ast.Name) and node.func.id == "is_dispatcher_session")
            )
        ]
        assert not dispatcher_session_calls, (
            "thinking-heartbeat.py must NOT call is_dispatcher_session() — "
            "use the direct agent_id field check instead (simpler, no I/O, no process tree)"
        )

        # Must NOT import session_role — the guard is self-contained.
        session_role_imports = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            and any(alias.name == "session_role" for alias in node.names)
        ] + [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "session_role"
        ]
        assert not session_role_imports, (
            "thinking-heartbeat.py must NOT import session_role — "
            "the agent_id guard requires no external helpers"
        )


# ---------------------------------------------------------------------------
# Hook main() integration tests
# ---------------------------------------------------------------------------

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
