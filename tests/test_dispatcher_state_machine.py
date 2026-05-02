"""
Tests for the 5-state dispatcher liveness machine (issue #1918).

Covers:
- state_machine.write_state(): writes correct JSON to dispatcher-state.json
- state_machine.write_state(): atomic write (uses .tmp then os.replace)
- state_machine.write_state(): silent on all errors (never raises)
- state_machine.read_state(): returns parsed dict when file is present
- state_machine.read_state(): returns None when file is absent or unreadable
- dispatcher-state-pretool.py: writes WAITING when tool is wait_for_messages
- dispatcher-state-pretool.py: writes PROCESSING when tool is mark_processing
- dispatcher-state-pretool.py: exits 0 silently for non-dispatcher sessions
- dispatcher-state-pretool.py: exits 0 for unrelated tool names (no write)
- dispatcher-state-pretool.py: exits 0 silently on malformed JSON input
- dispatcher-state-posttool.py: writes WAITING when tool is mark_processed
- dispatcher-state-posttool.py: exits 0 silently for non-dispatcher sessions
- dispatcher-state-posttool.py: exits 0 for unrelated tool names (no write)
- dispatcher-state-posttool.py: exits 0 silently on malformed JSON input
- dispatcher-state-stop.py: writes DEAD on stop for dispatcher sessions
- dispatcher-state-stop.py: exits 0 silently for non-dispatcher sessions
- inject-bootup-context.py: writes STARTING state when dispatcher is detected
- inject-bootup-context.py: does NOT write state for subagent sessions
"""

import importlib.util
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
SRC_DIR = REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state_machine(state_file_override: str | None = None):
    """Load src/state_machine.py with optional state file path override.

    Forces a fresh module load on every call so that STATE_FILE (set at module
    load time from the env var) reflects the override correctly.
    """
    sys.modules.pop("state_machine", None)
    env = {}
    if state_file_override:
        env["LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE"] = state_file_override
    else:
        env.pop("LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE", None)

    with patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location("state_machine", SRC_DIR / "state_machine.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    sys.modules.pop("state_machine", None)  # don't pollute cache
    return mod


def _load_hook(hook_name: str, state_file_override: str | None = None, extra_env: dict | None = None):
    """Load a hook file from HOOKS_DIR as a module.

    Forces a fresh load of both state_machine and the hook module on every call
    so that STATE_FILE (set at module load time from the env var) reflects the
    override correctly.
    """
    env: dict[str, str] = {}
    if state_file_override:
        env["LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE"] = state_file_override
    if extra_env:
        env.update(extra_env)

    # Ensure SRC_DIR and HOOKS_DIR are on sys.path for imports inside the hook
    for p in [str(HOOKS_DIR), str(SRC_DIR)]:
        if p not in sys.path:
            sys.path.insert(0, p)

    # Evict cached state_machine so a fresh copy is loaded with the new env var
    sys.modules.pop("state_machine", None)

    module_key = hook_name.replace("-", "_").replace(".", "_")
    sys.modules.pop(module_key, None)

    with patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location(
            module_key,
            HOOKS_DIR / hook_name,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    sys.modules.pop("state_machine", None)  # don't pollute cache
    return mod


def _raise_system_exit(code=0):
    """sys.exit replacement that actually raises SystemExit (like real sys.exit)."""
    raise SystemExit(code)


def _make_dispatcher_hook_input(tool_name: str = "", session_id: str = "test-session") -> str:
    """Build a JSON string for a dispatcher PreToolUse/PostToolUse hook input.

    No "agent_id" key → is_dispatcher_session() identifies this as dispatcher.
    """
    return json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
    })


def _make_subagent_hook_input(tool_name: str = "", session_id: str = "test-session") -> str:
    """Build a JSON string for a subagent hook input (has agent_id).

    Presence of "agent_id" → is_dispatcher_session() returns False.
    """
    return json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        "agent_id": "some-subagent-id",
    })


def _make_stop_hook_input(session_id: str = "test-session") -> str:
    """Build hook input for a SessionStop/Stop event."""
    return json.dumps({"session_id": session_id})


# ---------------------------------------------------------------------------
# state_machine tests
# ---------------------------------------------------------------------------

class TestWriteState:
    """state_machine.write_state() — writes correct, atomic JSON."""

    def test_writes_correct_state_field(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_state_machine(state_file)
        mod.write_state(mod.WAITING)
        data = json.loads(Path(state_file).read_text())
        assert data["state"] == "WAITING"

    def test_includes_pid(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_state_machine(state_file)
        mod.write_state(mod.PROCESSING)
        data = json.loads(Path(state_file).read_text())
        assert data["pid"] == os.getpid()

    def test_includes_session_id(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_state_machine(state_file)
        mod.write_state(mod.STARTING, session_id="abc-123")
        data = json.loads(Path(state_file).read_text())
        assert data["session_id"] == "abc-123"

    def test_includes_updated_at_iso(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_state_machine(state_file)
        mod.write_state(mod.DEAD)
        data = json.loads(Path(state_file).read_text())
        assert "updated_at" in data
        assert "T" in data["updated_at"]  # ISO 8601

    def test_atomic_no_leftover_tmp(self, tmp_path):
        """Temporary .json.tmp file must be gone after write."""
        state_file = tmp_path / "dispatcher-state.json"
        mod = _load_state_machine(str(state_file))
        mod.write_state(mod.WAITING)
        tmp_file = state_file.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "dispatcher-state.json"
        mod = _load_state_machine(str(nested))
        mod.write_state(mod.WAITING)
        assert nested.exists()

    def test_silent_on_unwritable_path(self):
        """write_state() must not raise even when the path is unwritable."""
        mod = _load_state_machine("/proc/nonexistent/dispatcher-state.json")
        # Should not raise:
        mod.write_state(mod.DEAD)

    def test_all_5_states_write(self, tmp_path):
        """All 5 valid state constants can be written and round-trip correctly."""
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_state_machine(state_file)
        for state in [mod.STARTING, mod.WAITING, mod.PROCESSING, mod.WINDING_DOWN, mod.DEAD]:
            mod.write_state(state)
            data = json.loads(Path(state_file).read_text())
            assert data["state"] == state


class TestReadState:
    """state_machine.read_state() — reads and parses the state file."""

    def test_returns_none_when_file_absent(self, tmp_path):
        state_file = str(tmp_path / "nonexistent.json")
        mod = _load_state_machine(state_file)
        assert mod.read_state() is None

    def test_returns_dict_when_file_present(self, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        state_file.write_text(json.dumps({"state": "WAITING", "pid": 99}))
        mod = _load_state_machine(str(state_file))
        result = mod.read_state()
        assert isinstance(result, dict)
        assert result["state"] == "WAITING"

    def test_returns_none_on_malformed_json(self, tmp_path):
        state_file = tmp_path / "dispatcher-state.json"
        state_file.write_text("not valid json{{{")
        mod = _load_state_machine(str(state_file))
        assert mod.read_state() is None


# ---------------------------------------------------------------------------
# dispatcher-state-pretool.py tests
# ---------------------------------------------------------------------------

class TestPreToolHook:
    """dispatcher-state-pretool.py — state transitions on PreToolUse."""

    WFM_TOOL = "mcp__lobster-inbox__wait_for_messages"
    MARK_PROC_TOOL = "mcp__lobster-inbox__mark_processing"

    def test_writes_waiting_on_wait_for_messages(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-pretool.py", state_file)
        hook_input = _make_dispatcher_hook_input(self.WFM_TOOL)
        with patch("sys.stdin", StringIO(hook_input)), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=True), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        data = json.loads(Path(state_file).read_text())
        assert data["state"] == "WAITING"

    def test_writes_processing_on_mark_processing(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-pretool.py", state_file)
        hook_input = _make_dispatcher_hook_input(self.MARK_PROC_TOOL)
        with patch("sys.stdin", StringIO(hook_input)), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=True), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        data = json.loads(Path(state_file).read_text())
        assert data["state"] == "PROCESSING"

    def test_no_write_for_unrelated_tool(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-pretool.py", state_file)
        hook_input = _make_dispatcher_hook_input("mcp__lobster-inbox__send_reply")
        with patch("sys.stdin", StringIO(hook_input)), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=True), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()

    def test_no_write_for_subagent_session(self, tmp_path):
        """is_dispatcher_session=False must suppress all state writes.

        The hook calls sys.exit(0) when not a dispatcher session; with
        sys.exit properly raising SystemExit the code never reaches write_state.
        """
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-pretool.py", state_file)
        hook_input = _make_dispatcher_hook_input(self.WFM_TOOL)
        with patch("sys.stdin", StringIO(hook_input)), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=False), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()

    def test_exits_silently_on_bad_json(self, tmp_path):
        """Malformed stdin triggers sys.exit(0) immediately — no state file created."""
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-pretool.py", state_file)
        with patch("sys.stdin", StringIO("{{invalid")), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()


# ---------------------------------------------------------------------------
# dispatcher-state-posttool.py tests
# ---------------------------------------------------------------------------

class TestPostToolHook:
    """dispatcher-state-posttool.py — state transitions on PostToolUse."""

    MARK_PROCESSED_TOOL = "mcp__lobster-inbox__mark_processed"

    def test_writes_waiting_on_mark_processed(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-posttool.py", state_file)
        hook_input = _make_dispatcher_hook_input(self.MARK_PROCESSED_TOOL)
        with patch("sys.stdin", StringIO(hook_input)), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=True), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        data = json.loads(Path(state_file).read_text())
        assert data["state"] == "WAITING"

    def test_no_write_for_unrelated_tool(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-posttool.py", state_file)
        hook_input = _make_dispatcher_hook_input("mcp__lobster-inbox__send_reply")
        with patch("sys.stdin", StringIO(hook_input)), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=True), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()

    def test_no_write_for_subagent_session(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-posttool.py", state_file)
        hook_input = _make_dispatcher_hook_input(self.MARK_PROCESSED_TOOL)
        with patch("sys.stdin", StringIO(hook_input)), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=False), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()

    def test_exits_silently_on_bad_json(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-posttool.py", state_file)
        with patch("sys.stdin", StringIO("{{invalid")), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()


# ---------------------------------------------------------------------------
# dispatcher-state-stop.py tests
# ---------------------------------------------------------------------------

class TestStopHook:
    """dispatcher-state-stop.py — writes DEAD on dispatcher session exit."""

    def test_writes_dead_on_dispatcher_stop(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-stop.py", state_file)
        hook_input = _make_stop_hook_input()
        with patch.object(mod.session_role, "is_dispatcher", return_value=True), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=True), \
             patch("sys.stdin", StringIO(hook_input)), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        data = json.loads(Path(state_file).read_text())
        assert data["state"] == "DEAD"

    def test_no_write_for_non_dispatcher_session(self, tmp_path):
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-stop.py", state_file)
        hook_input = _make_stop_hook_input()
        with patch.object(mod.session_role, "is_dispatcher", return_value=False), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=False), \
             patch("sys.stdin", StringIO(hook_input)), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()

    def test_writes_dead_even_with_bad_json(self, tmp_path):
        """On bad JSON, hook_input defaults to {} but still calls is_dispatcher()."""
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-stop.py", state_file)
        with patch.object(mod.session_role, "is_dispatcher", return_value=True), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=True), \
             patch("sys.stdin", StringIO("{{invalid")), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        data = json.loads(Path(state_file).read_text())
        assert data["state"] == "DEAD"

    def test_no_write_with_bad_json_and_non_dispatcher(self, tmp_path):
        """On bad JSON + non-dispatcher session, no state file is written."""
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = _load_hook("dispatcher-state-stop.py", state_file)
        with patch.object(mod.session_role, "is_dispatcher", return_value=False), \
             patch.object(mod.session_role, "is_dispatcher_session", return_value=False), \
             patch("sys.stdin", StringIO("{{invalid")), \
             patch("sys.exit", side_effect=_raise_system_exit):
            with pytest.raises(SystemExit):
                mod.main()
        assert not Path(state_file).exists()


# ---------------------------------------------------------------------------
# inject-bootup-context.py — STARTING state write
# ---------------------------------------------------------------------------

class TestInjectBootupStartingState:
    """inject-bootup-context.py writes STARTING state for dispatcher sessions.

    We load the hook with state_file_override and mock the dispatcher-detection
    functions so the test doesn't depend on the host filesystem.
    """

    def _load_inject_bootup(self, state_file_override: str):
        """Load inject-bootup-context.py with env overrides for isolation."""
        env = {"LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE": state_file_override}

        for p in [str(HOOKS_DIR), str(SRC_DIR)]:
            if p not in sys.path:
                sys.path.insert(0, p)

        sys.modules.pop("state_machine", None)
        sys.modules.pop("inject_bootup_context", None)

        with patch.dict(os.environ, env, clear=False):
            spec = importlib.util.spec_from_file_location(
                "inject_bootup_context",
                HOOKS_DIR / "inject-bootup-context.py",
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

        sys.modules.pop("state_machine", None)
        return mod

    def test_writes_starting_state_for_dispatcher(self, tmp_path):
        """When the session is identified as the dispatcher, STARTING is written."""
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = self._load_inject_bootup(state_file)

        hook_input = json.dumps({"session_id": "dispatcher-abc"})

        # Patch the dispatcher-detection chain to say "yes, dispatcher"
        with patch.object(mod, "_is_startup_flag_dispatcher", return_value=True), \
             patch.object(mod, "_consume_startup_flag"), \
             patch.object(mod, "_read_file_safe", return_value="# bootup content"), \
             patch.object(mod, "_inject_if_exists", return_value=False), \
             patch.object(mod, "_append_injection_log"), \
             patch("sys.stdin", StringIO(hook_input)), \
             patch("sys.exit", side_effect=_raise_system_exit), \
             patch("sys.stdout", StringIO()):
            with pytest.raises(SystemExit):
                mod.main()

        data = json.loads(Path(state_file).read_text())
        assert data["state"] == "STARTING"

    def test_does_not_write_state_for_subagent(self, tmp_path):
        """When the session is identified as a subagent, no state file is written."""
        state_file = str(tmp_path / "dispatcher-state.json")
        mod = self._load_inject_bootup(state_file)

        hook_input = json.dumps({"session_id": "subagent-xyz"})

        with patch.object(mod, "_is_startup_flag_dispatcher", return_value=False), \
             patch.object(mod, "_read_file_safe", return_value="# subagent bootup"), \
             patch.object(mod, "_inject_if_exists", return_value=False), \
             patch.object(mod, "_append_injection_log"), \
             patch("sys.stdin", StringIO(hook_input)), \
             patch("sys.exit", side_effect=_raise_system_exit), \
             patch("sys.stdout", StringIO()):
            with pytest.raises(SystemExit):
                mod.main()

        assert not Path(state_file).exists()
