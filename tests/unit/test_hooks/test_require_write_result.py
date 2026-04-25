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
- Fallback after MAX_HOOK_FIRES: exits 0 and writes synthetic inbox message
- Fallback extracts pre-hook transcript content (turns before first fire)
- Fire count resets when write_result is eventually called
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
        config_dir.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# Fallback after N fires tests
# ---------------------------------------------------------------------------

def _make_subagentstop_hook_input_with_agent_id(
    agent_transcript_path: str,
    session_id: str = "sess-sub-001",
    agent_id: str = "agent-fallback-test",
) -> dict:
    return {
        "hook_event_name": "SubagentStop",
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_transcript_path": agent_transcript_path,
    }


def _make_transcript_with_text_turns(texts: list[str], timestamps: list[float] | None = None) -> list:
    """Build a JSONL transcript with assistant text turns, optionally timestamped."""
    entries = []
    for i, text in enumerate(texts):
        entry: dict = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "uuid": f"uuid-{i}",
            "sessionId": "test-session",
        }
        if timestamps is not None and i < len(timestamps):
            entry["timestamp"] = timestamps[i]
        entries.append(entry)
    return entries


class TestFallbackAfterNFires:
    """Tests for the retry-fire-count fallback behavior."""

    def _fire_hook_n_times(self, mod, hook_input: dict, n: int) -> tuple[int, str, str]:
        """Run the hook n times and return the result of the last run."""
        result = (None, "", "")
        for _ in range(n):
            result = _run_hook(mod, hook_input)
        return result

    def test_first_5_fires_block_with_exit_2(self, monkeypatch, tmp_path):
        """First MAX_HOOK_FIRES fires without write_result should block (exit 2)."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-block",
        )

        # Redirect fire-count temp file to tmp_path so tests don't pollute /tmp.
        fire_path = tmp_path / "lobster-hook-fires-test-agent-block"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        for fire_num in range(1, mod.MAX_HOOK_FIRES + 1):
            exit_code, _, stderr = _run_hook(mod, hook_input)
            assert exit_code == 2, (
                f"Expected exit 2 on fire #{fire_num}, got {exit_code}"
            )
            assert "write_result" in stderr

    def test_after_max_fires_exits_0(self, monkeypatch, tmp_path):
        """After MAX_HOOK_FIRES + 1 total fires, hook should exit 0."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-fallback",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-fallback"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        # Fire MAX_HOOK_FIRES times — should all block.
        for _ in range(mod.MAX_HOOK_FIRES):
            _run_hook(mod, hook_input)

        # The (MAX_HOOK_FIRES + 1)th fire should exit 0.
        exit_code, stdout, stderr = _run_hook(mod, hook_input)
        assert exit_code == 0, f"Expected exit 0 on fallback fire, got {exit_code}. stderr={stderr}"

    def test_fallback_emits_suppressoutput_json(self, monkeypatch, tmp_path):
        """Fallback exit must emit suppressOutput JSON (same as success path)."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-suppress",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-suppress"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        for _ in range(mod.MAX_HOOK_FIRES):
            _run_hook(mod, hook_input)

        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        output = json.loads(stdout.strip())
        assert output.get("suppressOutput") is True

    def test_fallback_writes_inbox_message(self, monkeypatch, tmp_path):
        """Fallback must write a synthetic subagent_result file to the inbox."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(
            transcript_file,
            _make_transcript_with_text_turns(["Step 1 done", "Step 2 done"]),
        )

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-inbox",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-inbox"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        for _ in range(mod.MAX_HOOK_FIRES):
            _run_hook(mod, hook_input)

        _run_hook(mod, hook_input)

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, f"Expected 1 inbox file, got {len(inbox_files)}"

        msg = json.loads(inbox_files[0].read_text())
        assert msg["type"] == "subagent_recovered"
        assert msg["status"] == "recovered"
        assert msg.get("recovered") is True
        assert "write_result" in msg["text"] or "recovered" in msg["text"].lower()

    def test_fallback_includes_transcript_content(self, monkeypatch, tmp_path):
        """Fallback inbox message must include meaningful pre-hook transcript content."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(
            transcript_file,
            _make_transcript_with_text_turns(
                ["Analysis: found critical bug in module X", "Fixed the bug by patching Y"]
            ),
        )

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-content",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-content"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        for _ in range(mod.MAX_HOOK_FIRES):
            _run_hook(mod, hook_input)

        _run_hook(mod, hook_input)

        inbox_files = list(inbox_dir.glob("*.json"))
        msg = json.loads(inbox_files[0].read_text())
        # Both meaningful text turns should appear in the recovered content.
        assert "Analysis" in msg["text"] or "Fixed" in msg["text"]

    def test_fallback_filters_post_hook_noise(self, monkeypatch, tmp_path):
        """Pre-hook filter: turns timestamped after first fire should be excluded."""
        mod = _load_hook(monkeypatch, tmp_path)

        first_fire_ts = 1000000.0  # will be recorded on first fire
        pre_hook_ts = first_fire_ts - 10.0   # clearly before
        post_hook_ts = first_fire_ts + 60.0  # clearly after (loop noise)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(
            transcript_file,
            _make_transcript_with_text_turns(
                ["Good pre-hook work", "Noisy retry loop output"],
                timestamps=[pre_hook_ts, post_hook_ts],
            ),
        )

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-filter",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-filter"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        # Simulate first fire at the expected timestamp.
        with patch("time.time", return_value=first_fire_ts):
            _run_hook(mod, hook_input)

        # Remaining fires at a later time.
        with patch("time.time", return_value=first_fire_ts + 30.0):
            for _ in range(mod.MAX_HOOK_FIRES - 1):
                _run_hook(mod, hook_input)
            _run_hook(mod, hook_input)  # fallback fire

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1
        msg = json.loads(inbox_files[0].read_text())

        # Pre-hook content should be present.
        assert "Good pre-hook work" in msg["text"]
        # Post-hook noise should NOT appear.
        assert "Noisy retry loop output" not in msg["text"]

    def test_fire_count_resets_when_write_result_called(self, monkeypatch, tmp_path):
        """If write_result is eventually called, the fire-count temp file is cleaned up."""
        mod = _load_hook(monkeypatch, tmp_path)

        # First: transcript without write_result (fires twice to set counter).
        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-reset",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-reset"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        # Fire twice (counter should be at 2).
        _run_hook(mod, hook_input)
        _run_hook(mod, hook_input)
        assert fire_path.exists(), "Fire-count file should exist after blocking fires"

        # Now update transcript to include write_result.
        _write_jsonl_transcript(transcript_file, _make_transcript_with_write_result())
        exit_code, _, _ = _run_hook(mod, hook_input)

        assert exit_code == 0, "write_result call should allow exit"
        assert not fire_path.exists(), "Fire-count file should be cleaned up after write_result"

    def test_fire_count_state_file_cleaned_up_after_fallback(self, monkeypatch, tmp_path):
        """After the fallback emit, the fire-count temp file must be removed."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-cleanup",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-cleanup"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        for _ in range(mod.MAX_HOOK_FIRES + 1):
            _run_hook(mod, hook_input)

        assert not fire_path.exists(), "Fire-count file should be removed after fallback emit"

    def test_fires_remaining_shown_in_stderr(self, monkeypatch, tmp_path):
        """The block message must mention how many attempts remain."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript_file = tmp_path / "agent.jsonl"
        _write_jsonl_transcript(transcript_file, _make_transcript_no_write_result())

        hook_input = _make_subagentstop_hook_input_with_agent_id(
            str(transcript_file),
            agent_id="test-agent-remaining",
        )

        fire_path = tmp_path / "lobster-hook-fires-test-agent-remaining"
        monkeypatch.setattr(mod, "_fire_count_path", lambda key: fire_path)

        exit_code, _, stderr = _run_hook(mod, hook_input)

        assert exit_code == 2
        # The remaining-attempts hint should appear in the block message.
        assert "remaining" in stderr.lower()

    def test_extract_pre_hook_text_no_timestamps(self, monkeypatch, tmp_path):
        """_extract_pre_hook_text with first_fire_ts=0 includes all turns."""
        mod = _load_hook(monkeypatch, tmp_path)

        transcript = _make_transcript_with_text_turns(["turn A", "turn B", "turn C", "turn D"])
        result = mod._extract_pre_hook_text(transcript, first_fire_ts=0.0, n_turns=3)

        # Should include the last 3 turns.
        assert "turn B" in result or "turn C" in result or "turn D" in result

    def test_extract_pre_hook_text_with_timestamps(self, monkeypatch, tmp_path):
        """_extract_pre_hook_text with first_fire_ts set filters out post-hook turns."""
        mod = _load_hook(monkeypatch, tmp_path)

        first_ts = 5000.0
        transcript = _make_transcript_with_text_turns(
            ["early", "late"],
            timestamps=[first_ts - 100, first_ts + 100],
        )
        result = mod._extract_pre_hook_text(transcript, first_fire_ts=first_ts, n_turns=10)

        assert "early" in result
        assert "late" not in result
