"""
Smoke tests — Groups C1, C2, C3, C4: hooks/require-write-result.py

C1. No reminder is emitted when write_result was called during the session.
C2. A reminder IS emitted when a subagent session had tool calls but no write_result.
C3. No reminder is emitted for dispatcher sessions (identified by wait_for_messages).
C4. session_end is attempted with task_id from write_result AND with session_id.

Failure modes:
  C1 — if the hook fires spuriously for sessions that already called write_result,
       subagents receive a duplicate STOP message after completing work correctly.
  C2 — if the hook stays silent when write_result was skipped, the dispatcher
       blocks waiting for a result that never arrives.
  C3 — if the hook fires for the dispatcher, the main loop receives a spurious
       reminder on every compaction cycle, breaking the dispatcher's control flow.
  C4 — if the hook only uses session_id (UUID) for session_end, the "proper"
       hex-agentId DB row stays stuck at status=running forever (COMPLETED_NOT_UPDATED).
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


def _build_transcript_with_write_result_input(task_id: str, chat_id: int = 12345) -> str:
    """Build a transcript where write_result was called with a specific task_id."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "mcp__lobster-inbox__write_result",
                    "id": "call_wr",
                    "input": {
                        "task_id": task_id,
                        "chat_id": chat_id,
                        "text": "done",
                    },
                }
            ],
        }
    ]
    return json.dumps({"session_id": "test-session-uuid-1234", "transcript": messages})


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
    """C1: Hook emits nothing when the transcript contains write_result with chat_id.

    Failure mode caught: if the hook incorrectly emits the STOP reminder for
    sessions that DID call write_result, well-behaved subagents would receive
    a spurious prompt telling them to call write_result again, potentially
    causing duplicate result messages or confused retries.

    Note: write_result must include a non-null chat_id. Without it the hook
    treats the call as invalid (the MCP server rejects write_result without
    chat_id) and still emits a reminder.
    """
    # Use _build_transcript_with_write_result_input so chat_id is present —
    # that is the only path where the hook allows silent exit.
    transcript = _build_transcript_with_write_result_input(
        task_id="test-task-id", chat_id=12345
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
    """C2: Hook hard-blocks (exit 2) when a subagent had tool calls but no write_result.

    Failure mode caught: a subagent that finishes work without calling
    write_result leaves the dispatcher blocked waiting for a result message
    that never arrives.  The hook exits 2 to hard-block the session from
    terminating, and prints a STOP message so Claude is forced to report back.

    CC SubagentStop hooks that exit non-zero prevent the agent session from
    ending — this is the intentional enforcement mechanism.
    """
    transcript = _build_transcript(
        "mcp__github__issue_read",
        "mcp__github__create_pull_request",
    )
    result = _run_hook(transcript)

    assert result.returncode == 2, (
        f"Hook must exit 2 (hard-block mode), got {result.returncode}. "
        "Exit 2 prevents the subagent session from terminating without calling write_result."
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
    """C2 (edge case): an empty transcript also hard-blocks (exit 2).

    A session with no tool calls at all is still a subagent that failed to
    report back; the hook should fire here too and exit 2 to hard-block.
    """
    result = _run_hook(json.dumps({"transcript": []}))

    assert result.returncode == 2, (
        f"Hook must exit 2 (hard-block) even when the transcript has no tool calls at all. "
        f"Got exit {result.returncode}."
    )
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


# ---------------------------------------------------------------------------
# C4 — _extract_write_result_task_ids returns task_id from write_result input
# ---------------------------------------------------------------------------


def test_extract_write_result_task_ids_found():
    """C4: Hook extracts task_id from write_result inputs for session_end calls.

    The hook calls session_end() with the task_id passed to write_result so
    it can close the dispatcher-registered hex-agentId DB row (which is keyed
    on task_id when the dispatcher set it in register_agent).

    Failure mode caught: if the hook only uses session_id (UUID), the proper
    hex-agentId DB row never gets updated and stays stuck at status=running,
    producing COMPLETED_NOT_UPDATED alerts forever.
    """
    # Import the helper directly to unit-test it without needing a DB.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "require_write_result",
        str(HOOK),
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[attr-defined]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    items = [
        {
            "type": "tool_use",
            "name": "mcp__lobster-inbox__write_result",
            "input": {"task_id": "my-task-id", "chat_id": 12345, "text": "done"},
        },
        {
            "type": "tool_use",
            "name": "some_other_tool",
            "input": {},
        },
    ]
    result = mod._extract_write_result_task_ids(items)
    assert result == ["my-task-id"], (
        f"Expected ['my-task-id'], got {result!r}. "
        "Hook must extract task_id from write_result input for session_end."
    )


def test_extract_write_result_task_ids_empty_when_no_task_id():
    """C4 (edge): returns empty list when write_result has no task_id input."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "require_write_result",
        str(HOOK),
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[attr-defined]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    items = [
        {
            "type": "tool_use",
            "name": "mcp__lobster-inbox__write_result",
            "input": {"chat_id": 12345, "text": "done"},  # no task_id
        },
    ]
    result = mod._extract_write_result_task_ids(items)
    assert result == [], (
        f"Expected [], got {result!r}. "
        "Should return empty list when task_id is absent from write_result input."
    )


def test_hook_exits_0_with_write_result_including_task_id():
    """C4 (integration): hook exits 0 when write_result is called with task_id + chat_id."""
    transcript_json = _build_transcript_with_write_result_input(
        task_id="my-descriptive-task-id"
    )
    result = _run_hook(transcript_json)

    assert result.returncode == 0, (
        f"Hook must exit 0 when write_result was called with task_id + chat_id. "
        f"Got exit {result.returncode}. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "", (
        f"Hook must produce no output when write_result was called. "
        f"Got: {result.stdout!r}"
    )
