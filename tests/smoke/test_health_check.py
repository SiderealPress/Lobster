"""
Smoke tests — Group D: scripts/health-check-v3.sh

These tests verify the most critical correctness properties of the health check
script without requiring systemd, tmux, or Telegram credentials.  They work by
running isolated bash snippets extracted from the script — the same technique
the existing bash test suite uses — so they exercise real production code rather
than mocks.

Why these tests exist:

D1. A syntax error in health-check-v3.sh silently breaks monitoring: the cron
    job exits 0 (bash -n is non-zero but cron discards stderr) and the system
    goes unwatched. Catching syntax errors here means they surface before deploy.

D2. Compaction suppression is the most safety-critical logic in the health check.
    When Claude Code compacts its context, tool calls pause for 1–3 minutes.
    During this window, real user messages can age past STALE_THRESHOLD_SECONDS
    and trigger a false-positive restart. If is_compaction_recent() fails to
    return "true" for a fresh compacted_at, the health check will restart the
    dispatcher mid-compaction — corrupting state and losing messages.

D3. Compaction suppression must have a TTL: if the system crashes during a
    compaction window, the suppression must expire.  If is_compaction_recent()
    incorrectly returns "true" for a stale compacted_at (older than
    COMPACTION_SUPPRESS_SECONDS), monitoring is silently disabled for extended
    periods and genuine stuck-dispatcher events go undetected.

D4. The maintenance flag (written by `lobster stop`) must cause an immediate
    clean exit with no checks run and no restart attempted.  If this flag is
    ignored, manual maintenance windows trigger spurious health-check restarts
    that fight the operator.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Absolute path to the health check script under test.
HEALTH_SCRIPT = Path(__file__).parents[2] / "scripts" / "health-check-v3.sh"

# How long the script suppresses stale-inbox checks after a compaction.
# Must match COMPACTION_SUPPRESS_SECONDS in health-check-v3.sh (300).
COMPACTION_SUPPRESS_SECONDS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_bash_fragment(
    fragment: str,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """
    Run a bash fragment that sources helper functions from the health check
    script and then executes `fragment`.  Returns the CompletedProcess so
    callers can inspect returncode, stdout, and stderr.
    """
    merged_env = {**os.environ, **(env or {})}
    script = f"""
#!/bin/bash
set -o pipefail

# Source the is_compaction_recent helper and its dependencies.
# We extract only the functions we need so we don't need systemd/tmux/curl.
{fragment}
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=merged_env,
    )


def _extract_function(name: str) -> str:
    """
    Extract a single top-level bash function from the health check script.
    Returns the function source text (including the closing brace).

    Raises AssertionError if the function is not found.
    """
    text = HEALTH_SCRIPT.read_text()
    lines = text.splitlines(keepends=True)

    # Find the line that starts the function definition.
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{name}()"):
            start = i
            break

    assert start is not None, (
        f"Function '{name}()' not found in {HEALTH_SCRIPT}. "
        "Was it renamed or removed?"
    )

    # Collect lines until we hit the closing brace at column 0.
    result = []
    for line in lines[start:]:
        result.append(line)
        if line.rstrip() == "}":
            break

    return "".join(result)


def _is_compaction_recent_script(state_file: Path, tmp_path: Path) -> str:
    """
    Build a self-contained bash script fragment that:
    - Sets the minimal variables is_compaction_recent() reads
    - Injects a stub log_info() so logging doesn't fail
    - Calls is_compaction_recent()
    - Exits with the function's return code

    We source only the functions we need to keep tests isolated.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "health-check.log"

    fn_body = _extract_function("is_compaction_recent")

    return f"""
#!/bin/bash
LOBSTER_STATE_FILE="{state_file}"
COMPACTION_SUPPRESS_SECONDS={COMPACTION_SUPPRESS_SECONDS}
LOG_FILE="{log_file}"
mkdir -p "$(dirname "$LOG_FILE")"
log()      {{ echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }}
log_info() {{ log "INFO" "$1"; }}

{fn_body}

is_compaction_recent
exit $?
"""


# ---------------------------------------------------------------------------
# D1 — syntax check
# ---------------------------------------------------------------------------


def test_health_check_passes_syntax_check() -> None:
    """
    D1: health-check-v3.sh must pass bash -n (no syntax errors).

    Failure mode: a syntax error causes the cron job to silently exit non-zero.
    Bash does not write to cron's email by default, so a syntactically broken
    health check goes completely unnoticed — the system is unwatched.
    """
    assert HEALTH_SCRIPT.exists(), (
        f"health-check-v3.sh not found at {HEALTH_SCRIPT}. "
        "Has the script been moved or renamed?"
    )

    result = subprocess.run(
        ["bash", "-n", str(HEALTH_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bash -n reported syntax errors in {HEALTH_SCRIPT.name}:\n"
        f"{result.stderr}"
    )


# ---------------------------------------------------------------------------
# D2 — compaction suppression fires for a fresh compacted_at
# ---------------------------------------------------------------------------


def test_compaction_suppression_fires_when_compaction_recent(tmp_path: Path) -> None:
    """
    D2: is_compaction_recent() must return true (exit 0) when compacted_at in
    lobster-state.json is within the last COMPACTION_SUPPRESS_SECONDS seconds.

    Failure mode: if this function returns false for a fresh compaction, the
    health check proceeds to check the inbox for stale messages.  During the
    1-3 minute compaction pause, real user messages WILL be stale.  The health
    check then restarts the dispatcher — corrupting its state and discarding any
    in-progress work.  This is the most disruptive false-positive in the system.
    """
    state_file = tmp_path / "lobster-state.json"
    # Write a compacted_at that is 30 seconds old — well within suppress window.
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    state_file.write_text(json.dumps({"mode": "active", "compacted_at": ts}))

    fragment = _is_compaction_recent_script(state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 0, (
        "is_compaction_recent() returned false (non-zero) for a 30-second-old "
        "compacted_at, but should return true (0) to suppress stale-inbox checks "
        f"within the {COMPACTION_SUPPRESS_SECONDS}s window.\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# D3 — compaction suppression does NOT fire when compacted_at is stale
# ---------------------------------------------------------------------------


def test_compaction_suppression_off_when_compaction_stale(tmp_path: Path) -> None:
    """
    D3: is_compaction_recent() must return false (non-zero) when compacted_at is
    older than COMPACTION_SUPPRESS_SECONDS.

    Failure mode: if this function returns true for a stale compacted_at, the
    health check suppresses its stale-inbox logic indefinitely after any prior
    compaction event.  A genuinely stuck dispatcher would never trigger a
    restart or alert — monitoring is silently disabled.
    """
    state_file = tmp_path / "lobster-state.json"
    # Use a timestamp far enough in the past to be outside the suppress window.
    stale_age = COMPACTION_SUPPRESS_SECONDS + 120
    stale_epoch = time.time() - stale_age
    stale_ts = datetime.fromtimestamp(stale_epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    state_file.write_text(json.dumps({"mode": "active", "compacted_at": stale_ts}))

    fragment = _is_compaction_recent_script(state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        f"is_compaction_recent() returned true (0) for a compacted_at that is "
        f"{stale_age}s old, but the suppress window is only {COMPACTION_SUPPRESS_SECONDS}s. "
        "Stale compaction timestamps must NOT suppress monitoring.\n"
        f"stderr: {result.stderr!r}"
    )


def test_compaction_suppression_off_when_no_state_file(tmp_path: Path) -> None:
    """
    D3 (edge case): is_compaction_recent() must return false when lobster-state.json
    does not exist.

    Failure mode: if this returns true when the state file is absent (e.g. fresh
    install, deleted file), monitoring is silently disabled from the first run.
    """
    missing_state_file = tmp_path / "nonexistent-state.json"
    # Do NOT create the file.

    fragment = _is_compaction_recent_script(missing_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "is_compaction_recent() returned true (0) when lobster-state.json does "
        "not exist. This would suppress stale-inbox monitoring on fresh installs "
        "or after the state file is deleted.\n"
        f"stderr: {result.stderr!r}"
    )


def test_compaction_suppression_off_when_compacted_at_missing(tmp_path: Path) -> None:
    """
    D3 (edge case): is_compaction_recent() must return false when lobster-state.json
    exists but has no compacted_at field.

    Failure mode: if this returns true for a state file that never had a
    compacted_at (e.g. written by an older version of the wrapper), stale-inbox
    monitoring is incorrectly suppressed.
    """
    state_file = tmp_path / "lobster-state.json"
    state_file.write_text(json.dumps({"mode": "active"}))  # no compacted_at

    fragment = _is_compaction_recent_script(state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "is_compaction_recent() returned true (0) when compacted_at is absent "
        "from lobster-state.json. This would suppress stale-inbox monitoring "
        "on systems that have never had a compaction event.\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# D4 — maintenance flag causes immediate clean exit
# ---------------------------------------------------------------------------


def test_maintenance_flag_causes_clean_exit(tmp_path: Path) -> None:
    """
    D4: When the maintenance flag exists, the health check must exit 0 without
    running any health checks or triggering any restarts.

    Failure mode: if the maintenance flag is ignored, `lobster stop` followed by
    any health check run within the maintenance window causes the health check to
    restart the service the operator just stopped. This creates a fight between
    the operator and the monitor — the system cannot be cleanly taken offline.

    We test this by running the full health-check script with HOME and
    LOBSTER_MESSAGES redirected to a temp directory, placing the maintenance
    flag at the expected path, and asserting a clean exit.
    """
    # Set up a fake messages directory with the maintenance flag in place.
    messages_dir = tmp_path / "messages"
    config_dir = messages_dir / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "lobster-maintenance").touch()

    # Create required directory structure so the script can mkdir log dirs.
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "logs").mkdir(parents=True)

    env = {
        **os.environ,
        "LOBSTER_MESSAGES": str(messages_dir),
        "LOBSTER_WORKSPACE": str(workspace_dir),
        # Point config.env to a non-existent file so Telegram alerting is
        # safely disabled (no network calls in smoke tests).
        "LOBSTER_CONFIG_DIR": str(tmp_path / "no-config"),
        # Override the lock file to a temp path so the test doesn't conflict
        # with a running health check on the same machine.
        "LOBSTER_HEALTH_LOCK": str(tmp_path / "health-check.lock"),
    }

    result = subprocess.run(
        ["bash", str(HEALTH_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, (
        "health-check-v3.sh did not exit 0 under maintenance flag. "
        f"returncode={result.returncode}\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
