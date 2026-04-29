"""
Smoke tests — Group E: scripts/run-upgrades.sh (issue #1757)

These tests verify the correctness properties of the safe upgrade wrapper
script without requiring systemd, apt-get, or a live MCP instance.

Why these tests exist:

E1. A syntax error in run-upgrades.sh would silently break weekly upgrades —
    the cron job exits non-zero but cron discards stderr.  Catching syntax
    errors here means they surface before deploy.

E2. In LOBSTER_DEV_MODE, the script must exit 0 immediately without calling
    restart-mcp.sh or running any package manager.  Running upgrades on a
    developer machine would be disruptive and unexpected.

E3. In --dry-run mode the script must log what it would do and exit 0 without
    calling restart-mcp.sh or running any package manager.  Dry-run is used in
    CI and manual testing where side effects are unacceptable.

E4. The script must abort if restart-mcp.sh is missing or not executable.
    Proceeding with the upgrade without a safe MCP restart would recreate the
    exact failure mode the script was written to prevent (issue #1757).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Absolute path to the upgrade script under test.
UPGRADE_SCRIPT = Path(__file__).parents[2] / "scripts" / "run-upgrades.sh"


# ---------------------------------------------------------------------------
# E1 — syntax check
# ---------------------------------------------------------------------------


def test_run_upgrades_passes_syntax_check() -> None:
    """
    E1: run-upgrades.sh must pass bash -n (no syntax errors).

    Failure mode: a syntax error causes the weekly cron job to silently exit
    non-zero without performing upgrades or writing any log output.
    """
    assert UPGRADE_SCRIPT.exists(), (
        f"run-upgrades.sh not found at {UPGRADE_SCRIPT}. "
        "Has the script been moved or renamed?"
    )

    result = subprocess.run(
        ["bash", "-n", str(UPGRADE_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bash -n reported syntax errors in {UPGRADE_SCRIPT.name}:\n"
        f"{result.stderr}"
    )


# ---------------------------------------------------------------------------
# E2 — LOBSTER_DEV_MODE causes immediate clean exit
# ---------------------------------------------------------------------------


def test_dev_mode_causes_clean_exit_without_upgrade(tmp_path: Path) -> None:
    """
    E2: When LOBSTER_DEV_MODE=true is set in config.env, run-upgrades.sh must
    exit 0 immediately without calling restart-mcp.sh or running any upgrade.

    We verify this by pointing LOBSTER_CONFIG_DIR at a temp directory whose
    config.env sets LOBSTER_DEV_MODE=true, and placing a sentinel restart-mcp.sh
    that exits 99 if called.  The test asserts exit code 0 and that the sentinel
    was never invoked.
    """
    # Write config.env with dev mode enabled.
    config_dir = tmp_path / "lobster-config"
    config_dir.mkdir()
    (config_dir / "config.env").write_text("LOBSTER_DEV_MODE=true\n")

    # Write a workspace with logs dir so the script can write its log.
    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

    # Write a fake lobster install dir with a sentinel restart-mcp.sh that
    # signals it was called by creating a marker file.
    install_dir = tmp_path / "lobster"
    scripts_dir = install_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    sentinel = tmp_path / "restart-mcp-called"
    fake_restart = scripts_dir / "restart-mcp.sh"
    fake_restart.write_text(
        f"#!/bin/bash\ntouch {sentinel}\nexit 99\n"
    )
    fake_restart.chmod(0o755)

    env = {
        **os.environ,
        "LOBSTER_CONFIG_DIR": str(config_dir),
        "LOBSTER_WORKSPACE": str(workspace_dir),
        "LOBSTER_INSTALL_DIR": str(install_dir),
    }

    result = subprocess.run(
        ["bash", str(UPGRADE_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, (
        "run-upgrades.sh did not exit 0 in LOBSTER_DEV_MODE. "
        f"returncode={result.returncode}\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert not sentinel.exists(), (
        "run-upgrades.sh called restart-mcp.sh in LOBSTER_DEV_MODE. "
        "Upgrades must be fully suppressed when dev mode is active."
    )


# ---------------------------------------------------------------------------
# E3 — --dry-run exits cleanly without calling restart-mcp.sh
# ---------------------------------------------------------------------------


def test_dry_run_exits_cleanly_without_side_effects(tmp_path: Path) -> None:
    """
    E3: When invoked with --dry-run, run-upgrades.sh must exit 0 and must NOT
    call restart-mcp.sh or any package manager.

    We verify this by placing a sentinel restart-mcp.sh and asserting it is
    never invoked.
    """
    # No LOBSTER_DEV_MODE — ensure we are past the dev mode guard.
    config_dir = tmp_path / "lobster-config"
    config_dir.mkdir()
    (config_dir / "config.env").write_text("# no dev mode\n")

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

    install_dir = tmp_path / "lobster"
    scripts_dir = install_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    sentinel = tmp_path / "restart-mcp-called"
    fake_restart = scripts_dir / "restart-mcp.sh"
    fake_restart.write_text(
        f"#!/bin/bash\ntouch {sentinel}\nexit 99\n"
    )
    fake_restart.chmod(0o755)

    env = {
        **os.environ,
        "LOBSTER_CONFIG_DIR": str(config_dir),
        "LOBSTER_WORKSPACE": str(workspace_dir),
        "LOBSTER_INSTALL_DIR": str(install_dir),
    }

    result = subprocess.run(
        ["bash", str(UPGRADE_SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, (
        "run-upgrades.sh --dry-run did not exit 0. "
        f"returncode={result.returncode}\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert not sentinel.exists(), (
        "run-upgrades.sh --dry-run called restart-mcp.sh. "
        "Dry-run must not trigger any restarts or upgrades."
    )


# ---------------------------------------------------------------------------
# E4 — missing restart-mcp.sh causes non-zero exit (abort before upgrade)
# ---------------------------------------------------------------------------


def test_missing_restart_script_causes_abort(tmp_path: Path) -> None:
    """
    E4: When restart-mcp.sh is missing or not executable at LOBSTER_INSTALL_DIR,
    run-upgrades.sh must abort with a non-zero exit before running any upgrade.

    Failure mode: if this guard is absent, a broken install would proceed to
    run apt-get upgrade without the safe MCP restart pre-step — recreating the
    exact failure mode described in issue #1757.
    """
    config_dir = tmp_path / "lobster-config"
    config_dir.mkdir()
    (config_dir / "config.env").write_text("# no dev mode\n")

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

    # Install dir has scripts/ but restart-mcp.sh is absent.
    install_dir = tmp_path / "lobster"
    (install_dir / "scripts").mkdir(parents=True, exist_ok=True)
    # Intentionally NOT creating restart-mcp.sh here.

    env = {
        **os.environ,
        "LOBSTER_CONFIG_DIR": str(config_dir),
        "LOBSTER_WORKSPACE": str(workspace_dir),
        "LOBSTER_INSTALL_DIR": str(install_dir),
    }

    result = subprocess.run(
        ["bash", str(UPGRADE_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode != 0, (
        "run-upgrades.sh exited 0 when restart-mcp.sh was missing. "
        "The script must abort before running any upgrade if the safe "
        "restart wrapper is not present."
    )
