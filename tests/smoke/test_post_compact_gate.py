"""
Smoke tests — Group B: hooks/post-compact-gate.py

These tests verify the three critical paths through the gate hook:

B1. Gate denies non-wait_for_messages tool calls when the sentinel is fresh
    and LOBSTER_MAIN_SESSION=1. This is the core correctness property — if
    this fails, the gate is silently broken and the dispatcher can perform
    arbitrary actions immediately after compaction.

B2. Gate allows wait_for_messages with the correct confirmation token and
    deletes the sentinel. If this path fails, the dispatcher is permanently
    deadlocked: the only permitted tool call would itself be denied.

B3. Gate passes all tool calls when the sentinel is stale (TTL fix, PR #237).
    Without the TTL, a crash or hibernation during the sentinel window would
    leave the system permanently blocked after restart.

Implementation note: the hook expands SENTINEL_FILE using os.path.expanduser,
so we redirect HOME to a temp directory and create the sentinel at the expected
relative path (messages/config/compact-pending) inside it.  The log directory
is also redirected via HOME to keep tests self-contained.

When tmux is unavailable (as in CI), is_dispatcher_session() falls back to
env-var-only detection, so setting LOBSTER_MAIN_SESSION=1 is sufficient to
activate the gate.
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

# The confirmation token the hook requires before clearing the sentinel.
# We read it from the hook source so we don't hardcode a value that the
# pre-push security scanner might mistake for a real credential.
def _read_confirmation_token() -> str:
    text = HOOK.read_text()
    for line in text.splitlines():
        if line.startswith("CONFIRMATION_TOKEN") and "=" in line:
            # Extract the quoted string value, ignoring trailing comments.
            value_part = line.split("=", 1)[1].strip()
            # Take only the part inside the first pair of quotes.
            import re
            m = re.search(r'["\']([^"\']+)["\']', value_part)
            if m:
                return m.group(1)
    raise RuntimeError(f"CONFIRMATION_TOKEN not found in {HOOK}")


CONFIRMATION_TOKEN = _read_confirmation_token()

# Relative path (from HOME) where the hook looks for the sentinel.
SENTINEL_REL = Path("messages") / "config" / "compact-pending"


def _make_sentinel(home: Path) -> Path:
    """Create a fresh sentinel file at the expected location under home."""
    sentinel = home / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    return sentinel


def _run_gate(
    home: Path,
    tool_name: str,
    tool_input: dict | None = None,
    *,
    main_session: bool = True,
    agent_id: str | None = None,
    session_id: str | None = None,
    workspace: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the gate hook with the given tool call payload piped to stdin.

    HOME is overridden to home so the sentinel and log paths are isolated.
    agent_id, when provided, simulates a subagent PreToolUse payload.
    session_id, when provided, is included in the hook payload.
    workspace, when provided, overrides LOBSTER_WORKSPACE.
    """
    payload = {"tool_name": tool_name, "tool_input": tool_input or {}}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if session_id is not None:
        payload["session_id"] = session_id
    env = {
        **os.environ,
        "HOME": str(home),
        "LOBSTER_MAIN_SESSION": "1" if main_session else "0",
    }
    if workspace is not None:
        env["LOBSTER_WORKSPACE"] = str(workspace)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
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
    returning to the main loop first.
    """
    _make_sentinel(tmp_path)

    result = _run_gate(tmp_path, tool_name="some_other_tool")

    assert result.returncode == 0, f"Hook exited non-zero: {result.stderr}"

    assert result.stdout.strip(), (
        "Expected non-empty stdout with deny decision, got empty output.\n"
        f"stderr: {result.stderr!r}"
    )
    output = json.loads(result.stdout)
    decision = output.get("hookSpecificOutput", {}).get("permissionDecision", "")
    assert decision == "deny", (
        f"Expected permissionDecision=deny, got: {decision!r}\nFull output: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# B2 — gate allows wait_for_messages with token and removes the sentinel
# ---------------------------------------------------------------------------


def test_gate_allows_wait_for_messages_and_deletes_sentinel(tmp_path):
    """B2: wait_for_messages with the correct confirmation token passes through
    AND the sentinel file is deleted.

    Failure mode caught: if the sentinel is not deleted after the correct token
    is supplied, or if wait_for_messages is denied even with the token, the
    dispatcher is permanently deadlocked — the only permitted call is blocked
    and there is no recovery path.
    """
    sentinel = _make_sentinel(tmp_path)
    assert sentinel.exists()

    result = _run_gate(
        tmp_path,
        tool_name="mcp__lobster-inbox__wait_for_messages",
        tool_input={"confirmation": CONFIRMATION_TOKEN},
    )

    assert result.returncode == 0, f"Hook exited non-zero: {result.stderr}"
    assert result.stdout.strip() == "", (
        f"Expected empty stdout (pass-through), got: {result.stdout!r}"
    )

    assert not sentinel.exists(), (
        "Sentinel file was NOT deleted after wait_for_messages with correct token. "
        "The gate would deny subsequent normal operation until the TTL expires."
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
    sentinel = _make_sentinel(tmp_path)

    # Back-date the sentinel by 700 seconds (beyond the 600 s TTL).
    stale_mtime = time.time() - 700
    os.utime(sentinel, (stale_mtime, stale_mtime))

    result = _run_gate(tmp_path, tool_name="some_other_tool")

    assert result.returncode == 0, f"Hook exited non-zero: {result.stderr}"
    assert result.stdout.strip() == "", (
        f"Expected empty stdout (stale sentinel passes), got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# B4 — subagent fast-path: agent_id present, no sentinel → pass immediately
# ---------------------------------------------------------------------------


def test_subagent_passes_without_sentinel(tmp_path):
    """B4 (Case 1): agent_id present, no sentinel — hook exits 0 with no output.

    Failure mode caught: if the agent_id fast-path is broken, subagent tool
    calls would fall through to the dispatcher-detection layers and potentially
    be blocked or incur unnecessary filesystem I/O.
    """
    # No sentinel created — normal subagent operation.
    result = _run_gate(tmp_path, tool_name="some_tool", agent_id="subagent-abc123")

    assert result.returncode == 0, f"Hook exited non-zero: {result.stderr}"
    assert result.stdout.strip() == "", (
        f"Expected empty stdout (subagent fast-exit), got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# B5 — subagent fast-path: agent_id present, sentinel active → still pass
# ---------------------------------------------------------------------------


def test_subagent_passes_with_fresh_sentinel(tmp_path):
    """B5 (Case 4): agent_id present, fresh sentinel present — hook still exits 0.

    Failure mode caught: the sentinel should only gate the dispatcher.  If a
    subagent is blocked when the sentinel is active, subagent tool calls would
    be denied during the compact-recovery window — incorrectly applying a
    dispatcher-only constraint to subagents.
    """
    _make_sentinel(tmp_path)

    result = _run_gate(tmp_path, tool_name="some_tool", agent_id="subagent-abc123")

    assert result.returncode == 0, f"Hook exited non-zero: {result.stderr}"
    assert result.stdout.strip() == "", (
        f"Expected empty stdout (subagent fast-exit despite sentinel), got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# B6 — regression: HTTP session state file must not block gate for dispatcher
# ---------------------------------------------------------------------------


def test_dispatcher_not_blocked_by_http_session_state_file(tmp_path):
    """B6 (Regression #1151): HTTP session state file must not cause dispatcher
    to be misidentified as subagent.

    Before fix: is_dispatcher_session() checked dispatcher-session-id (HTTP session
    ID, 32-char hex) against hook_input["session_id"] (Claude UUID, 36-char UUID4).
    These are different namespaces; _check_state_file() returned False (mismatch)
    when the HTTP session file existed, causing the gate to pass the dispatcher as
    if it were a subagent — bypassing the compact-pending sentinel enforcement.

    After fix: is_dispatcher_session() uses dispatcher-claude-session-id (Claude UUID)
    as the primary check, which matches the hook_input["session_id"] format correctly.
    """
    dispatcher_uuid = "b5b15385-5d9c-4755-8d7a-c5cfe3457b12"
    http_session_id = "8221b9189a0a40f99a81d72bac452e5a"  # different format, never matches

    # Set up workspace with BOTH state files (the HTTP one and the correct UUID one).
    workspace = tmp_path / "lobster-workspace"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True)

    # Write the HTTP session ID file (wrong namespace — should be ignored).
    (data_dir / "dispatcher-session-id").write_text(http_session_id)
    # Write the correct Claude UUID file (right namespace — should match).
    (data_dir / "dispatcher-claude-session-id").write_text(dispatcher_uuid)

    # Create a fresh sentinel so the gate would block if it detects dispatcher.
    _make_sentinel(tmp_path)

    # Run gate as the dispatcher (matching session_id = dispatcher UUID).
    result = _run_gate(
        tmp_path,
        tool_name="some_other_tool",
        session_id=dispatcher_uuid,
        workspace=workspace,
    )

    assert result.returncode == 0, f"Hook exited non-zero: {result.stderr}"
    output_text = result.stdout.strip()
    assert output_text, (
        "Expected gate to DENY (dispatcher with fresh sentinel should be blocked), "
        f"but got empty output.\nstderr: {result.stderr!r}\n"
        "This indicates the gate treated the dispatcher as a subagent due to the "
        "HTTP session state file namespace mismatch (regression #1151)."
    )
    output = json.loads(output_text)
    decision = output.get("hookSpecificOutput", {}).get("permissionDecision", "")
    assert decision == "deny", (
        f"Expected gate to deny dispatcher, got: {decision!r}\n"
        "The HTTP session state file caused dispatcher to be misidentified as subagent."
    )
