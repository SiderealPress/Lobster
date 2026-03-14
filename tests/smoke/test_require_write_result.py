"""
Smoke tests — Groups C1, C2, C3: hooks/require-write-result.py

C1. No reminder is emitted when write_result was called during the session.
C2. A reminder IS emitted when a subagent session had tool calls but no write_result.
C3. No reminder is emitted for dispatcher sessions (identified by wait_for_messages).

Failure modes:
  C1 — if the hook fires spuriously for sessions that already called write_result,
       subagents receive a duplicate STOP message after completing work correctly.
  C2 — if the hook stays silent when write_result was skipped, the dispatcher
       blocks waiting for a result that never arrives.
  C3 — if the hook fires for the dispatcher, the main loop receives a spurious
       reminder on every compaction cycle, breaking the dispatcher's control flow.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Absolute path to the hook script.
HOOK = Path(__file__).parent.parent.parent / "hooks" / "require-write-result.py"


def _build_transcript(*tool_names: str) -> str:
    """Build a minimal hook transcript JSON containing the given tool calls."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": name, "id": f"call_{i}"}
                for i, name in enumerate(tool_names)
            ],
        }
    ]
    return json.dumps({"transcript": messages})


def _run_hook(transcript_json: str) -> subprocess.CompletedProcess:
    """Run the require-write-result hook with transcript piped to stdin."""
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=transcript_json,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# C1 — no reminder when write_result was called
# ---------------------------------------------------------------------------


def test_no_reminder_when_write_result_called():
    """C1: Hook emits nothing when the transcript contains write_result.

    Failure mode caught: if the hook incorrectly emits the STOP reminder for
    sessions that DID call write_result, well-behaved subagents would receive
    a spurious prompt telling them to call write_result again, potentially
    causing duplicate result messages or confused retries.
    """
    transcript = _build_transcript(
        "some_other_tool",
        "mcp__lobster-inbox__write_result",
    )
    result = _run_hook(transcript)

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"Expected empty stdout (no reminder), got: {result.stdout!r}\n"
        "Hook emitted a spurious reminder despite write_result being present."
    )


# ---------------------------------------------------------------------------
# C2 — reminder IS emitted when write_result was NOT called
# ---------------------------------------------------------------------------


def test_reminder_emitted_when_write_result_not_called():
    """C2: Hook emits a reminder when a subagent had tool calls but no write_result.

    Failure mode caught: a subagent that finishes work without calling
    write_result leaves the dispatcher blocked waiting for a result message
    that never arrives.  The hook injects a STOP prompt so Claude is forced
    to report back before the session ends.
    """
    transcript = _build_transcript(
        "mcp__github__issue_read",
        "mcp__github__create_pull_request",
    )
    result = _run_hook(transcript)

    assert result.returncode == 0, (
        f"Hook must exit 0 (inject mode), got {result.returncode}. "
        "A non-zero exit would block Claude Code from proceeding."
    )
    assert result.stdout.strip(), (
        "Hook must print a reminder to stdout when write_result was not called.\n"
        f"Got empty stdout. stderr={result.stderr!r}"
    )
    assert "write_result" in result.stdout, (
        "Reminder text must mention 'write_result' so the subagent knows "
        f"which tool to call. Got: {result.stdout!r}"
    )


def test_reminder_emitted_when_transcript_is_empty():
    """C2 (edge case): an empty transcript also counts as missing write_result.

    A session with no tool calls at all is still a subagent that failed to
    report back; the hook should fire here too.
    """
    result = _run_hook(json.dumps({"transcript": []}))

    assert result.returncode == 0
    assert result.stdout.strip(), (
        "Hook must emit a reminder even when the transcript has no tool calls at all."
    )


# ---------------------------------------------------------------------------
# C3 — dispatcher sessions are exempt from the enforcement
# ---------------------------------------------------------------------------


def test_dispatcher_skips_enforcement():
    """C3: Hook emits nothing when the transcript contains wait_for_messages.

    The dispatcher is identified by the presence of wait_for_messages in its
    tool calls.  The hook must never tell the dispatcher to call write_result —
    the dispatcher never does (it receives results, it does not produce them).

    Failure mode caught: if the hook fires for the dispatcher, every post-
    compact cycle would inject a spurious STOP message into the main loop,
    breaking the dispatcher's ability to resume normal message processing.
    """
    transcript = _build_transcript(
        "mcp__lobster-inbox__wait_for_messages",
        "mcp__lobster-inbox__send_reply",
        "mcp__lobster-inbox__mark_processed",
    )
    result = _run_hook(transcript)

    assert result.returncode == 0, (
        f"Hook must exit 0 for dispatcher sessions, got {result.returncode}."
    )
    assert result.stdout.strip() == "", (
        "Hook must produce no output for dispatcher sessions. "
        f"Got: {result.stdout!r}"
    )
