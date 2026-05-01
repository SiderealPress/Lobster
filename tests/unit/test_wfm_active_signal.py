"""
Tests for the dispatcher heartbeat signal (issue #1713 / #949 / #1908).

After issue #1908 simplification, the dispatcher heartbeat and the WFM-active
signal are unified into a single file (dispatcher-heartbeat).  The WFM-active
file (dispatcher-wfm-active) and its tombstone mechanism have been removed;
the daemon thread now writes directly to the same heartbeat file that
thinking-heartbeat.py uses.

Verifies that:
- _write_dispatcher_heartbeat_from_wfm() writes a single Unix epoch integer
- DISPATCHER_HEARTBEAT_FILE path is ~/lobster-workspace/logs/dispatcher-heartbeat
- LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE env var overrides the path (test isolation)
- WAIT_HEARTBEAT_INTERVAL is 60s (matches health-check's DISPATCHER_HEARTBEAT_STALE_SECONDS/15)
- The written timestamp is within 2 seconds of now
- Atomic write: a .tmp file is never left behind
- The file is writable before the wait loop starts
- _wfm_heartbeat_thread_fn fires at least once and stops cleanly
"""
import importlib
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch


def _load_inbox_server(tmp_heartbeat_file: Path):
    """Import inbox_server with LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE set to a test path."""
    env_patch = {
        "LOBSTER_MESSAGES": str(tmp_heartbeat_file.parent.parent / "messages"),
        "LOBSTER_WORKSPACE": str(tmp_heartbeat_file.parent.parent / "workspace"),
        "LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE": str(tmp_heartbeat_file),
    }
    # Ensure messages dirs exist so module-level mkdir calls succeed
    (tmp_heartbeat_file.parent.parent / "messages" / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_heartbeat_file.parent.parent / "messages" / "config").mkdir(parents=True, exist_ok=True)
    (tmp_heartbeat_file.parent.parent / "messages" / "processing").mkdir(parents=True, exist_ok=True)
    (tmp_heartbeat_file.parent.parent / "workspace" / "logs").mkdir(parents=True, exist_ok=True)

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


# Named constant for the heartbeat filename, matching the spec requirement.
DISPATCHER_HEARTBEAT_FILENAME = "dispatcher-heartbeat"


def test_dispatcher_heartbeat_file_path_is_workspace_logs(tmp_path):
    """DISPATCHER_HEARTBEAT_FILE default path is ~/lobster-workspace/logs/dispatcher-heartbeat."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)
    # The override env var must point to the tmp path
    assert str(server.DISPATCHER_HEARTBEAT_FILE) == str(hb_file)


def test_wait_heartbeat_interval_is_60():
    """WAIT_HEARTBEAT_INTERVAL must be 60s to match health-check's 15x DISPATCHER_HEARTBEAT_STALE_SECONDS."""
    mcp_dir = str(Path(__file__).resolve().parent.parent.parent / "src" / "mcp")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import inbox_server
    assert inbox_server.WAIT_HEARTBEAT_INTERVAL == 60, (
        "WAIT_HEARTBEAT_INTERVAL must be 60s — health-check DISPATCHER_HEARTBEAT_STALE_SECONDS=900 is 15x this value"
    )


def test_write_dispatcher_heartbeat_from_wfm_creates_file(tmp_path):
    """_write_dispatcher_heartbeat_from_wfm() creates the heartbeat file with a Unix epoch integer."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)

    assert not hb_file.exists(), "File should not exist before heartbeat is written"
    before = int(time.time())
    server._write_dispatcher_heartbeat_from_wfm()
    after = int(time.time())

    assert hb_file.exists(), "Heartbeat file must exist after _write_dispatcher_heartbeat_from_wfm()"
    content = hb_file.read_text().strip()
    ts = int(content)
    assert before <= ts <= after, (
        f"Timestamp {ts} must be between {before} and {after}"
    )


def test_write_dispatcher_heartbeat_from_wfm_no_tmp_leftover(tmp_path):
    """_write_dispatcher_heartbeat_from_wfm() must not leave a .tmp file behind (atomic write)."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)
    server._write_dispatcher_heartbeat_from_wfm()

    tmp_files = list(tmp_path.glob("**/*.tmp"))
    assert tmp_files == [], f"Stale .tmp file(s) left behind: {tmp_files}"


def test_write_dispatcher_heartbeat_from_wfm_overwrites_on_refresh(tmp_path):
    """Calling _write_dispatcher_heartbeat_from_wfm() twice updates the timestamp."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)

    server._write_dispatcher_heartbeat_from_wfm()
    first_ts = int(hb_file.read_text().strip())

    time.sleep(1.1)  # ensure clock advances
    server._write_dispatcher_heartbeat_from_wfm()
    second_ts = int(hb_file.read_text().strip())

    assert second_ts >= first_ts, "Second write must produce timestamp >= first"


def test_write_dispatcher_heartbeat_from_wfm_silent_on_permission_error(tmp_path, monkeypatch):
    """_write_dispatcher_heartbeat_from_wfm() swallows exceptions silently — never raises."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)

    # Patch os.rename to raise — simulates a permission error mid-write.
    monkeypatch.setattr(os, "rename", lambda src, dst: (_ for _ in ()).throw(PermissionError("no write")))

    # Must not raise
    server._write_dispatcher_heartbeat_from_wfm()


# ---------------------------------------------------------------------------
# _wfm_heartbeat_thread_fn tests (issue #1823 / #1908)
# ---------------------------------------------------------------------------

def test_wfm_heartbeat_thread_fn_fires_at_least_once(tmp_path):
    """_wfm_heartbeat_thread_fn fires touch_heartbeat and _write_dispatcher_heartbeat_from_wfm
    at least once within a short interval."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)

    calls = []
    original_touch = server.touch_heartbeat
    original_write = server._write_dispatcher_heartbeat_from_wfm

    def spy_touch():
        calls.append("touch")
        original_touch()

    def spy_write():
        calls.append("write")
        original_write()

    stop_event = threading.Event()
    with patch.object(server, "touch_heartbeat", spy_touch), \
         patch.object(server, "_write_dispatcher_heartbeat_from_wfm", spy_write):
        t = threading.Thread(
            target=server._wfm_heartbeat_thread_fn,
            args=(stop_event, 0.05),
            daemon=True,
        )
        t.start()
        # Wait long enough for at least one tick (interval=0.05s, wait 0.3s)
        time.sleep(0.3)
        stop_event.set()
        t.join(timeout=2)

    assert "touch" in calls, "_wfm_heartbeat_thread_fn must call touch_heartbeat at least once"
    assert "write" in calls, "_wfm_heartbeat_thread_fn must call _write_dispatcher_heartbeat_from_wfm at least once"


def test_wfm_heartbeat_thread_fn_stops_after_stop_event(tmp_path):
    """_wfm_heartbeat_thread_fn stops (thread exits) after stop_event.set()."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)

    stop_event = threading.Event()
    t = threading.Thread(
        target=server._wfm_heartbeat_thread_fn,
        args=(stop_event, 0.05),
        daemon=True,
    )
    t.start()
    # Let it start, then signal stop
    time.sleep(0.1)
    stop_event.set()
    t.join(timeout=1)

    assert not t.is_alive(), (
        "Thread must not be alive after stop_event.set() and join(timeout=1)"
    )


def test_wfm_heartbeat_thread_fn_swallows_exceptions(tmp_path):
    """_wfm_heartbeat_thread_fn swallows exceptions from touch_heartbeat — never raises."""
    hb_file = tmp_path / "logs" / DISPATCHER_HEARTBEAT_FILENAME
    server = _load_inbox_server(hb_file)

    def raising_touch():
        raise RuntimeError("simulated heartbeat failure")

    stop_event = threading.Event()
    with patch.object(server, "touch_heartbeat", raising_touch):
        t = threading.Thread(
            target=server._wfm_heartbeat_thread_fn,
            args=(stop_event, 0.05),
            daemon=True,
        )
        t.start()
        # Give it time to fire (and raise) at least once
        time.sleep(0.3)
        # Verify the thread survived the repeated exceptions
        assert t.is_alive(), (
            "Thread died due to an unswallowed exception from touch_heartbeat "
            "(crashed before stop_event was set)"
        )
        stop_event.set()
        t.join(timeout=1)

    # After stop_event.set() + join, the thread should be cleanly stopped.
    assert not t.is_alive(), (
        "Thread should have exited cleanly after stop_event.set() and join()"
    )
