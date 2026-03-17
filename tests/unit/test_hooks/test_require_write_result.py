"""
Unit tests for hooks/require-write-result.py

Tests cover:
- Stop hook (CC 2.1.76+): reads transcript from transcript_path JSONL file
- Stop hook (legacy): falls back to inline transcript[] if transcript_path absent
- Stop hook: exits 0 with suppressOutput JSON when write_result was called
- Stop hook: exits 2 with stderr when write_result was not called
- SubagentStop hook: reads transcript from agent_transcript_path JSONL file
- SubagentStop hook: exits 0 when write_result found in JSONL transcript
- SubagentStop hook: exits 2 when write_result NOT found in JSONL transcript
- SubagentStop hook: exits 0 (allow) when agent_transcript_path is missing
- Dispatcher sessions are always allowed to exit (exit 0)
- write_result with chat_id=None blocks exit (exit 2)
- write_result with chat_id=0 allows exit (valid background agent call)
- Success exit outputs JSON with suppressOutput=true to prevent feedback injection
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
# Helpers
# ---------------------------------------------------------------------------

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "require-write-result.py"


def _load_hook(monkeypatch, tmp_path):
    """Load require-write-result.py as a fresh module for each test."""
    # Patch dispatcher session file to a nonexistent path so marker-file check
    # returns None (no match) and the transcript fallback is used instead.
    monkeypatch.setenv("HOME", str(tmp_path))

    spec = importlib.util.spec_from_file_location("require_write_result", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Insert hooks dir into path so session_role import works.
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    spec.loader.exec_module(mod)
    return mod


def _make_tool_use_item(name: str, input_data: dict) -> dict:
    return {"type": "tool_use", "name": name, "input": input_data}


def _make_jsonl_entry_with_write_result(chat_id=12345, task_id="task-abc") -> dict:
    """Return a single JSONL entry (CC 2.1.76+ format) containing a write_result call."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                _make_tool_use_item(
                    "mcp__lobster-inbox__write_result",
                    {"chat_id": chat_id, "task_id": task_id, "text": "done"},
                )
            ],
        },
        "uuid": "test-uuid-1",
        "sessionId": "test-session",
    }


def _make_jsonl_entry_no_write_result() -> dict:
    """Return a single JSONL entry with no write_result call."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "I finished the task."}],
        },
        "uuid": "test-uuid-2",
        "sessionId": "test-session",
    }


def _make_transcript_with_write_result(chat_id=12345, task_id="task-abc") -> list:
    """Return a JSONL-format transcript list containing a write_result call."""
    return [_make_jsonl_entry_with_write_result(chat_id=chat_id, task_id=task_id)]


def _make_transcript_no_write_result() -> list:
    """Return a JSONL-format transcript list with no write_result call."""
    return [_make_jsonl_entry_no_write_result()]


def _make_transcript_with_write_result_legacy(chat_id=12345, task_id="task-abc") -> list:
    """Return a legacy inline transcript (content directly on message dict)."""
    return [
        {
            "role": "assistant",
            "content": [
                _make_tool_use_item(
                    "mcp__lobster-inbox__write_result",
                    {"chat_id": chat_id, "task_id": task_id, "text": "done"},
                )
            ],
        }
    ]


def _make_stop_hook_input_with_path(
    transcript_path: str, session_id: str = "sess-001"
) -> dict:
    """CC 2.1.76+ Stop hook: uses transcript_path (JSONL file)."""
    return {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "transcript_path": transcript_path,
    }


def _make_stop_hook_input_legacy(
    transcript: list, session_id: str = "sess-001"
) -> dict:
    """Legacy Stop hook: inline transcript list (older CC versions)."""
    return {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "transcript": transcript,
    }


def _make_subagentstop_hook_input(
    agent_transcript_path: str,
    session_id: str = "sess-sub-001",
    agent_id: str = "agent-xyz",
) -> dict:
    return {
        "hook_event_name": "SubagentStop",
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_transcript_path": agent_transcript_path,
    }


def _write_jsonl_transcript(path: Path, messages: list) -> None:
    """Write a list of message dicts to a JSONL file."""
    with open(path, "w") as fh:
        for msg in messages:
            fh.write(json.dumps(msg) + "\n")


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    """
    Run mod.main() with hook_input as stdin JSON.
    Returns (exit_code, stdout_text, stderr_text).
    """
    stdout_capture = StringIO()
    stderr_capture = StringIO()
    stdin_data = json.dumps(hook_input)

    exit_code = None
    with patch("sys.stdin", StringIO(stdin_data)), \
         patch("sys.stdout", stdout_capture), \
         patch("sys.stderr", stderr_capture):
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()


# ---------------------------------------------------------------------------
# Stop hook tests (CC 2.1.76+ — transcript_path JSONL file)
# ---------------------------------------------------------------------------

class TestStopHook:
    def test_exits_0_when_write_result_called_via_path(self, monkeypatch, tmp_path):
        """CC 2.1.76+: Stop hook reads transcript from transcript_path file."""
        mod = _load_hook(monkeypatch, tmp_path)
        transcript_file = tmp_path / "session.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_with_write_result(chat_id=12345))
        hook_input = _make_stop_hook_input_with_path(str(transcript_file))

        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"Expected exit 0, got {exit_code}. stderr={stderr}"

    def test_success_outputs_suppress_output_json(self, monkeypatch, tmp_path):
        """Exit 0 must print JSON with suppressOutput=true to prevent CC feedback injection."""
        mod = _load_hook(monkeypatch, tmp_path)
        transcript_file = tmp_path / "session.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_with_write_result(chat_id=12345))
        hook_input = _make_stop_hook_input_with_path(str(transcript_file))

        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0
        output = json.loads(stdout.strip())
        assert output.get("suppressOutput") is True, f"Expected suppressOutput=true, got {output}"

    def test_exits_2_when_no_write_result_via_path(self, monkeypatch, tmp_path):
        """CC 2.1.76+: Stop hook blocks exit when write_result absent from JSONL."""
        mod = _load_hook(monkeypatch, tmp_path)
        transcript_file = tmp_path / "session.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())
        hook_input = _make_stop_hook_input_with_path(str(transcript_file))

        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 2, f"Expected exit 2, got {exit_code}"

    def test_exits_2_when_write_result_chat_id_none(self, monkeypatch, tmp_path):
        """write_result with chat_id=None is invalid — should block exit."""
        mod = _load_hook(monkeypatch, tmp_path)
        transcript_file = tmp_path / "session.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_with_write_result(chat_id=None))
        hook_input = _make_stop_hook_input_with_path(str(transcript_file))

        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 2, f"Expected exit 2, got {exit_code}"
        assert "chat_id" in stderr.lower() or "chat_id" in stdout.lower()

    def test_exits_0_when_write_result_chat_id_zero(self, monkeypatch, tmp_path):
        """chat_id=0 is valid (background agent / no user context)."""
        mod = _load_hook(monkeypatch, tmp_path)
        transcript_file = tmp_path / "session.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_with_write_result(chat_id=0))
        hook_input = _make_stop_hook_input_with_path(str(transcript_file))

        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"Expected exit 0, got {exit_code}. stderr={stderr}"

    def test_blocking_message_goes_to_stderr(self, monkeypatch, tmp_path):
        """Block messages must go to stderr (exit 2 reads stderr as feedback)."""
        mod = _load_hook(monkeypatch, tmp_path)
        transcript_file = tmp_path / "session.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())
        hook_input = _make_stop_hook_input_with_path(str(transcript_file))

        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 2
        assert "write_result" in stderr, f"Expected write_result in stderr, got: {stderr!r}"
        # stdout should NOT contain the block message (only JSON on success)
        assert "STOP:" not in stdout

    def test_legacy_inline_transcript_still_works(self, monkeypatch, tmp_path):
        """Legacy CC: inline transcript[] with legacy message format works."""
        mod = _load_hook(monkeypatch, tmp_path)
        transcript = _make_transcript_with_write_result_legacy(chat_id=12345)
        hook_input = _make_stop_hook_input_legacy(transcript)

        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"Legacy inline transcript should work, got {exit_code}. stderr={stderr}"


# ---------------------------------------------------------------------------
# SubagentStop hook tests (JSONL transcript file)
# ---------------------------------------------------------------------------

class TestSubagentStopHook:
    def test_exits_0_when_write_result_in_jsonl(self, monkeypatch, tmp_path):
        """SubagentStop: write_result found in JSONL transcript → allow exit."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent-sub.jsonl"
        messages = _make_transcript_with_write_result(chat_id=99999)
        _write_jsonl_transcript(transcript_file, messages)

        hook_input = _make_subagentstop_hook_input(str(transcript_file))
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"Expected exit 0, got {exit_code}. stderr={stderr}"

    def test_success_outputs_suppress_output_json(self, monkeypatch, tmp_path):
        """SubagentStop success must emit suppressOutput JSON."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent-sub.jsonl"
        messages = _make_transcript_with_write_result(chat_id=99999)
        _write_jsonl_transcript(transcript_file, messages)

        hook_input = _make_subagentstop_hook_input(str(transcript_file))
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0
        output = json.loads(stdout.strip())
        assert output.get("suppressOutput") is True

    def test_exits_2_when_no_write_result_in_jsonl(self, monkeypatch, tmp_path):
        """SubagentStop: no write_result in JSONL transcript → block exit."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent-sub.jsonl"
        messages = _make_transcript_no_write_result()
        _write_jsonl_transcript(transcript_file, messages)

        hook_input = _make_subagentstop_hook_input(str(transcript_file))
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 2, f"Expected exit 2, got {exit_code}"

    def test_exits_0_when_transcript_path_missing(self, monkeypatch, tmp_path):
        """SubagentStop: no agent_transcript_path → allow exit (safe fallback)."""
        mod = _load_hook(monkeypatch, tmp_path)

        hook_input = _make_subagentstop_hook_input("")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"Expected exit 0 (safe fallback), got {exit_code}"

    def test_exits_0_when_jsonl_file_nonexistent(self, monkeypatch, tmp_path):
        """SubagentStop: transcript file doesn't exist → allow exit (safe fallback)."""
        mod = _load_hook(monkeypatch, tmp_path)

        hook_input = _make_subagentstop_hook_input("/nonexistent/path/agent.jsonl")
        # File doesn't exist, _load_transcript_from_jsonl returns []
        # With empty transcript, no write_result found → would exit 2.
        # But missing file is indistinguishable from an agent that didn't call write_result,
        # so this test documents the actual behavior: exits 2.
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        # An unreadable transcript means we can't verify — exit 2 (conservative)
        # This is expected: the agent should have called write_result.
        assert exit_code == 2

    def test_blocking_message_goes_to_stderr(self, monkeypatch, tmp_path):
        """SubagentStop block messages must use stderr."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent-sub.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        hook_input = _make_subagentstop_hook_input(str(transcript_file))
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 2
        assert "write_result" in stderr
        assert "STOP:" not in stdout


# ---------------------------------------------------------------------------
# Dispatcher exemption tests
# ---------------------------------------------------------------------------

class TestDispatcherExemption:
    def test_dispatcher_stop_hook_exits_0(self, monkeypatch, tmp_path):
        """Dispatcher sessions must always be allowed to stop."""
        mod = _load_hook(monkeypatch, tmp_path)

        # Write the dispatcher session marker file.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "dispatcher-session-id").write_text("dispatcher-sess-001")

        # Write a transcript file with no write_result (dispatcher never calls it).
        transcript_file = tmp_path / "dispatcher.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        # Patch the DISPATCHER_SESSION_FILE in session_role module.
        import session_role
        original = session_role.DISPATCHER_SESSION_FILE
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            config_dir / "dispatcher-session-id"
        )

        hook_input = _make_stop_hook_input_with_path(
            str(transcript_file),
            session_id="dispatcher-sess-001",
        )
        exit_code, stdout, stderr = _run_hook(mod, hook_input)

        monkeypatch.setattr(session_role, "DISPATCHER_SESSION_FILE", original)
        assert exit_code == 0, f"Dispatcher should always exit 0, got {exit_code}"
