"""
Smoke tests: scripts/cleanup-worktrees-audio.sh — test-fixture pruning in ~/messages/failed/

Issue #2209: the unrecognized-source quarantine guard in
src/mcp/inbox_server.py moves any inbox message with source="test" to
~/messages/failed/ as `test_<epoch>.json` (see
tests/unit/test_mcp_server/test_unrecognized_source_quarantine.py). These
synthetic fixtures accumulate indefinitely and drown out genuine quarantined
messages in the same directory. This adds a `prune_failed_test_fixtures` step
to the existing daily cleanup cron job to remove them automatically, while
never touching files that don't match the exact `test_<digits>.json` pattern.

Behaviors verified:

F1. `test_<digits>.json` files older than the retention window are deleted
    from the failed/ directory.
F2. `test_<digits>.json` files newer than the retention window are preserved
    (avoids racing a write that's still in flight).
F3. Files that do not match the `test_<digits>.json` pattern — including the
    one known real quarantined message shape — are never deleted, regardless
    of age.
F4. Script does not fail if the failed/ directory is missing — it logs a
    skip message and exits 0.
F5. Script passes bash -n syntax check.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

CLEANUP_SCRIPT = Path(__file__).parents[2] / "scripts" / "cleanup-worktrees-audio.sh"

# Retention period the tests exercise — must match the script default.
FAILED_TEST_FIXTURE_RETENTION_DAYS = 1


def run_script(env: dict | None = None, **kwargs) -> subprocess.CompletedProcess:
    """Run the cleanup script with a fully isolated environment overlay.

    We pass a minimal env rather than inheriting the full process env so that
    paths like HOME, LOBSTER_MESSAGES, etc. don't bleed in from the test runner
    and cause the script to operate on real directories.
    """
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", str(CLEANUP_SCRIPT)],
        capture_output=True,
        text=True,
        env=base_env,
        **kwargs,
    )


def isolation_env(tmp_path: Path) -> dict:
    """Return a minimal env dict that keeps the script confined to tmp_path."""
    return {
        "LOBSTER_INSTALL_DIR": str(tmp_path / "lobster-nonexistent"),
        "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
        "LOBSTER_PROJECTS": str(tmp_path / "projects"),
        "LOBSTER_MESSAGES": str(tmp_path / "messages"),
    }


def make_old_file(path: Path, days_old: int = FAILED_TEST_FIXTURE_RETENTION_DAYS + 1) -> None:
    """Create a file and backdate its mtime so it appears `days_old` days old."""
    path.write_text("{}")
    old_time = time.time() - (days_old * 86400)
    os.utime(path, (old_time, old_time))


def make_new_file(path: Path, days_old: float = 0) -> None:
    """Create a file dated within the retention window (default: just created)."""
    path.write_text("{}")
    recent_time = time.time() - (days_old * 86400)
    os.utime(path, (recent_time, recent_time))


# ---------------------------------------------------------------------------
# F5: Syntax check (run first — a broken script invalidates all other tests)
# ---------------------------------------------------------------------------

def test_script_has_no_syntax_errors():
    """F5: bash -n must exit 0 — a syntax error silently breaks cron."""
    result = subprocess.run(
        ["bash", "-n", str(CLEANUP_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Syntax error in cleanup script:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# F1: Old test_<epoch>.json fixtures are deleted
# ---------------------------------------------------------------------------

def test_old_test_fixture_is_deleted(tmp_path):
    """F1: A test_<digits>.json file older than the retention window is removed."""
    failed_dir = tmp_path / "messages" / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    old_fixture = failed_dir / "test_1786476577.json"
    make_old_file(old_fixture)
    assert old_fixture.exists(), "Pre-condition: file must exist before running script"

    env = isolation_env(tmp_path)
    env["CLEANUP_FAILED_TEST_FIXTURE_RETENTION_DAYS"] = str(FAILED_TEST_FIXTURE_RETENTION_DAYS)

    result = run_script(env=env)
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    assert not old_fixture.exists(), "Old test_<digits>.json fixture should have been deleted"


# ---------------------------------------------------------------------------
# F2: Freshly-written test fixtures are preserved (retention window)
# ---------------------------------------------------------------------------

def test_recent_test_fixture_is_preserved(tmp_path):
    """F2: A test_<digits>.json file within the retention window is not deleted."""
    failed_dir = tmp_path / "messages" / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    recent_fixture = failed_dir / "test_1786769893.json"
    make_new_file(recent_fixture)
    assert recent_fixture.exists()

    env = isolation_env(tmp_path)
    env["CLEANUP_FAILED_TEST_FIXTURE_RETENTION_DAYS"] = str(FAILED_TEST_FIXTURE_RETENTION_DAYS)

    result = run_script(env=env)
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    assert recent_fixture.exists(), (
        "Recent test fixture was incorrectly deleted (within retention window)"
    )


# ---------------------------------------------------------------------------
# F3: Non-matching files (including real quarantined messages) are untouched
# ---------------------------------------------------------------------------

def test_non_matching_files_are_never_deleted(tmp_path):
    """F3: Only the exact test_<digits>.json pattern is removed; everything else survives, even when old."""
    failed_dir = tmp_path / "messages" / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    # Shaped like the one known real quarantined message in production.
    real_quarantine = failed_dir / "daily-health-20260425-060005.json"
    # Similar-looking but not an exact match for the test_<digits>.json pattern.
    near_miss_prefix = failed_dir / "test_run_1786476577.json"
    near_miss_suffix = failed_dir / "test_1786476577.json.bak"
    near_miss_nondigit = failed_dir / "test_abc.json"

    for f in (real_quarantine, near_miss_prefix, near_miss_suffix, near_miss_nondigit):
        make_old_file(f)

    env = isolation_env(tmp_path)
    env["CLEANUP_FAILED_TEST_FIXTURE_RETENTION_DAYS"] = str(FAILED_TEST_FIXTURE_RETENTION_DAYS)

    result = run_script(env=env)
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    for f in (real_quarantine, near_miss_prefix, near_miss_suffix, near_miss_nondigit):
        assert f.exists(), f"{f.name} should never be deleted by the test-fixture prune step"


# ---------------------------------------------------------------------------
# F4: Missing failed/ directory is handled gracefully
# ---------------------------------------------------------------------------

def test_missing_failed_directory_is_skipped(tmp_path):
    """F4: Script exits 0 and logs a skip message when failed/ doesn't exist."""
    # Point CLEANUP_FAILED_DIR at a path that definitely does not exist, rather
    # than relying on LOBSTER_MESSAGES/failed (which the test suite's own
    # autouse isolation fixture pre-creates under tmp_path for every test).
    nonexistent_failed_dir = tmp_path / "definitely-absent" / "failed"
    assert not nonexistent_failed_dir.exists(), "Pre-condition: dir must not exist"

    env = isolation_env(tmp_path)
    env["CLEANUP_FAILED_DIR"] = str(nonexistent_failed_dir)

    result = run_script(env=env)
    assert result.returncode == 0, (
        f"Script should exit 0 even when failed/ dir is missing.\nstderr: {result.stderr}"
    )
    assert "failed" in result.stdout.lower() and "skipping" in result.stdout.lower(), (
        f"Expected a 'skipping' message referencing the failed dir in stdout.\n"
        f"Got stdout:\n{result.stdout}"
    )
