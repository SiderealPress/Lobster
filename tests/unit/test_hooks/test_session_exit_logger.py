"""
Tests for the session-exit-logger.py hook (Task 4).

Verifies that the hook:
  1. Logs a session_exit entry to observations.log on Stop and SubagentStop events
  2. Includes session_id, hook_event, exit_cause, message_count, is_dispatcher
  3. Tags exit_cause as "potential_overflow" when message_count > OVERFLOW_THRESHOLD
  4. Tags exit_cause as "write_result" when write_result was called
  5. Tags dispatcher exits as "end_turn" (dispatchers don't call write_result)
  6. Always exits 0 (never blocks the session)
  7. Emits suppressOutput=True to prevent CC feedback injection

Constants under test:
  OVERFLOW_THRESHOLD = 200
"""

import io
import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _run_hook(hook_input: dict, tmp_path: Path) -> tuple[int, str, dict | None]:
    """Run the session-exit-logger.py hook with the given input.

    Returns (exit_code, stdout, log_record_or_None).
    log_record_or_None is the parsed JSON from observations.log if it was written.
    """
    import subprocess
    import json

    hook_path = (
        Path(__file__).parent.parent.parent.parent / "hooks" / "session-exit-logger.py"
    )
    env = os.environ.copy()
    env["LOBSTER_WORKSPACE"] = str(tmp_path / "lobster-workspace")
    (tmp_path / "lobster-workspace" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "lobster-workspace" / "data").mkdir(parents=True, exist_ok=True)

    # Write empty dispatcher session files so is_dispatcher() returns False
    # for all test inputs unless we explicitly set them.
    (tmp_path / "lobster-workspace" / "data" / "dispatcher-claude-session-id").write_text("")

    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
    )

    obs_log = tmp_path / "lobster-workspace" / "logs" / "observations.log"
    log_record = None
    if obs_log.exists():
        lines = [l for l in obs_log.read_text().splitlines() if l.strip()]
        if lines:
            log_record = json.loads(lines[-1])

    return result.returncode, result.stdout, log_record


def _make_transcript_jsonl(tmp_path: Path, messages: list[dict]) -> Path:
    """Write a JSONL transcript file and return its path."""
    transcript_file = tmp_path / "transcript.jsonl"
    lines = [json.dumps(msg) for msg in messages]
    transcript_file.write_text("\n".join(lines) + "\n")
    return transcript_file


def _make_write_result_message():
    """Return a JSONL transcript entry that includes a write_result tool call."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "mcp__lobster-inbox__write_result",
                    "input": {"task_id": "t1", "chat_id": 12345, "text": "done"},
                }
            ],
        },
    }


class TestSessionExitLoggerBasic:
    """Hook writes a session_exit entry to observations.log on every exit event."""

    def test_stop_event_writes_log_entry(self, tmp_path):
        """Stop event writes a session_exit entry with required fields."""
        hook_input = {
            "hook_event_name": "Stop",
            "session_id": "test-session-uuid",
        }
        exit_code, stdout, record = _run_hook(hook_input, tmp_path)

        assert exit_code == 0, f"Hook must exit 0; got {exit_code}"
        assert record is not None, "No entry written to observations.log"
        assert record["category"] == "session_exit"
        assert record["session_id"] == "test-session-uuid"
        assert record["hook_event"] == "Stop"
        assert "exit_cause" in record
        assert "message_count" in record
        assert "is_dispatcher" in record
        assert "ts" in record

    def test_subagentstop_event_writes_log_entry(self, tmp_path):
        """SubagentStop event writes a session_exit entry."""
        hook_input = {
            "hook_event_name": "SubagentStop",
            "session_id": "subagent-uuid",
            "agent_id": "agent-hex-id",
        }
        exit_code, stdout, record = _run_hook(hook_input, tmp_path)

        assert exit_code == 0
        assert record is not None
        assert record["category"] == "session_exit"
        assert record["hook_event"] == "SubagentStop"

    def test_hook_emits_suppress_output(self, tmp_path):
        """Hook always emits suppressOutput=True on stdout."""
        hook_input = {"hook_event_name": "Stop", "session_id": "s1"}
        exit_code, stdout, _ = _run_hook(hook_input, tmp_path)

        assert exit_code == 0
        assert stdout.strip(), "Hook produced no stdout"
        parsed = json.loads(stdout.strip())
        assert parsed.get("suppressOutput") is True


class TestSessionExitCauseDetection:
    """exit_cause is derived correctly from transcript content and message count."""

    def test_write_result_call_sets_cause_write_result(self, tmp_path):
        """Subagent that called write_result gets exit_cause='write_result'."""
        transcript = _make_transcript_jsonl(tmp_path, [_make_write_result_message()])
        hook_input = {
            "hook_event_name": "SubagentStop",
            "session_id": "s1",
            "agent_transcript_path": str(transcript),
        }
        _, _, record = _run_hook(hook_input, tmp_path)
        assert record["exit_cause"] == "write_result"

    def test_high_message_count_tags_potential_overflow(self, tmp_path):
        """Session with > 200 messages and no write_result gets 'potential_overflow'."""
        # Create 201 simple transcript entries
        messages = [{"type": "user", "message": {"role": "user", "content": "msg"}}] * 201
        transcript = _make_transcript_jsonl(tmp_path, messages)
        hook_input = {
            "hook_event_name": "SubagentStop",
            "session_id": "s-overflow",
            "agent_transcript_path": str(transcript),
        }
        _, _, record = _run_hook(hook_input, tmp_path)
        assert record["exit_cause"] == "potential_overflow"
        assert record["message_count"] == 201

    def test_low_message_count_without_write_result_is_unknown(self, tmp_path):
        """Session with <= 200 messages and no write_result gets exit_cause='unknown'."""
        messages = [{"type": "user", "message": {"role": "user", "content": "msg"}}] * 10
        transcript = _make_transcript_jsonl(tmp_path, messages)
        hook_input = {
            "hook_event_name": "SubagentStop",
            "session_id": "s-short",
            "agent_transcript_path": str(transcript),
        }
        _, _, record = _run_hook(hook_input, tmp_path)
        assert record["exit_cause"] == "unknown"
        assert record["message_count"] == 10

    def test_no_transcript_path_message_count_is_zero(self, tmp_path):
        """When no transcript path is provided, message_count is 0."""
        hook_input = {"hook_event_name": "SubagentStop", "session_id": "s2"}
        _, _, record = _run_hook(hook_input, tmp_path)
        assert record["message_count"] == 0


class TestSessionExitOverflowThreshold:
    """OVERFLOW_THRESHOLD constant must be 200."""

    def test_overflow_threshold_is_200(self):
        """The overflow detection threshold is 200 messages."""
        hooks_dir = Path(__file__).parent.parent.parent.parent / "hooks"
        hook_path = hooks_dir / "session-exit-logger.py"
        source = hook_path.read_text()
        assert "OVERFLOW_THRESHOLD = 200" in source, (
            "session-exit-logger.py must define OVERFLOW_THRESHOLD = 200"
        )


class TestSessionExitLoggerRobustness:
    """Hook is resilient to malformed/missing input and always exits 0."""

    def test_unparseable_stdin_exits_0(self, tmp_path):
        """Unparseable JSON on stdin must not crash the hook (exits 0)."""
        import subprocess
        hook_path = (
            Path(__file__).parent.parent.parent.parent / "hooks" / "session-exit-logger.py"
        )
        env = os.environ.copy()
        env["LOBSTER_WORKSPACE"] = str(tmp_path / "lobster-workspace")
        (tmp_path / "lobster-workspace" / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "lobster-workspace" / "data").mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [sys.executable, str(hook_path)],
            input="NOT JSON",
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0

    def test_missing_transcript_path_does_not_crash(self, tmp_path):
        """Missing transcript file does not crash the hook."""
        hook_input = {
            "hook_event_name": "Stop",
            "session_id": "s3",
            "transcript_path": "/nonexistent/path/transcript.jsonl",
        }
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0
