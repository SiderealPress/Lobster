"""
Unit tests for hooks/wfm-lifecycle-logger.py

The hook writes WFM_ENTER (PreToolUse) and WFM_EXIT (PostToolUse) events to
session-lifecycle.log whenever wait_for_messages is called by the dispatcher.

Tests cover:
- WFM_ENTER is written on pre-tool-use for dispatcher sessions
- WFM_ENTER is NOT written for subagent sessions (dispatcher guard)
- WFM_EXIT is written with correct duration_s computation
- WFM_EXIT is written even when enter-ts state file is absent (no crash)
- WFM_EXIT includes message count when tool_response is a JSON list
- WFM_EXIT omits message count when tool_response is unparseable
- Log file is created if absent
- Each event is a valid JSON object followed by newline
- Unknown HOOK_TYPE produces no output and exits 0
- Exceptions during write do not propagate
- Enter-ts state file is cleaned up after WFM_EXIT

Named constants from spec (issue #1895):
  WFM_ENTER_EVENT = "WFM_ENTER"
  WFM_EXIT_EVENT  = "WFM_EXIT"
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
HOOK_PATH = _HOOKS_DIR / "wfm-lifecycle-logger.py"

# Named constants matching the spec
WFM_ENTER_EVENT = "WFM_ENTER"
WFM_EXIT_EVENT = "WFM_EXIT"
SESSION_LIFECYCLE_LOG_NAME = "session-lifecycle.log"
WFM_ENTER_TS_FILE_NAME = "wfm-enter-ts"


# ---------------------------------------------------------------------------
# Module loader helpers
# ---------------------------------------------------------------------------

def _load_module(monkeypatch, workspace: Path, hook_type: str):
    """Load wfm-lifecycle-logger as a fresh module with workspace + hook type overrides."""
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))
    monkeypatch.setenv("WFM_HOOK_TYPE", hook_type)
    # Ensure hooks dir is on sys.path so session_role can be imported
    if str(_HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOKS_DIR))
    spec = importlib.util.spec_from_file_location("wfm_lifecycle_logger", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_hook_input(session_id: str = "test-session-123", agent_id: str | None = None,
                     tool_response: str | None = None) -> dict:
    d: dict = {"session_id": session_id}
    if agent_id is not None:
        d["agent_id"] = agent_id
    if tool_response is not None:
        d["tool_response"] = tool_response
    return d


def _run_hook(mod, hook_input: dict) -> int:
    """Run mod.main() with hook_input on stdin. Returns exit code."""
    with patch.object(sys, "stdin", StringIO(json.dumps(hook_input))):
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
    return exc_info.value.code


def _read_log_events(log_file: Path) -> list[dict]:
    """Read all JSONL events from the lifecycle log."""
    events = []
    for line in log_file.read_text().splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    """Provide a temp workspace dir with logs/ created."""
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "logs").mkdir(exist_ok=True)
    return ws


# ---------------------------------------------------------------------------
# WFM_ENTER tests (PreToolUse path)
# ---------------------------------------------------------------------------

class TestWfmEnter:
    def test_wfm_enter_written_for_dispatcher(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "pre")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        exit_code = _run_hook(mod, _make_hook_input())
        assert exit_code == 0

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        assert log_file.exists(), "lifecycle log should be created"
        events = _read_log_events(log_file)
        assert len(events) == 1
        event = events[0]
        assert event["event"] == WFM_ENTER_EVENT
        assert event["session_id"] == "test-session-123"
        assert "ts" in event

    def test_wfm_enter_not_written_for_subagent(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "pre")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: False)

        exit_code = _run_hook(mod, _make_hook_input(agent_id="some-agent-id"))
        assert exit_code == 0

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        assert not log_file.exists(), "log should not be written for subagents"

    def test_wfm_enter_writes_enter_ts_file(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "pre")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        before = time.time()
        _run_hook(mod, _make_hook_input())
        after = time.time()

        ts_file = workspace / "logs" / WFM_ENTER_TS_FILE_NAME
        assert ts_file.exists(), "enter-ts state file should be written"
        recorded = float(ts_file.read_text().strip())
        assert before <= recorded <= after + 1

    def test_wfm_enter_is_valid_json_line(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "pre")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        _run_hook(mod, _make_hook_input())

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        raw = log_file.read_text()
        assert raw.endswith("\n"), "each event line must end with newline"
        json.loads(raw.strip())  # must be valid JSON

    def test_wfm_enter_appends_to_existing_log(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "pre")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        existing = json.dumps({"event": "CC_START", "ts": "2026-05-01T00:00:00Z"})
        log_file.write_text(existing + "\n")

        _run_hook(mod, _make_hook_input())

        events = _read_log_events(log_file)
        assert len(events) == 2
        assert events[0]["event"] == "CC_START"
        assert events[1]["event"] == WFM_ENTER_EVENT


# ---------------------------------------------------------------------------
# WFM_EXIT tests (PostToolUse path)
# ---------------------------------------------------------------------------

class TestWfmExit:
    def _write_enter_ts(self, workspace: Path, epoch: float):
        ts_file = workspace / "logs" / WFM_ENTER_TS_FILE_NAME
        ts_file.write_text(str(epoch))

    def test_wfm_exit_written_for_dispatcher(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        self._write_enter_ts(workspace, time.time() - 10.0)
        exit_code = _run_hook(mod, _make_hook_input())
        assert exit_code == 0

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        events = _read_log_events(log_file)
        assert len(events) == 1
        event = events[0]
        assert event["event"] == WFM_EXIT_EVENT
        assert "ts" in event
        assert "duration_s" in event

    def test_wfm_exit_duration_is_accurate(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        # Simulate ~5 second WFM block
        enter_time = time.time() - 5.0
        self._write_enter_ts(workspace, enter_time)
        _run_hook(mod, _make_hook_input())

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        event = _read_log_events(log_file)[0]
        # Duration should be approximately 5s — allow 1.5s slack for test execution
        assert 3.5 <= event["duration_s"] <= 6.5

    def test_wfm_exit_no_duration_when_enter_ts_absent(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        # No enter-ts file written — duration should be omitted, not crash
        exit_code = _run_hook(mod, _make_hook_input())
        assert exit_code == 0

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        events = _read_log_events(log_file)
        assert len(events) == 1
        assert events[0]["event"] == WFM_EXIT_EVENT
        assert "duration_s" not in events[0]

    def test_wfm_exit_includes_message_count_from_json_list(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        self._write_enter_ts(workspace, time.time() - 1.0)
        tool_response = json.dumps([{"id": "msg1"}, {"id": "msg2"}])
        exit_code = _run_hook(mod, _make_hook_input(tool_response=tool_response))
        assert exit_code == 0

        event = _read_log_events(workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME)[0]
        assert event["messages"] == 2

    def test_wfm_exit_omits_message_count_on_unparseable_response(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        self._write_enter_ts(workspace, time.time() - 1.0)
        exit_code = _run_hook(mod, _make_hook_input(tool_response="not json at all"))
        assert exit_code == 0

        event = _read_log_events(workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME)[0]
        assert "messages" not in event

    def test_wfm_exit_cleans_up_enter_ts_file(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        self._write_enter_ts(workspace, time.time() - 1.0)
        _run_hook(mod, _make_hook_input())

        ts_file = workspace / "logs" / WFM_ENTER_TS_FILE_NAME
        assert not ts_file.exists(), "enter-ts file should be removed after WFM_EXIT"

    def test_wfm_exit_not_written_for_subagent(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: False)

        self._write_enter_ts(workspace, time.time() - 1.0)
        _run_hook(mod, _make_hook_input())

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        assert not log_file.exists()

    def test_wfm_exit_includes_session_id(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        self._write_enter_ts(workspace, time.time() - 1.0)
        _run_hook(mod, _make_hook_input(session_id="abc-def-123"))

        event = _read_log_events(workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME)[0]
        assert event["session_id"] == "abc-def-123"


# ---------------------------------------------------------------------------
# Unknown HOOK_TYPE
# ---------------------------------------------------------------------------

class TestUnknownHookType:
    def test_unknown_hook_type_does_nothing(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "unknown_value")

        exit_code = _run_hook(mod, _make_hook_input())
        assert exit_code == 0

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        assert not log_file.exists()


# ---------------------------------------------------------------------------
# Resilience: exceptions must not propagate
# ---------------------------------------------------------------------------

class TestResilience:
    def test_pre_hook_does_not_raise_on_write_error(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "pre")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        # Simulate a write failure by making the log file unwritable
        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        log_file.write_text("")  # create it
        log_file.chmod(0o444)    # make read-only

        try:
            exit_code = _run_hook(mod, _make_hook_input())
            assert exit_code == 0
        finally:
            log_file.chmod(0o644)  # restore for cleanup

    def test_post_hook_does_not_raise_on_write_error(self, monkeypatch, workspace):
        mod = _load_module(monkeypatch, workspace, "post")
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _: True)

        log_file = workspace / "logs" / SESSION_LIFECYCLE_LOG_NAME
        log_file.write_text("")
        log_file.chmod(0o444)

        try:
            exit_code = _run_hook(mod, _make_hook_input())
            assert exit_code == 0
        finally:
            log_file.chmod(0o644)
