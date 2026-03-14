"""
Smoke tests — Group B: hooks/post-compact-gate.py

These tests verify the three critical paths through the gate hook:

B1. Gate denies non-wait_for_messages tool calls when the sentinel is fresh
    and LOBSTER_MAIN_SESSION=1. This is the core correctness property — if
    this fails, the gate is silently broken and the dispatcher can perform
    arbitrary actions immediately after compaction.

B2. Gate allows wait_for_messages and deletes the sentinel. If this path
    fails, the dispatcher is permanently deadlocked: the gate would deny
    every tool including the one needed to clear it.

B3. Gate passes all tool calls when the sentinel is stale (TTL fix, PR #237).
    Without the TTL, a crash or hibernation during the sentinel window would
    leave the system permanently blocked after restart.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Absolute path to the hook script.
HOOK = Path(__file__).parent.parent.parent / "hooks" / "post-compact-gate.py"


def _run_gate(tool_name: str, sentinel: Path, env_overrides: dict) -> subprocess.CompletedProcess:
    """Run the gate hook with the given tool name piped to stdin.

    Returns the completed process so callers can inspect stdout and returncode.
    """
    payload = json.dumps({"tool_name": tool_name})
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# B1 — gate denies non-WFM tool when sentinel is fresh
# ---------------------------------------------------------------------------


def test_gate_denies_non_wfm_tools_when_sentinel_fresh(tmp_path):
    """B1: A fresh sentinel causes the gate to deny non-wait_for_messages tools.

    Failure mode caught: gate is broken/bypassed and the dispatcher would run
    arbitrary tool calls immediately after context compaction instead of
    returning to the main loop.
    """
    sentinel = tmp_path / "compact-pending"
    sentinel.touch()

    result = _run_gate(
        tool_name="some_other_tool",
        sentinel=sentinel,
        env_overrides={
            "LOBSTER_MAIN_SESSION": "1",
            # Point the hook at our temp sentinel via a monkeypatch approach:
            # the hook uses the hardcoded ~/messages/config path, so we
            # override HOME to redirect it.
            "HOME": str(tmp_path),
        },
    )

    # The gate must not crash.
    assert result.returncode == 0

    # The hook writes a JSON object to stdout containing permissionDecision: deny.
    assert result.stdout.strip(), "Expected non-empty stdout with deny decision"
    output = json.loads(result.stdout)
    decision = (
        output.get("hookSpecificOutput", {}).get("permissionDecision", "")
    )
    assert decision == "deny", (
        f"Expected permissionDecision=deny, got: {decision!r}\nFull output: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# B2 — gate allows wait_for_messages and removes the sentinel
# ---------------------------------------------------------------------------


def test_gate_allows_wait_for_messages_and_deletes_sentinel(tmp_path):
    """B2: wait_for_messages passes through AND the sentinel file is deleted.

    Failure mode caught: if wait_for_messages were denied or the sentinel
    were not deleted, the dispatcher would be permanently deadlocked — every
    tool call denied, and the only escape hatch also blocked.
    """
    # Set up the sentinel at the path the hook will look for.
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True)
    sentinel = config_dir / "compact-pending"
    sentinel.touch()
    assert sentinel.exists()

    result = _run_gate(
        tool_name="mcp__lobster-inbox__wait_for_messages",
        sentinel=sentinel,
        env_overrides={
            "LOBSTER_MAIN_SESSION": "1",
            "HOME": str(tmp_path),
        },
    )

    # Hook must exit 0 and produce no deny output.
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"Expected empty stdout (pass-through), got: {result.stdout!r}"
    )

    # The sentinel must have been deleted.
    assert not sentinel.exists(), (
        "Sentinel file was NOT deleted after wait_for_messages — "
        "dispatcher would call wait_for_messages again next compaction and "
        "the sentinel would still be there, causing a spurious deny."
    )


# ---------------------------------------------------------------------------
# B3 — gate passes all tools when sentinel is stale (TTL fix, PR #237)
# ---------------------------------------------------------------------------


def test_gate_passes_when_sentinel_is_stale(tmp_path):
    """B3: A sentinel older than SENTINEL_TTL_SECONDS (600 s) is ignored.

    Failure mode caught: without the TTL, a crash or hibernation while the
    sentinel was active would permanently block the dispatcher on next boot.
    Any tool call — including innocuous ones — would be denied forever.
    """
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True)
    sentinel = config_dir / "compact-pending"
    sentinel.touch()

    # Back-date the sentinel by 700 seconds (beyond the 600 s TTL).
    stale_mtime = time.time() - 700
    os.utime(sentinel, (stale_mtime, stale_mtime))

    result = _run_gate(
        tool_name="some_other_tool",
        sentinel=sentinel,
        env_overrides={
            "LOBSTER_MAIN_SESSION": "1",
            "HOME": str(tmp_path),
        },
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"Expected empty stdout (stale sentinel passes), got: {result.stdout!r}"
    )
