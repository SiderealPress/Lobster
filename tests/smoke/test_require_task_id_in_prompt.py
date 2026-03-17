"""
Smoke tests for hooks/require-task-id-in-prompt.py

D1. Non-Agent tool calls pass through (exit 0, no output).
D2. Agent calls with "task_id is: <slug>" in the prompt pass through.
D3. Agent calls with "task_id" mentioned but without the "task_id is:" pattern are blocked (exit 2).
D4. Agent calls with no task_id mention are blocked (exit 2).
D5. Malformed JSON input exits 0 (pass-through, warning to stderr).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent.parent / "hooks" / "require-task-id-in-prompt.py"


def _make_input(tool_name: str, prompt: str) -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": {"prompt": prompt}})


def _run_hook(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_text,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# D1 — non-Agent tool calls are ignored
# ---------------------------------------------------------------------------


def test_non_agent_tool_passes_through():
    """D1: Hook exits 0 without output for non-Agent tool calls."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# D2 — Agent calls with "task_id is:" in the prompt pass through
# ---------------------------------------------------------------------------


def test_agent_with_task_id_is_pattern_passes():
    """D2: Hook exits 0 when prompt contains the canonical 'task_id is: <slug>' pattern."""
    prompt = "Your task_id is: fix-pr-485-review\n\nDo some work."
    payload = _make_input("Agent", prompt)
    result = _run_hook(payload)
    assert result.returncode == 0, (
        f"Expected exit 0 for prompt containing 'task_id is:', got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == ""


def test_agent_with_task_id_is_pattern_mid_prompt_passes():
    """D2 (variant): Pattern can appear anywhere in the prompt."""
    prompt = "Do some work.\n\nYour task_id is: some-slug\n\nMore instructions."
    payload = _make_input("Agent", prompt)
    result = _run_hook(payload)
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# D3 — "task_id" mentioned without assignment pattern is blocked
# ---------------------------------------------------------------------------


def test_agent_with_task_id_mention_only_is_blocked():
    """D3: Prompt discussing task_id but lacking 'task_id is:' is blocked (exit 2).

    This is the false-positive risk: a prompt like "explain what task_id does"
    previously passed because it contained the substring 'task_id'. Now only
    the assignment pattern 'task_id is:' is accepted.
    """
    prompt = "Explain what task_id does and why it matters to the system."
    payload = _make_input("Agent", prompt)
    result = _run_hook(payload)
    assert result.returncode == 2, (
        f"Expected exit 2 (blocked) for prompt with 'task_id' but not 'task_id is:', "
        f"got {result.returncode}."
    )
    assert result.stderr.strip(), "Hook must emit a message to stderr when blocking."


def test_agent_with_task_id_equals_pattern_is_blocked():
    """D3 (variant): 'task_id=foo' does not satisfy the 'task_id is:' requirement."""
    prompt = "task_id=my-task do the thing"
    payload = _make_input("Agent", prompt)
    result = _run_hook(payload)
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# D4 — Agent calls with no task_id at all are blocked
# ---------------------------------------------------------------------------


def test_agent_without_task_id_is_blocked():
    """D4: Agent prompt with no task_id mention is blocked (exit 2)."""
    prompt = "Please do some work without specifying any identifier."
    payload = _make_input("Agent", prompt)
    result = _run_hook(payload)
    assert result.returncode == 2, (
        f"Expected exit 2 (blocked), got {result.returncode}."
    )
    assert result.stderr.strip(), "Hook must emit a message to stderr when blocking."
    assert "task_id" in result.stderr, (
        f"Error message should mention task_id. Got: {result.stderr!r}"
    )


def test_agent_with_empty_prompt_is_blocked():
    """D4 (edge): Empty prompt is blocked."""
    payload = _make_input("Agent", "")
    result = _run_hook(payload)
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# D5 — malformed JSON input exits 0 (pass-through with warning)
# ---------------------------------------------------------------------------


def test_malformed_json_exits_0():
    """D5: Invalid JSON on stdin exits 0 (pass-through) and warns to stderr.

    The hook must not block on misconfigured input — an uncaught exception
    would cause every Agent spawn to fail with a confusing traceback instead
    of a useful error message.
    """
    result = _run_hook("this is not valid json {{{")
    assert result.returncode == 0, (
        f"Hook must exit 0 (pass-through) on malformed JSON, got {result.returncode}."
    )
    assert result.stderr.strip(), (
        "Hook should warn to stderr when it receives malformed input."
    )


def test_empty_stdin_exits_0():
    """D5 (edge): Empty stdin also exits 0 with a warning."""
    result = _run_hook("")
    assert result.returncode == 0
