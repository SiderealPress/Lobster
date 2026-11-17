"""Behaviour tests for scripts/disk-usage-alert.sh.

The script is exercised end-to-end with a stub ``scripts/alert.sh`` (captures the
message instead of writing to the Telegram outbox) and a throwaway workspace, so
nothing here touches production state.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "disk-usage-alert.sh"

ALWAYS_ABOVE = 0      # root usage is never 0%, so this threshold always alerts
NEVER_ABOVE = 100     # and never exceeds 100%
COOLDOWN_SECS = 3600
TEST_CHAT_ID = "424242"


@pytest.fixture
def harness(tmp_path):
    """Fake install dir (stub alert.sh), fake workspace, isolated state file."""
    install = tmp_path / "install" / "scripts"
    install.mkdir(parents=True)
    captured = tmp_path / "captured-alert.txt"
    stub = install / "alert.sh"
    stub.write_text('#!/bin/bash\nprintf "%s" "$1" > "' + str(captured) + '"\n'
                    'echo "chat=$LOBSTER_ADMIN_CHAT_ID" >> "' + str(captured) + '"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    env = dict(
        os.environ,
        LOBSTER_INSTALL_DIR=str(tmp_path / "install"),
        LOBSTER_WORKSPACE=str(workspace),
        LOBSTER_CONFIG_DIR=str(tmp_path / "no-config"),
        DISK_ALERT_CHAT_ID=TEST_CHAT_ID,
        DISK_ALERT_STATE=str(tmp_path / "state"),
        DISK_ALERT_COOLDOWN_SECS=str(COOLDOWN_SECS),
    )
    return {"env": env, "captured": captured, "state": tmp_path / "state",
            "log": workspace / "logs" / "disk-usage-alert.log"}


def run(harness, threshold):
    env = dict(harness["env"], DISK_ALERT_THRESHOLD=str(threshold))
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)


def test_quiet_when_usage_is_under_threshold(harness):
    r = run(harness, NEVER_ABOVE)
    assert r.returncode == 0, r.stderr
    assert not harness["captured"].exists(), "no alert may be sent under the threshold"
    assert not harness["state"].exists()
    assert "ok:" in harness["log"].read_text()


def test_alerts_with_top_consumers_when_usage_exceeds_threshold(harness):
    r = run(harness, ALWAYS_ABOVE)
    assert r.returncode == 0, r.stderr
    body = harness["captured"].read_text()
    assert "Root disk at" in body and f"threshold {ALWAYS_ABOVE}%" in body
    assert "Top 5 consumers:" in body
    assert f"chat={TEST_CHAT_ID}" in body, "alert must go to the configured admin chat"
    assert harness["state"].read_text().strip().isdigit(), "last-alert epoch recorded"


def test_second_alert_is_suppressed_during_cooldown(harness):
    run(harness, ALWAYS_ABOVE)
    harness["captured"].unlink()
    r = run(harness, ALWAYS_ABOVE)
    assert r.returncode == 0, r.stderr
    assert not harness["captured"].exists(), "a second alert inside the cooldown must be suppressed"
    assert "in cooldown" in harness["log"].read_text()


def test_alerts_again_once_cooldown_has_expired(harness):
    harness["state"].write_text(str(int(time.time()) - COOLDOWN_SECS - 1))
    run(harness, ALWAYS_ABOVE)
    assert harness["captured"].exists(), "an expired cooldown must not suppress the alert"
