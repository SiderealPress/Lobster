"""
Smoke tests for hooks/require-background-agent.py

The hook guards against the dispatcher being blocked by a foreground Agent call.
An Agent call without run_in_background: true stalls the message-processing loop
for the full duration of the agent — potentially minutes.

Test cases:
  A1. Agent with run_in_background: true  → exit 0, no output
  A2. Agent with run_in_background: false → exit 1, warning on stdout
  A3. Agent with run_in_background absent → exit 1, warning on stdout
  A4. Non-Agent tool call                 → exit 0, no output

Failure modes by case:
  A1 — if the hook fires on correct usage, Claude is incorrectly warned away
       from the right pattern, likely causing it to switch to foreground mode.
  A2/A3 — if the hook stays silent, foreground Agent calls go unwarned and the
       dispatcher stalls silently.
  A4 — if the hook fires on non-Agent tools, unrelated tools generate spurious
       warnings on every call.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent.parent / "hooks" / "require-background-agent.py"

WARNING_FRAGMENT = "run_in_background: true"


def _run_hook(tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
    """Run the hook with the given tool_name and tool_input piped to stdin."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# A1 — Agent with run_in_background: true → allowed silently
# ---------------------------------------------------------------------------


def test_background_agent_exits_zero():
    """A1: Agent called with run_in_background=True → exit 0, no warning output.

    Failure mode: if the hook warns on correct usage, Claude gets pushed toward
    foreground calls, which is the opposite of what we want.
    """
    result = _run_hook("Agent", {"run_in_background": True, "prompt": "do stuff"})

    assert result.returncode == 0, (
        f"Expected exit 0 for background Agent, got {result.returncode}.\n"
        f"stdout: {result.stdout!r}"
    )
    assert result.stdout.strip() == "", (
        f"Expected no stdout for background Agent, got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# A2 — Agent with run_in_background: false → soft warning
# ---------------------------------------------------------------------------


def test_foreground_agent_explicit_false_warns():
    """A2: Agent called with run_in_background=False → exit 1 with warning on stdout.

    Failure mode: a silent hook here means Claude never sees that it is about
    to block the dispatcher's message-processing loop.
    """
    result = _run_hook("Agent", {"run_in_background": False, "prompt": "do stuff"})

    assert result.returncode == 1, (
        f"Expected exit 1 (soft warning) for foreground Agent, got {result.returncode}."
    )
    assert WARNING_FRAGMENT in result.stdout, (
        f"Warning must mention '{WARNING_FRAGMENT}' so Claude knows what to fix.\n"
        f"Got stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# A3 — Agent with run_in_background absent → soft warning
# ---------------------------------------------------------------------------


def test_foreground_agent_field_absent_warns():
    """A3: Agent called without run_in_background field → exit 1 with warning on stdout.

    Omitting the field is the most common mistake; it must be treated the same
    as run_in_background: false.

    Failure mode: same as A2 — silent hook means silent dispatcher stall.
    """
    result = _run_hook("Agent", {"prompt": "do stuff"})

    assert result.returncode == 1, (
        f"Expected exit 1 when run_in_background is absent, got {result.returncode}."
    )
    assert WARNING_FRAGMENT in result.stdout, (
        f"Warning must mention '{WARNING_FRAGMENT}'.\nGot stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# A4 — Non-Agent tool → ignored entirely
# ---------------------------------------------------------------------------


def test_non_agent_tool_exits_zero():
    """A4: Non-Agent tools are outside the hook's concern → exit 0, no output.

    Failure mode: if the hook fires on every tool, Claude receives spurious
    run_in_background warnings for tools that have nothing to do with agents.
    """
    for tool_name in ["Bash", "Read", "Edit", "mcp__github__issue_write"]:
        result = _run_hook(tool_name, {"some_param": "value"})

        assert result.returncode == 0, (
            f"Expected exit 0 for non-Agent tool '{tool_name}', "
            f"got {result.returncode}.\nstdout: {result.stdout!r}"
        )
        assert result.stdout.strip() == "", (
            f"Expected no stdout for non-Agent tool '{tool_name}', "
            f"got: {result.stdout!r}"
        )
