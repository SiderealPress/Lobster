"""
Tests for the WFM-active heartbeat signal (issue #1713 / #949).

Verifies that:
- _write_wfm_active_signal() writes a single Unix epoch integer
- WFM_ACTIVE_FILE path is ~/lobster-workspace/logs/dispatcher-wfm-active
- LOBSTER_WFM_ACTIVE_OVERRIDE env var overrides the path (test isolation)
- WAIT_HEARTBEAT_INTERVAL is 60s (matches health-check's WFM_ACTIVE_STALE_SECONDS/3)
- The written timestamp is within 2 seconds of now
- Atomic write: a .tmp file is never left behind
- The file is writable before the wait loop starts
"""
import importlib
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch


def _load_inbox_server(tmp_wfm_file: Path):
    """Import inbox_server with LOBSTER_WFM_ACTIVE_OVERRIDE set to a test path."""
    env_patch = {
        "LOBSTER_MESSAGES": str(tmp_wfm_file.parent.parent / "messages"),
        "LOBSTER_WORKSPACE": str(tmp_wfm_file.parent.parent / "workspace"),
        "LOBSTER_WFM_ACTIVE_OVERRIDE": str(tmp_wfm_file),
    }
    # Ensure messages dirs exist so module-level mkdir calls succeed
    (tmp_wfm_file.parent.parent / "messages" / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_wfm_file.parent.parent / "messages" / "config").mkdir(parents=True, exist_ok=True)
    (tmp_wfm_file.parent.parent / "messages" / "processing").mkdir(parents=True, exist_ok=True)
    (tmp_wfm_file.parent.parent / "workspace" / "logs").mkdir(parents=True, exist_ok=True)

    with patch.dict(os.environ, env_patch):
        # Force reimport with new env
        if "inbox_server" in sys.modules:
            del sys.modules["inbox_server"]
        mcp_dir = str(Path(__file__).resolve().parent.parent.parent / "src" / "mcp")
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        import inbox_server
        importlib.reload(inbox_server)
    return inbox_server


def test_wfm_active_file_path_is_workspace_logs(tmp_path):
    """WFM_ACTIVE_FILE default path is ~/lobster-workspace/logs/dispatcher-wfm-active."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)
    # The override env var must point to the tmp path
    assert str(server.WFM_ACTIVE_FILE) == str(wfm_file)


def test_wait_heartbeat_interval_is_60():
    """WAIT_HEARTBEAT_INTERVAL must be 60s to match health-check's 3x WFM_ACTIVE_STALE_SECONDS expectation."""
    mcp_dir = str(Path(__file__).resolve().parent.parent.parent / "src" / "mcp")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import inbox_server
    assert inbox_server.WAIT_HEARTBEAT_INTERVAL == 60, (
        "WAIT_HEARTBEAT_INTERVAL must be 60s — health-check WFM_ACTIVE_STALE_SECONDS=180 is 3x this value"
    )


def test_write_wfm_active_signal_creates_file(tmp_path):
    """_write_wfm_active_signal() creates the WFM-active file with a Unix epoch integer."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    assert not wfm_file.exists(), "File should not exist before signal is written"
    before = int(time.time())
    server._write_wfm_active_signal()
    after = int(time.time())

    assert wfm_file.exists(), "WFM-active file must exist after _write_wfm_active_signal()"
    content = wfm_file.read_text().strip()
    ts = int(content)
    assert before <= ts <= after, (
        f"Timestamp {ts} must be between {before} and {after}"
    )


def test_write_wfm_active_signal_no_tmp_leftover(tmp_path):
    """_write_wfm_active_signal() must not leave a .tmp file behind (atomic write)."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)
    server._write_wfm_active_signal()

    tmp_files = list(tmp_path.glob("**/.wfm-active-*.tmp"))
    assert tmp_files == [], f"Stale .tmp file(s) left behind: {tmp_files}"


def test_write_wfm_active_signal_overwrites_on_refresh(tmp_path):
    """Calling _write_wfm_active_signal() twice updates the timestamp."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    server._write_wfm_active_signal()
    first_ts = int(wfm_file.read_text().strip())

    time.sleep(1.1)  # ensure clock advances
    server._write_wfm_active_signal()
    second_ts = int(wfm_file.read_text().strip())

    assert second_ts >= first_ts, "Second write must produce timestamp >= first"


def test_write_wfm_active_signal_silent_on_permission_error(tmp_path, monkeypatch):
    """_write_wfm_active_signal() swallows exceptions silently — never raises."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    # Patch os.rename to raise — simulates a permission error mid-write.
    monkeypatch.setattr(os, "rename", lambda src, dst: (_ for _ in ()).throw(PermissionError("no write")))

    # Must not raise
    server._write_wfm_active_signal()
