"""
Unit tests for the consecutive-failure bail-out in hooks/post-compact-gate.py.

Root cause addressed: after N repeated blocked-needs-token denials, the model
exhausts viable actions in its context window and goes silent (issue #2061).
The gate had no escape mechanism — it blocked indefinitely until the 10-minute
TTL expired or the health check killed the session.

Fix: after MAX_FAILED_ATTEMPTS consecutive blocked-needs-token failures within
the same sentinel window, the gate deletes the sentinel (and its associated
failure counter) and logs a bail-out warning. This prevents the model from
reaching the freeze state by clearing the gate before the context window is
exhausted.

Behaviors verified:
  F1. First N-1 failures increment the counter and still block.
  F2. The Nth failure triggers bail-out: sentinel + counter deleted, no deny output.
  F3. Correct token clears sentinel + resets counter on success.
  F4. Counter file is deleted when sentinel expires (stale TTL path).
  F5. Counter file is absent after clean sentinel deletion.
"""

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "post-compact-gate.py"

SENTINEL_REL = Path("messages") / "config" / "compact-pending"
COUNTER_REL = Path("messages") / "config" / "compact-pending-failures"

# The confirmation token that clears the sentinel on a successful WFM call.
# Derived from the hook source so we don't hardcode a value that the security
# scanner might flag as a credential.
def _read_confirmation_token() -> str:
    text = _HOOK_PATH.read_text()
    for line in text.splitlines():
        if line.startswith("CONFIRMATION_TOKEN") and "=" in line:
            import re
            value_part = line.split("=", 1)[1].strip()
            m = re.search(r'["\']([^"\']+)["\']', value_part)
            if m:
                return m.group(1)
    raise RuntimeError(f"CONFIRMATION_TOKEN not found in {_HOOK_PATH}")

CONFIRMATION_TOKEN = _read_confirmation_token()


def _make_sentinel(home: Path) -> Path:
    sentinel = home / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    return sentinel


def _make_dispatcher_session_file(tmp_path: Path, session_id: str) -> Path:
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / "dispatcher-session-id"
    marker.write_text(session_id)
    return marker


def _load_hook(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

    spec = importlib.util.spec_from_file_location("post_compact_gate", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    if str(_HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOKS_DIR))
    spec.loader.exec_module(mod)
    return mod


def _make_hook_input(
    tool_name: str,
    tool_input: dict | None = None,
    agent_id: str | None = None,
) -> dict:
    payload = {"tool_name": tool_name, "tool_input": tool_input or {}}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    stdout_cap = StringIO()
    stderr_cap = StringIO()
    stdin_data = json.dumps(hook_input)
    exit_code = None

    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
    ):
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _wfm_input_no_token() -> dict:
    return _make_hook_input(
        tool_name="mcp__lobster-inbox__wait_for_messages",
        tool_input={},
    )


def _wfm_input_wrong_token() -> dict:
    return _make_hook_input(
        tool_name="mcp__lobster-inbox__wait_for_messages",
        tool_input={"confirmation": "wrong-token"},
    )


def _wfm_input_correct_token() -> dict:
    return _make_hook_input(
        tool_name="mcp__lobster-inbox__wait_for_messages",
        tool_input={"confirmation": CONFIRMATION_TOKEN},
    )


def _setup(monkeypatch, tmp_path: Path):
    """Create sentinel + dispatcher marker, load hook module with redirected paths."""
    _make_sentinel(tmp_path)
    _make_dispatcher_session_file(tmp_path, "sess-disp-bailout")
    mod = _load_hook(monkeypatch, tmp_path)
    mod.SENTINEL_FILE = tmp_path / SENTINEL_REL
    mod.FAILURE_COUNTER_FILE = tmp_path / COUNTER_REL

    import session_role
    monkeypatch.setattr(
        session_role, "DISPATCHER_SESSION_FILE",
        tmp_path / "messages" / "config" / "dispatcher-session-id",
    )
    return mod


# ---------------------------------------------------------------------------
# F1: failures before the threshold still block
# ---------------------------------------------------------------------------


class TestFailuresBeforeThreshold:
    """Each failure below MAX_FAILED_ATTEMPTS increments the counter but keeps blocking."""

    def test_first_failure_still_blocks(self, monkeypatch, tmp_path):
        """F1a: a single failed WFM call (no token) still produces a deny decision."""
        mod = _setup(monkeypatch, tmp_path)
        sentinel = tmp_path / SENTINEL_REL

        exit_code, stdout, stderr = _run_hook(mod, _wfm_input_no_token())

        assert exit_code == 0, f"Hook should exit 0, got {exit_code}. stderr: {stderr!r}"
        assert stdout.strip(), "Expected deny decision on stdout"
        output = json.loads(stdout)
        decision = output.get("hookSpecificOutput", {}).get("permissionDecision", "")
        assert decision == "deny", f"Expected deny, got {decision!r}"
        assert sentinel.exists(), "Sentinel must remain after first failure — threshold not reached"

    def test_failure_increments_counter(self, monkeypatch, tmp_path):
        """F1b: each blocked-needs-token event increments the failure counter file."""
        mod = _setup(monkeypatch, tmp_path)
        counter_file = tmp_path / COUNTER_REL

        assert not counter_file.exists(), "Counter should not exist before any failures"

        _run_hook(mod, _wfm_input_no_token())
        assert counter_file.exists(), "Counter file must be created after first failure"
        count_after_1 = int(counter_file.read_text().strip())
        assert count_after_1 == 1, f"Expected count=1 after 1 failure, got {count_after_1}"

        _run_hook(mod, _wfm_input_wrong_token())
        count_after_2 = int(counter_file.read_text().strip())
        assert count_after_2 == 2, f"Expected count=2 after 2 failures, got {count_after_2}"

    def test_second_failure_still_blocks(self, monkeypatch, tmp_path):
        """F1c: a second failed WFM call (below threshold) still blocks."""
        mod = _setup(monkeypatch, tmp_path)
        sentinel = tmp_path / SENTINEL_REL

        _run_hook(mod, _wfm_input_no_token())
        exit_code, stdout, stderr = _run_hook(mod, _wfm_input_no_token())

        assert exit_code == 0
        output = json.loads(stdout)
        decision = output.get("hookSpecificOutput", {}).get("permissionDecision", "")
        assert decision == "deny", f"Expected deny on 2nd failure, got {decision!r}"
        assert sentinel.exists(), "Sentinel must remain after 2nd failure — threshold not reached"


# ---------------------------------------------------------------------------
# F2: Nth failure triggers bail-out
# ---------------------------------------------------------------------------


class TestBailoutOnThreshold:
    """After MAX_FAILED_ATTEMPTS consecutive failures, gate clears sentinel and allows through."""

    def _exhaust_failures(self, mod, count: int) -> None:
        """Run count failures against the gate without checking results."""
        for _ in range(count):
            _run_hook(mod, _wfm_input_no_token())

    def test_nth_failure_clears_sentinel(self, monkeypatch, tmp_path):
        """F2a: after MAX_FAILED_ATTEMPTS failures, the sentinel file is deleted."""
        mod = _setup(monkeypatch, tmp_path)
        sentinel = tmp_path / SENTINEL_REL
        threshold = mod.MAX_FAILED_ATTEMPTS

        # Exhaust all failures up to the threshold.
        for _ in range(threshold):
            _run_hook(mod, _wfm_input_no_token())

        assert not sentinel.exists(), (
            f"Sentinel must be deleted after {threshold} consecutive failures. "
            "The gate must bail out to prevent model freeze."
        )

    def test_nth_failure_clears_counter(self, monkeypatch, tmp_path):
        """F2b: after bail-out, the failure counter file is also deleted."""
        mod = _setup(monkeypatch, tmp_path)
        counter_file = tmp_path / COUNTER_REL
        threshold = mod.MAX_FAILED_ATTEMPTS

        for _ in range(threshold):
            _run_hook(mod, _wfm_input_no_token())

        assert not counter_file.exists(), (
            "Counter file must be deleted after bail-out."
        )

    def test_nth_failure_allows_tool_through(self, monkeypatch, tmp_path):
        """F2c: the Nth failure produces no deny output (gate allows through after bail-out)."""
        mod = _setup(monkeypatch, tmp_path)
        threshold = mod.MAX_FAILED_ATTEMPTS

        # Run threshold-1 failures first.
        for _ in range(threshold - 1):
            _run_hook(mod, _wfm_input_no_token())

        # The final (Nth) failure should bail out and allow through.
        exit_code, stdout, stderr = _run_hook(mod, _wfm_input_no_token())

        assert exit_code == 0, f"Hook should exit 0 on bail-out, got {exit_code}"
        assert stdout.strip() == "", (
            f"Bail-out call should produce no deny output (empty stdout), got: {stdout!r}"
        )

    def test_post_bailout_tool_calls_allowed(self, monkeypatch, tmp_path):
        """F2d: after bail-out, subsequent tool calls pass through normally (sentinel gone)."""
        mod = _setup(monkeypatch, tmp_path)
        threshold = mod.MAX_FAILED_ATTEMPTS

        for _ in range(threshold):
            _run_hook(mod, _wfm_input_no_token())

        # Sentinel is now gone — any tool should pass.
        hook_input = _make_hook_input(tool_name="mcp__lobster-inbox__check_inbox")
        exit_code, stdout, _ = _run_hook(mod, hook_input)

        assert exit_code == 0
        assert stdout.strip() == "", (
            "After bail-out, non-WFM tools should pass through (sentinel deleted)."
        )

    def test_threshold_is_named_constant(self, monkeypatch, tmp_path):
        """F2e: MAX_FAILED_ATTEMPTS is a named constant exposed on the module (not a magic literal)."""
        mod = _setup(monkeypatch, tmp_path)
        assert hasattr(mod, "MAX_FAILED_ATTEMPTS"), (
            "MAX_FAILED_ATTEMPTS must be a named constant on the module for testability."
        )
        assert isinstance(mod.MAX_FAILED_ATTEMPTS, int), "MAX_FAILED_ATTEMPTS must be an int"
        assert mod.MAX_FAILED_ATTEMPTS >= 2, "Threshold must be at least 2 (1 would bail on first failure)"


# ---------------------------------------------------------------------------
# F3: correct token clears sentinel + resets counter
# ---------------------------------------------------------------------------


class TestCorrectTokenClearsCounter:
    """A correct token clears both sentinel and counter in a single call."""

    def test_correct_token_after_failures_clears_counter(self, monkeypatch, tmp_path):
        """F3a: counter is reset when the correct token is supplied (even after prior failures)."""
        mod = _setup(monkeypatch, tmp_path)
        counter_file = tmp_path / COUNTER_REL
        threshold = mod.MAX_FAILED_ATTEMPTS

        # Accumulate some failures (below threshold).
        for _ in range(threshold - 1):
            _run_hook(mod, _wfm_input_no_token())

        assert counter_file.exists(), "Counter should exist after failures"

        # Supply the correct token.
        _run_hook(mod, _wfm_input_correct_token())

        assert not counter_file.exists(), (
            "Counter file must be deleted when the correct token clears the sentinel."
        )

    def test_correct_token_allows_through(self, monkeypatch, tmp_path):
        """F3b: correct token allows WFM through (basic correctness, regression guard)."""
        mod = _setup(monkeypatch, tmp_path)

        exit_code, stdout, stderr = _run_hook(mod, _wfm_input_correct_token())

        assert exit_code == 0, f"Correct token should exit 0, got {exit_code}. stderr: {stderr!r}"
        assert stdout.strip() == "", (
            f"Correct token should produce no deny output, got: {stdout!r}"
        )


# ---------------------------------------------------------------------------
# F4: stale sentinel clears counter
# ---------------------------------------------------------------------------


class TestStaleSentinelClearsCounter:
    """A stale sentinel (TTL expired) causes the gate to pass and cleans up the counter file."""

    def test_stale_sentinel_deletes_counter(self, monkeypatch, tmp_path):
        """F4a: when the sentinel is stale, the failure counter is also deleted."""
        import os
        import time

        mod = _setup(monkeypatch, tmp_path)
        sentinel = tmp_path / SENTINEL_REL
        counter_file = tmp_path / COUNTER_REL

        # Write a failure count manually (simulating prior failures).
        counter_file.parent.mkdir(parents=True, exist_ok=True)
        counter_file.write_text("2")

        # Back-date the sentinel beyond the TTL.
        stale_mtime = time.time() - (mod.SENTINEL_TTL_SECONDS + 60)
        os.utime(sentinel, (stale_mtime, stale_mtime))

        # Any tool call should pass (stale sentinel) and clean up counter.
        hook_input = _make_hook_input(tool_name="mcp__lobster-inbox__check_inbox")
        exit_code, stdout, _ = _run_hook(mod, hook_input)

        assert exit_code == 0
        assert stdout.strip() == "", "Stale sentinel should allow through"
        assert not counter_file.exists(), (
            "Counter file must be deleted when a stale sentinel is cleaned up."
        )


# ---------------------------------------------------------------------------
# F5: no sentinel means no counter accumulation
# ---------------------------------------------------------------------------


class TestNoSentinelNoCounter:
    """Without a sentinel, the counter is never created (normal operation)."""

    def test_no_counter_without_sentinel(self, monkeypatch, tmp_path):
        """F5a: calling WFM without a sentinel does not create a failure counter."""
        # No sentinel — normal operation.
        _make_dispatcher_session_file(tmp_path, "sess-no-sentinel")
        mod = _load_hook(monkeypatch, tmp_path)
        mod.SENTINEL_FILE = tmp_path / SENTINEL_REL
        mod.FAILURE_COUNTER_FILE = tmp_path / COUNTER_REL

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        counter_file = tmp_path / COUNTER_REL

        # Call WFM (no sentinel) — should pass, no counter created.
        _run_hook(mod, _wfm_input_no_token())

        assert not counter_file.exists(), (
            "Failure counter must not be created when there is no active sentinel."
        )
