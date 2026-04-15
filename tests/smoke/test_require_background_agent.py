"""
Smoke tests for hooks/require-background-agent.py

The hook guards against the dispatcher being blocked by a foreground Agent call.
An Agent call without run_in_background: true stalls the message-processing loop
for the full duration of the agent — potentially minutes.

Test cases:
  A1. Dispatcher: Agent with run_in_background: true  → exit 0, no output
  A2. Dispatcher: Agent with run_in_background: false → exit 2, block message on stderr
  A3. Dispatcher: Agent with run_in_background absent → exit 2, block message on stderr
  A4. Non-Agent tool call                             → exit 0, no output
  A5. Subagent: Agent without run_in_background       → exit 0 (subagents are exempt)
  A6. Task tool name (old CC): dispatcher + sync      → exit 2 (treated same as Agent)

Failure modes by case:
  A1 — if the hook fires on correct usage, Claude is incorrectly warned away
       from the right pattern, likely causing it to switch to foreground mode.
  A2/A3 — if the hook stays silent, foreground Agent calls go unwarned and the
       dispatcher stalls silently.
  A4 — if the hook fires on non-Agent tools, unrelated tools generate spurious
       warnings on every call.
  A5 — if the hook blocks subagents, nested sync agents in engineering workflows fail.
  A6 — if "Task" is not treated like "Agent", older CC installs are unprotected.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent.parent / "hooks" / "require-background-agent.py"

BLOCK_FRAGMENT = "BLOCKED"


def _run_hook(
    tool_name: str,
    tool_input: dict,
    session_id: str = "sess-sub-001",
    dispatcher_session_id: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the hook with the given inputs, optionally setting the dispatcher marker file."""
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
    )

    env = os.environ.copy()

    with tempfile.TemporaryDirectory() as tmpdir:
        if dispatcher_session_id is not None:
            marker_dir = Path(tmpdir) / "messages" / "config"
            marker_dir.mkdir(parents=True)
            (marker_dir / "dispatcher-session-id").write_text(dispatcher_session_id)
        # Override HOME so session_role reads from our temp dir.
        env["HOME"] = tmpdir

        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
        )


# ---------------------------------------------------------------------------
# A1 — Dispatcher: Agent with run_in_background: true → allowed silently
# ---------------------------------------------------------------------------


def test_dispatcher_background_agent_exits_zero():
    """A1: Dispatcher calling Agent with run_in_background=True → exit 0.

    Failure mode: if the hook fires on correct usage, Claude is pushed toward
    foreground calls, which is the opposite of what we want.
    """
    result = _run_hook(
        "Agent",
        {"run_in_background": True, "prompt": "do stuff"},
        session_id="dispatcher-sess",
        dispatcher_session_id="dispatcher-sess",
    )

    assert result.returncode == 0, (
        f"Expected exit 0 for background Agent, got {result.returncode}.\n"
        f"stderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "", (
        f"Expected no stdout for background Agent, got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# A2 — Dispatcher: Agent with run_in_background: false → hard block
# ---------------------------------------------------------------------------


def test_dispatcher_foreground_agent_explicit_false_blocked():
    """A2: Dispatcher calling Agent with run_in_background=False → exit 2 with block on stderr.

    Failure mode: a silent hook here means Claude never sees that it is about
    to block the dispatcher's message-processing loop.
    """
    result = _run_hook(
        "Agent",
        {"run_in_background": False, "prompt": "do stuff"},
        session_id="dispatcher-sess",
        dispatcher_session_id="dispatcher-sess",
    )

    assert result.returncode == 2, (
        f"Expected exit 2 (hard block) for foreground Agent, got {result.returncode}."
    )
    assert BLOCK_FRAGMENT in result.stderr, (
        f"Block message must appear on stderr.\nGot stderr: {result.stderr!r}"
    )
    assert result.stdout == "", (
        f"Expected empty stdout on block, got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# A3 — Dispatcher: Agent with run_in_background absent → hard block
# ---------------------------------------------------------------------------


def test_dispatcher_foreground_agent_field_absent_blocked():
    """A3: Dispatcher calling Agent without run_in_background field → exit 2.

    Omitting the field is the most common mistake; it must be treated the same
    as run_in_background: false.

    Failure mode: same as A2 — silent hook means silent dispatcher stall.
    """
    result = _run_hook(
        "Agent",
        {"prompt": "do stuff"},
        session_id="dispatcher-sess",
        dispatcher_session_id="dispatcher-sess",
    )

    assert result.returncode == 2, (
        f"Expected exit 2 when run_in_background is absent, got {result.returncode}."
    )
    assert BLOCK_FRAGMENT in result.stderr, (
        f"Block message must appear on stderr.\nGot stderr: {result.stderr!r}"
    )
    assert result.stdout == "", (
        f"Expected empty stdout on block, got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# A4 — Non-Agent tool → ignored entirely
# ---------------------------------------------------------------------------


def test_non_agent_tool_exits_zero():
    """A4: Non-Agent tools are outside the hook's concern → exit 0, no output.

    Failure mode: if the hook fires on every tool, Claude receives spurious
    warnings for tools that have nothing to do with agents.
    """
    for tool_name in ["Bash", "Read", "Edit", "mcp__github__issue_write"]:
        result = _run_hook(
            tool_name,
            {"some_param": "value"},
            session_id="dispatcher-sess",
            dispatcher_session_id="dispatcher-sess",
        )

        assert result.returncode == 0, (
            f"Expected exit 0 for non-Agent tool '{tool_name}', "
            f"got {result.returncode}.\nstderr: {result.stderr!r}"
        )
        assert result.stdout.strip() == "", (
            f"Expected no stdout for non-Agent tool '{tool_name}', "
            f"got: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# A5 — Subagent calling Agent synchronously → allowed
# ---------------------------------------------------------------------------


def test_subagent_sync_agent_exits_zero():
    """A5: Subagents may call Agent synchronously — hook must not fire for them.

    Failure mode: blocking subagent nested sync calls breaks engineer workflows
    that need the nested result before proceeding.
    """
    result = _run_hook(
        "Agent",
        {"prompt": "do nested work"},
        # subagent session does NOT match dispatcher marker
        session_id="subagent-sess-999",
        dispatcher_session_id="dispatcher-sess-001",
    )

    assert result.returncode == 0, (
        f"Subagent should be allowed to call Agent synchronously, "
        f"got exit {result.returncode}.\nstderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# A6 — "Task" tool name (old CC) → treated same as "Agent"
# ---------------------------------------------------------------------------


def test_dispatcher_task_tool_sync_blocked():
    """A6: Older CC versions use "Task" instead of "Agent" — both must be blocked.

    Failure mode: if only "Agent" is checked, older installs are unprotected
    and the dispatcher can be stalled by a "Task" call.
    """
    result = _run_hook(
        "Task",
        {"prompt": "do stuff"},
        session_id="dispatcher-sess",
        dispatcher_session_id="dispatcher-sess",
    )

    assert result.returncode == 2, (
        f"Expected exit 2 for sync Task call from dispatcher, got {result.returncode}."
    )
    assert BLOCK_FRAGMENT in result.stderr, (
        f"Block message must appear on stderr.\nGot stderr: {result.stderr!r}"
    )
    assert result.stdout == "", (
        f"Expected empty stdout on block, got: {result.stdout!r}"
    )
