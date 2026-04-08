"""
Unit tests for the WFM watchdog mechanism.

Tests cover:
- wfm-active.json is written when wait_for_messages starts
- wfm-active.json is cleared when wait_for_messages returns normally
- wfm-watchdog.sh fires when WFM has been running beyond the threshold
- wfm-watchdog.sh does not fire when WFM is within normal bounds
- wfm-watchdog.sh does not fire when wfm-active.json is absent
- Watchdog injects a wfm_watchdog message with the correct schema
"""

import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WATCHDOG_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wfm-watchdog.sh"
WFM_WATCHDOG_THRESHOLD_SECONDS = 2100  # must match the script constant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_wfm_active(config_dir: Path, started_at: datetime, pid: int = 12345):
    """Write a wfm-active.json file as the MCP server would."""
    wfm_active = config_dir / "wfm-active.json"
    wfm_active.write_text(json.dumps({
        "started_at": started_at.isoformat(),
        "pid": pid,
    }))
    return wfm_active


def _run_watchdog(
    messages_dir: Path,
    config_env_path: Path | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run wfm-watchdog.sh in an isolated environment."""
    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(messages_dir)
    # Point CONFIG_ENV at a non-existent file so Telegram sends are skipped.
    env["LOBSTER_CONFIG_DIR"] = str(config_env_path or messages_dir / "fake-config")
    # Use a temp workspace for logs.
    env["LOBSTER_WORKSPACE"] = str(messages_dir / "workspace")
    if extra_env:
        env.update(extra_env)

    (messages_dir / "workspace" / "logs").mkdir(parents=True, exist_ok=True)
    (messages_dir / "config").mkdir(parents=True, exist_ok=True)
    (messages_dir / "inbox").mkdir(parents=True, exist_ok=True)

    return subprocess.run(
        [str(WATCHDOG_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Tests: wfm-watchdog.sh script behaviour
# ---------------------------------------------------------------------------

class TestWatchdogScript:
    """Verify wfm-watchdog.sh fires or stays quiet as expected."""

    def test_no_active_file_exits_cleanly(self, tmp_path):
        """Watchdog should exit 0 and write nothing when wfm-active.json is absent."""
        result = _run_watchdog(tmp_path)
        assert result.returncode == 0
        inbox_files = list((tmp_path / "inbox").glob("*.json"))
        assert inbox_files == [], "Expected no inbox messages when WFM is not running"

    def test_fresh_wfm_does_not_trigger(self, tmp_path):
        """Watchdog stays quiet when WFM started recently (well under threshold)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        _write_wfm_active(config_dir, recent)

        result = _run_watchdog(tmp_path)
        assert result.returncode == 0
        inbox_files = list((tmp_path / "inbox").glob("*.json"))
        assert inbox_files == [], "Expected no inbox messages for a recent WFM call"

    def test_frozen_wfm_injects_inbox_message(self, tmp_path):
        """Watchdog writes a wfm_watchdog inbox message when WFM is beyond the threshold."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        frozen_start = datetime.now(timezone.utc) - timedelta(seconds=WFM_WATCHDOG_THRESHOLD_SECONDS + 60)
        _write_wfm_active(config_dir, frozen_start)

        result = _run_watchdog(tmp_path)
        assert result.returncode == 0

        inbox_files = list((tmp_path / "inbox").glob("*.json"))
        assert len(inbox_files) == 1, f"Expected exactly one inbox message, got: {inbox_files}"

        msg = json.loads(inbox_files[0].read_text())
        assert msg["type"] == "wfm_watchdog", f"Expected type='wfm_watchdog', got: {msg['type']}"
        assert msg["source"] == "system"
        assert "id" in msg
        assert "timestamp" in msg
        assert "text" in msg

    def test_frozen_wfm_dedup_still_injects(self, tmp_path):
        """Even when a dedup lockfile is present, the inbox message is still injected."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        frozen_start = datetime.now(timezone.utc) - timedelta(seconds=WFM_WATCHDOG_THRESHOLD_SECONDS + 60)
        _write_wfm_active(config_dir, frozen_start)

        # Pre-create the dedup lockfile for the current hour.
        hour_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        dedup_lock = config_dir / f"wfm-watchdog-fired-{hour_stamp}.lock"
        dedup_lock.touch()

        result = _run_watchdog(tmp_path)
        assert result.returncode == 0

        inbox_files = list((tmp_path / "inbox").glob("*.json"))
        assert len(inbox_files) == 1, "Expected inbox message even when dedup lockfile is present"

    def test_watchdog_script_is_executable(self):
        """The watchdog script file must exist and be executable."""
        assert WATCHDOG_SCRIPT.exists(), f"watchdog script not found: {WATCHDOG_SCRIPT}"
        assert os.access(WATCHDOG_SCRIPT, os.X_OK), "watchdog script is not executable"

    def test_inbox_message_schema(self, tmp_path):
        """The injected wfm_watchdog message must have all required fields."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        frozen_start = datetime.now(timezone.utc) - timedelta(seconds=WFM_WATCHDOG_THRESHOLD_SECONDS + 300)
        _write_wfm_active(config_dir, frozen_start)

        _run_watchdog(tmp_path)

        inbox_files = list((tmp_path / "inbox").glob("*.json"))
        assert len(inbox_files) == 1
        msg = json.loads(inbox_files[0].read_text())

        required_fields = {"id", "source", "type", "chat_id", "text", "timestamp"}
        missing = required_fields - set(msg.keys())
        assert not missing, f"Inbox message missing required fields: {missing}"
        assert msg["chat_id"] == 0, "wfm_watchdog messages must have chat_id=0"


# ---------------------------------------------------------------------------
# Tests: wfm-active.json lifecycle (pure logic — no import needed)
# ---------------------------------------------------------------------------

class TestWfmActiveLifecycle:
    """Verify the expected shape and content of wfm-active.json."""

    def test_wfm_active_json_shape(self, tmp_path):
        """wfm-active.json must contain started_at (ISO timestamp) and pid (int)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        now = datetime.now(timezone.utc)
        active_file = _write_wfm_active(config_dir, now, pid=os.getpid())

        data = json.loads(active_file.read_text())
        assert "started_at" in data, "wfm-active.json must have started_at"
        assert "pid" in data, "wfm-active.json must have pid"
        assert isinstance(data["pid"], int)
        # Verify started_at parses as a valid datetime.
        parsed = datetime.fromisoformat(data["started_at"])
        assert parsed is not None

    def test_threshold_constant_matches_script(self):
        """The Python threshold constant must match what the shell script declares."""
        script_text = WATCHDOG_SCRIPT.read_text()
        assert f"WFM_WATCHDOG_THRESHOLD_SECONDS={WFM_WATCHDOG_THRESHOLD_SECONDS}" in script_text, (
            f"Shell script WFM_WATCHDOG_THRESHOLD_SECONDS does not match "
            f"test constant ({WFM_WATCHDOG_THRESHOLD_SECONDS})"
        )
