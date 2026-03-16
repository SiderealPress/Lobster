"""
Smoke tests for hooks/dispatcher-inline-tool-guard.py

The hook guards against the dispatcher making inline network calls (WebFetch,
WebSearch) that block the message-processing loop for the full duration of
the request.

Test cases:
  B1. WebFetch called inline   → exit 1, warning on stdout mentioning WebFetch
  B2. WebSearch called inline  → exit 1, warning on stdout mentioning WebSearch
  B3. Non-guarded tool call    → exit 0, no output

Failure modes by case:
  B1/B2 — if the hook stays silent, the dispatcher can freely make slow network
       calls that stall the message loop for 5–30+ seconds with no warning.
  B3 — if the hook fires on arbitrary tools, Claude receives spurious warnings
       on every tool call, degrading signal quality.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent.parent / "hooks" / "dispatcher-inline-tool-guard.py"


def _run_hook(tool_name: str, tool_input: dict | None = None) -> subprocess.CompletedProcess:
    """Run the hook with the given tool_name piped to stdin."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input or {}})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# B1 — WebFetch inline → soft warning
# ---------------------------------------------------------------------------


def test_webfetch_inline_warns():
    """B1: WebFetch called inline → exit 1 with warning on stdout mentioning WebFetch.

    Failure mode: a silent hook here means the dispatcher can issue multi-second
    network requests inline, stalling the message loop with no indication to Claude.
    """
    result = _run_hook("WebFetch", {"url": "https://example.com"})

    assert result.returncode == 1, (
        f"Expected exit 1 (soft warning) for inline WebFetch, got {result.returncode}."
    )
    assert "WebFetch" in result.stdout, (
        f"Warning must mention 'WebFetch'.\nGot stdout: {result.stdout!r}"
    )
    assert "background" in result.stdout.lower(), (
        f"Warning should suggest using a background subagent.\nGot stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# B2 — WebSearch inline → soft warning
# ---------------------------------------------------------------------------


def test_websearch_inline_warns():
    """B2: WebSearch called inline → exit 1 with warning on stdout mentioning WebSearch.

    Failure mode: same as B1, but for search queries that can also take several
    seconds and block the dispatcher loop.
    """
    result = _run_hook("WebSearch", {"query": "what is the weather"})

    assert result.returncode == 1, (
        f"Expected exit 1 (soft warning) for inline WebSearch, got {result.returncode}."
    )
    assert "WebSearch" in result.stdout, (
        f"Warning must mention 'WebSearch'.\nGot stdout: {result.stdout!r}"
    )
    assert "background" in result.stdout.lower(), (
        f"Warning should suggest using a background subagent.\nGot stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# B3 — Non-guarded tools → ignored entirely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", [
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Agent",
    "mcp__lobster-inbox__send_reply",
    "mcp__github__issue_write",
])
def test_non_guarded_tool_exits_zero(tool_name: str):
    """B3: Non-guarded tools are outside the hook's concern → exit 0, no output.

    Failure mode: if the hook fires on arbitrary tools, Claude receives spurious
    network-related warnings on every tool call.
    """
    result = _run_hook(tool_name, {"some_param": "value"})

    assert result.returncode == 0, (
        f"Expected exit 0 for non-guarded tool '{tool_name}', "
        f"got {result.returncode}.\nstdout: {result.stdout!r}"
    )
    assert result.stdout.strip() == "", (
        f"Expected no stdout for non-guarded tool '{tool_name}', "
        f"got: {result.stdout!r}"
    )
