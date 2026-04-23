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

TOCTOU fix (issue #1730):
- _clear_wfm_active_signal() writes a tombstone value ("exited") instead of
  deleting the file, ensuring the file is never absent between the health check's
  existence check and its read (closes the -f / cat race window).
- WFM_ACTIVE_TOMBSTONE constant is the string "exited"
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


# ---------------------------------------------------------------------------
# TOCTOU fix tests (issue #1730)
# ---------------------------------------------------------------------------

def test_wfm_active_tombstone_constant_is_exited(tmp_path):
    """WFM_ACTIVE_TOMBSTONE must be the string 'exited' — the health check parses
    this as a non-integer and treats it as absent, which is the correct semantic."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)
    assert server.WFM_ACTIVE_TOMBSTONE == "exited", (
        "WFM_ACTIVE_TOMBSTONE must be 'exited' — health check regex ^[0-9]+$ "
        "rejects it and treats it as absent (WFM not active)"
    )


def test_clear_wfm_active_signal_writes_tombstone_not_deletes(tmp_path):
    """TOCTOU fix: _clear_wfm_active_signal() must write a tombstone value,
    not delete the file. The file must still exist after clearing, containing
    the tombstone string — never absent."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    # Write a live signal first
    server._write_wfm_active_signal()
    assert wfm_file.exists(), "Precondition: WFM-active file must exist after signal write"

    # Clear it — must NOT delete, must write tombstone
    server._clear_wfm_active_signal()

    assert wfm_file.exists(), (
        "File must still exist after _clear_wfm_active_signal() — "
        "deleting it creates a TOCTOU race with the health check"
    )
    content = wfm_file.read_text().strip()
    assert content == server.WFM_ACTIVE_TOMBSTONE, (
        f"Expected tombstone '{server.WFM_ACTIVE_TOMBSTONE}', got '{content}'"
    )


def test_clear_wfm_active_signal_tombstone_is_non_integer(tmp_path):
    """The tombstone written by _clear_wfm_active_signal() must be a non-integer
    so the health check's regex guard ([[ \"$ts\" =~ ^[0-9]+$ ]]) rejects it and
    treats the file as absent — meaning WFM is not active."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    server._write_wfm_active_signal()
    server._clear_wfm_active_signal()

    content = wfm_file.read_text().strip()
    assert not content.isdigit(), (
        f"Tombstone '{content}' must not be a pure integer — "
        "the health check treats integers as live timestamps"
    )


def test_clear_wfm_active_signal_when_file_absent_is_silent(tmp_path):
    """_clear_wfm_active_signal() must not raise when the file does not yet exist
    (e.g. WFM exits before the first _write_wfm_active_signal() completes)."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    assert not wfm_file.exists(), "Precondition: file must not exist"
    # Must not raise
    server._clear_wfm_active_signal()


def test_clear_wfm_active_signal_silent_on_write_error(tmp_path, monkeypatch):
    """_clear_wfm_active_signal() swallows exceptions silently — never raises.
    This is critical: it runs in a finally block and must not mask the original exception."""
    wfm_file = tmp_path / "logs" / "dispatcher-wfm-active"
    server = _load_inbox_server(wfm_file)

    # Make the parent directory read-only to provoke a write failure
    wfm_file.parent.mkdir(parents=True, exist_ok=True)
    wfm_file.parent.chmod(0o555)
    try:
        # Must not raise
        server._clear_wfm_active_signal()
    finally:
        wfm_file.parent.chmod(0o755)
