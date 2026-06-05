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
  A7. Dispatcher: Agent with background: true in prompt frontmatter (schema workaround)
      → exit 0 (issue #1872: run_in_background stripped by additionalProperties: false)
  A8. Dispatcher: Agent with background: false in prompt frontmatter → exit 2
  A9. Dispatcher: Agent with background: true (Python-style) in frontmatter → exit 0

Failure modes by case:
  A1 — if the hook fires on correct usage, Claude is incorrectly warned away
       from the right pattern, likely causing it to switch to foreground mode.
  A2/A3 — if the hook stays silent, foreground Agent calls go unwarned and the
       dispatcher stalls silently.
  A4 — if the hook fires on non-Agent tools, unrelated tools generate spurious
       warnings on every call.
  A5 — if the hook blocks subagents, nested sync agents in engineering workflows fail.
  A6 — if "Task" is not treated like "Agent", older CC installs are unprotected.
  A7 — if the sentinel is not checked, dispatcher remains unable to spawn background
       subagents when Agent schema strips run_in_background (issue #1872).
  A8/A9 — normalization failures produce inconsistent behavior.
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
    simulate_dispatcher: bool = False,
) -> subprocess.CompletedProcess:
    """Run the hook with the given inputs.

    When simulate_dispatcher=True, writes the current process PID to the
    dispatcher startup flag file so is_dispatcher() returns True inside the
    hook subprocess. This replaces the old dispatcher_session_id marker file
    approach (issue #1908: simplified startup-flag detection).

    dispatcher_session_id is accepted for API compatibility but is no longer
    used by is_dispatcher() — pass simulate_dispatcher=True instead.
    """
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
        # Override HOME and LOBSTER_WORKSPACE so session_role reads from our
        # temp dir. LOBSTER_WORKSPACE may be set in the parent environment
        # (pointing at the real workspace), so it must be explicitly overridden
        # here to prevent the hook from looking at real workspace files.
        env["HOME"] = tmpdir
        workspace_dir = Path(tmpdir) / "lobster-workspace"
        env["LOBSTER_WORKSPACE"] = str(workspace_dir)

        if simulate_dispatcher:
            # Write the current process PID as the "dispatcher launcher PID".
            # The hook subprocess checks os.kill(pid, 0) — the test process
            # is alive throughout the subprocess run, so this always succeeds.
            flag_dir = workspace_dir / "data"
            flag_dir.mkdir(parents=True, exist_ok=True)
            (flag_dir / "dispatcher-startup-flag").write_text(str(os.getpid()))

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
        simulate_dispatcher=True,
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
        simulate_dispatcher=True,
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
        simulate_dispatcher=True,
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
            simulate_dispatcher=True,
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
        # No simulate_dispatcher — startup flag absent → is_dispatcher() → False
        session_id="subagent-sess-999",
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
        simulate_dispatcher=True,
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


# ---------------------------------------------------------------------------
# A7 — Dispatcher: background: true in prompt frontmatter → allowed
# ---------------------------------------------------------------------------
# Issue #1872: Agent schema has additionalProperties: false and no run_in_background
# field, so the client strips the parameter before the hook sees tool_input.
# Workaround: dispatcher includes `background: true` in the YAML frontmatter
# block of the prompt. The hook checks this as a secondary acceptance signal.
# ---------------------------------------------------------------------------

_FRONTMATTER_BACKGROUND_TRUE = """\
---
task_id: test-1872
chat_id: 12345
source: telegram
background: true
---

Build a flashcard deck."""

_FRONTMATTER_BACKGROUND_FALSE = """\
---
task_id: test-1872
chat_id: 12345
source: telegram
background: false
---

Build a flashcard deck."""

_FRONTMATTER_BACKGROUND_TRUE_PYTHON = """\
---
task_id: test-1872
chat_id: 12345
source: telegram
background: True
---

Build a flashcard deck (Python-style bool)."""


def test_dispatcher_frontmatter_background_true_exits_zero():
    """A7: Dispatcher with background: true in prompt frontmatter → exit 0.

    This is the primary fix for issue #1872. The Agent schema strips
    run_in_background before the hook runs; the frontmatter sentinel is the
    only reliable signal available to the hook.

    Failure mode: if the sentinel is not checked, dispatcher is permanently
    unable to spawn background subagents when the schema lacks run_in_background.
    """
    result = _run_hook(
        "Agent",
        {"prompt": _FRONTMATTER_BACKGROUND_TRUE},
        session_id="dispatcher-sess",
        dispatcher_session_id="dispatcher-sess",
    )

    assert result.returncode == 0, (
        f"Expected exit 0 for Agent with background: true in frontmatter, "
        f"got {result.returncode}.\nstderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "", (
        f"Expected no stdout for allowed call, got: {result.stdout!r}"
    )


def test_dispatcher_frontmatter_background_false_exits_two():
    """A8: Dispatcher with background: false in frontmatter → exit 2 (hard block).

    An explicit false means the dispatcher is not requesting background mode.
    Must be blocked regardless of the sentinel check path.

    Failure mode: if false is treated as acceptable, silent foreground calls slip through.
    """
    result = _run_hook(
        "Agent",
        {"prompt": _FRONTMATTER_BACKGROUND_FALSE},
        simulate_dispatcher=True,
    )

    assert result.returncode == 2, (
        f"Expected exit 2 for background: false in frontmatter, got {result.returncode}."
    )
    assert BLOCK_FRAGMENT in result.stderr, (
        f"Block message must appear on stderr.\nGot stderr: {result.stderr!r}"
    )


def test_dispatcher_frontmatter_background_true_python_case_exits_zero():
    """A9: background: True (Python-style) in frontmatter → exit 0.

    Claude frequently writes Python-style True/False in YAML-like blocks.
    The hook must accept both `true` (YAML) and `True` (Python).

    Failure mode: if only lowercase `true` is accepted, Claude's natural
    output style (Python True) causes every background call to be blocked.
    """
    result = _run_hook(
        "Agent",
        {"prompt": _FRONTMATTER_BACKGROUND_TRUE_PYTHON},
        session_id="dispatcher-sess",
        dispatcher_session_id="dispatcher-sess",
    )

    assert result.returncode == 0, (
        f"Expected exit 0 for background: True (Python-style), "
        f"got {result.returncode}.\nstderr: {result.stderr!r}"
    )
