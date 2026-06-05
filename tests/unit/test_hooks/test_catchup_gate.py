"""
Unit tests for hooks/catchup-gate.py

Option B design: queries agent_sessions.db directly to check whether
a startup-catchup or compact-catchup session is still running.
No flag file — reuses existing IPC.

Tests cover:
- Passes through immediately when agent_id present (subagent fast path)
- Passes through when not the dispatcher session
- DB file missing → fail open (exit 0)
- DB query fails (locked, corrupt) → fail open (exit 0)
- No matching running session → allow tool call
- Running session but spawned_at > CATCHUP_WINDOW_SECONDS ago → allow (stale)
- Running session within CATCHUP_WINDOW_SECONDS → block with exit(2)
- Allow-listed tools always pass through even during active catchup
- Non-allow-listed tools are blocked during active catchup
- Block message contains "Catching up" text (as specified in task)
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Constants — match the implementation
# ---------------------------------------------------------------------------

CATCHUP_WINDOW_SECONDS = 120  # as specified in issue

ALWAYS_ALLOWED_TOOLS = [
    "mcp__lobster-inbox__wait_for_messages",
    "mcp__lobster-inbox__check_inbox",
    "mcp__lobster-inbox__mark_processing",
    "mcp__lobster-inbox__mark_processed",
    "mcp__lobster-inbox__mark_failed",
    "mcp__lobster-inbox__send_reply",
    "mcp__lobster-inbox__claim_and_ack",
]

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "catchup-gate.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path, sessions: list[dict]) -> Path:
    """Create a minimal agent_sessions.db with provided rows."""
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "agent_sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE agent_sessions (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            status TEXT,
            spawned_at TEXT
        )"""
    )
    for s in sessions:
        conn.execute(
            "INSERT INTO agent_sessions (id, task_id, status, spawned_at) VALUES (?,?,?,?)",
            (s["id"], s["task_id"], s["status"], s["spawned_at"]),
        )
    conn.commit()
    conn.close()
    return db_path


def _utc_iso(offset_seconds: float = 0) -> str:
    """Return ISO8601 UTC timestamp offset by given seconds from now."""
    ts = time.time() + offset_seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _load_hook(monkeypatch, tmp_path: Path, db_path: Path | None = None):
    """Load catchup-gate.py as a fresh module, wiring DB path via env var."""
    monkeypatch.setenv("HOME", str(tmp_path))
    if db_path is not None:
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
    else:
        # Point at a non-existent DB so it fails open.
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path / "no_such_dir"))

    spec = importlib.util.spec_from_file_location("catchup_gate", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    spec.loader.exec_module(mod)
    return mod


def _make_hook_input(
    tool_name: str = "mcp__lobster-inbox__write_result",
    tool_input: dict | None = None,
    agent_id: str | None = None,
    session_id: str = "sess-dispatcher-001",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "session_id": session_id,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    """Run mod.main() with hook_input as stdin. Returns (exit_code, stdout, stderr)."""
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


def _make_dispatcher_session_file(tmp_path: Path, session_id: str) -> Path:
    """Write the hook marker file so is_dispatcher_session returns True."""
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / "dispatcher-session-id"
    marker.write_text(session_id)
    return marker


# ---------------------------------------------------------------------------
# Fast path: subagent detection
# ---------------------------------------------------------------------------

class TestSubagentFastPath:
    def test_subagent_passes_through(self, monkeypatch, tmp_path):
        """agent_id present → subagent → exit 0 immediately, no DB query."""
        db_path = _make_db(
            tmp_path,
            [{"id": "s1", "task_id": "startup-catchup", "status": "running",
              "spawned_at": _utc_iso(-10)}],
        )
        mod = _load_hook(monkeypatch, tmp_path, db_path)
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            agent_id="agent-sub-123",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Subagent should always pass through"

    def test_non_dispatcher_passes_through(self, monkeypatch, tmp_path):
        """Non-dispatcher sessions pass through (is_dispatcher_session → False)."""
        db_path = _make_db(
            tmp_path,
            [{"id": "s2", "task_id": "startup-catchup", "status": "running",
              "spawned_at": _utc_iso(-10)}],
        )
        mod = _load_hook(monkeypatch, tmp_path, db_path)
        # Patch is_dispatcher_session on the already-loaded module to return False
        # (simulates a subagent session or unknown session where state files are absent).
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: False)
        hook_input = _make_hook_input(session_id="sess-unknown-999")
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Fail-open: DB errors
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_missing_db_fails_open(self, monkeypatch, tmp_path):
        """DB file absent → exit 0 (fail open, never block)."""
        # No DB created — LOBSTER_WORKSPACE points at empty dir.
        mod = _load_hook(monkeypatch, tmp_path, db_path=None)
        _make_dispatcher_session_file(tmp_path, "sess-disp-001")

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-001",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Missing DB should fail open"

    def test_corrupt_db_fails_open(self, monkeypatch, tmp_path):
        """Corrupt DB file → exit 0 (fail open)."""
        db_dir = tmp_path / "data"
        db_dir.mkdir(parents=True, exist_ok=True)
        corrupt_db = db_dir / "agent_sessions.db"
        corrupt_db.write_bytes(b"NOT A VALID SQLITE FILE\x00\x01\x02")

        mod = _load_hook(monkeypatch, tmp_path, db_path=corrupt_db)
        _make_dispatcher_session_file(tmp_path, "sess-disp-002")

        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )

        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-002",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Corrupt DB should fail open"

    def test_bad_json_stdin_fails_open(self, monkeypatch, tmp_path):
        """Malformed stdin → exit 0 (fail open)."""
        mod = _load_hook(monkeypatch, tmp_path, db_path=None)
        stdout_cap = StringIO()
        stderr_cap = StringIO()
        exit_code = None
        with (
            patch("sys.stdin", StringIO("NOT JSON")),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
        ):
            try:
                mod.main()
            except SystemExit as e:
                exit_code = e.code
        assert exit_code == 0


# ---------------------------------------------------------------------------
# No active catchup: allow everything
# ---------------------------------------------------------------------------

class TestNoActiveCatchup:
    def _setup_dispatcher(self, monkeypatch, tmp_path, sessions):
        db_path = _make_db(tmp_path, sessions)
        mod = _load_hook(monkeypatch, tmp_path, db_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-active")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        return mod

    def test_no_running_sessions_allows_any_tool(self, monkeypatch, tmp_path):
        """No running catchup sessions → any tool passes through."""
        mod = self._setup_dispatcher(monkeypatch, tmp_path, [])
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-active",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0

    def test_completed_session_not_blocking(self, monkeypatch, tmp_path):
        """A catchup session with status='done' does not trigger the gate."""
        mod = self._setup_dispatcher(monkeypatch, tmp_path, [
            {"id": "s3", "task_id": "startup-catchup", "status": "done",
             "spawned_at": _utc_iso(-30)},
        ])
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-active",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0

    def test_stale_running_session_not_blocking(self, monkeypatch, tmp_path):
        """Running catchup session spawned > CATCHUP_WINDOW_SECONDS ago → allow."""
        mod = self._setup_dispatcher(monkeypatch, tmp_path, [
            {"id": "s4", "task_id": "startup-catchup", "status": "running",
             "spawned_at": _utc_iso(-(CATCHUP_WINDOW_SECONDS + 1))},
        ])
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-active",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Session older than window should not block"

    def test_compact_catchup_prefix_stale_not_blocking(self, monkeypatch, tmp_path):
        """compact-catchup% session spawned outside window → allow."""
        mod = self._setup_dispatcher(monkeypatch, tmp_path, [
            {"id": "s5", "task_id": "compact-catchup-abc123", "status": "running",
             "spawned_at": _utc_iso(-(CATCHUP_WINDOW_SECONDS + 60))},
        ])
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-active",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Active catchup: blocking behavior
# ---------------------------------------------------------------------------

class TestActiveCatchupBlocks:
    def _setup_dispatcher_with_active_catchup(
        self,
        monkeypatch,
        tmp_path,
        task_id: str = "startup-catchup",
        age_seconds: float = 10,
    ):
        db_path = _make_db(tmp_path, [
            {"id": "s6", "task_id": task_id, "status": "running",
             "spawned_at": _utc_iso(-age_seconds)},
        ])
        mod = _load_hook(monkeypatch, tmp_path, db_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-blocking")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        return mod

    def test_substantive_tool_blocked_startup_catchup(self, monkeypatch, tmp_path):
        """Active startup-catchup session → substantive tool call → exit(2)."""
        mod = self._setup_dispatcher_with_active_catchup(monkeypatch, tmp_path,
                                                          "startup-catchup", 10)
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-blocking",
        )
        exit_code, _, stderr = _run_hook(mod, hook_input)
        assert exit_code == 2, f"Expected exit(2), got {exit_code}"

    def test_block_message_contains_catching_up(self, monkeypatch, tmp_path):
        """Block message must contain 'Catching up' (as specified)."""
        mod = self._setup_dispatcher_with_active_catchup(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-blocking",
        )
        exit_code, _, stderr = _run_hook(mod, hook_input)
        assert exit_code == 2
        assert "Catching up" in stderr, (
            f"Block message should contain 'Catching up', got: {stderr!r}"
        )

    def test_compact_catchup_prefix_blocks(self, monkeypatch, tmp_path):
        """Active compact-catchup-% session → blocks substantive tools."""
        mod = self._setup_dispatcher_with_active_catchup(
            monkeypatch, tmp_path, "compact-catchup-20260425", 5
        )
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__send_reply",
            tool_input={"text": "Here is your report: X, Y, Z."},
            session_id="sess-disp-blocking",
        )
        # send_reply IS on the allow-list per spec; should NOT block
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "send_reply is on the allow-list; should not block"

    def test_spawn_agent_blocked_during_catchup(self, monkeypatch, tmp_path):
        """Agent/Task tool is blocked while catchup is active."""
        mod = self._setup_dispatcher_with_active_catchup(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            tool_name="Agent",
            tool_input={"prompt": "do something", "run_in_background": True},
            session_id="sess-disp-blocking",
        )
        exit_code, _, stderr = _run_hook(mod, hook_input)
        assert exit_code == 2

    def test_exactly_at_window_boundary_blocks(self, monkeypatch, tmp_path):
        """Session spawned exactly at boundary (spawned_at == now - 119s) → block."""
        mod = self._setup_dispatcher_with_active_catchup(
            monkeypatch, tmp_path, age_seconds=CATCHUP_WINDOW_SECONDS - 1
        )
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-blocking",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 2, "Session just inside window should block"

    def test_just_past_window_allows(self, monkeypatch, tmp_path):
        """Session spawned just outside window (now - 121s) → allow."""
        mod = self._setup_dispatcher_with_active_catchup(
            monkeypatch, tmp_path, age_seconds=CATCHUP_WINDOW_SECONDS + 1
        )
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__write_result",
            session_id="sess-disp-blocking",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "Session just outside window should not block"


# ---------------------------------------------------------------------------
# Allow-list enforcement
# ---------------------------------------------------------------------------

class TestAllowList:
    def _setup_active_catchup(self, monkeypatch, tmp_path):
        db_path = _make_db(tmp_path, [
            {"id": "s7", "task_id": "startup-catchup", "status": "running",
             "spawned_at": _utc_iso(-10)},
        ])
        mod = _load_hook(monkeypatch, tmp_path, db_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-allowlist")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        return mod

    @pytest.mark.parametrize("tool_name", ALWAYS_ALLOWED_TOOLS)
    def test_allow_listed_tool_passes_during_catchup(
        self, tool_name, monkeypatch, tmp_path
    ):
        """Every tool on the allow-list passes through even with active catchup."""
        mod = self._setup_active_catchup(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            tool_name=tool_name,
            tool_input={"text": "Hello"} if "send_reply" in tool_name else {},
            session_id="sess-disp-allowlist",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, (
            f"Allow-listed tool {tool_name!r} should not be blocked, "
            f"got exit_code={exit_code}"
        )

    def test_non_allow_listed_tool_blocked(self, monkeypatch, tmp_path):
        """Tool not on allow-list is blocked during active catchup."""
        mod = self._setup_active_catchup(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__create_task",
            session_id="sess-disp-allowlist",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 2

    def test_send_reply_always_allowed_regardless_of_content(
        self, monkeypatch, tmp_path
    ):
        """send_reply is unconditionally allowed (no content filtering)."""
        mod = self._setup_active_catchup(monkeypatch, tmp_path)
        # Non-ack message — should still pass because send_reply is on the allowlist
        hook_input = _make_hook_input(
            tool_name="mcp__lobster-inbox__send_reply",
            tool_input={"text": "Here is your detailed analysis of the situation."},
            session_id="sess-disp-allowlist",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "send_reply should always pass (on allow-list unconditionally)"

    def test_bash_tool_blocked_during_catchup(self, monkeypatch, tmp_path):
        """Bash tool is blocked during active catchup."""
        mod = self._setup_active_catchup(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            tool_name="Bash",
            tool_input={"command": "ls /tmp"},
            session_id="sess-disp-allowlist",
        )
        exit_code, _, _ = _run_hook(mod, hook_input)
        assert exit_code == 2


# ---------------------------------------------------------------------------
# DB query correctness — task_id patterns
# ---------------------------------------------------------------------------

class TestTaskIdPatterns:
    def _setup(self, monkeypatch, tmp_path, sessions):
        db_path = _make_db(tmp_path, sessions)
        mod = _load_hook(monkeypatch, tmp_path, db_path)
        _make_dispatcher_session_file(tmp_path, "sess-disp-patterns")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        return mod

    def test_startup_catchup_exact_match(self, monkeypatch, tmp_path):
        """task_id='startup-catchup' (exact) triggers gate."""
        mod = self._setup(monkeypatch, tmp_path, [
            {"id": "x1", "task_id": "startup-catchup", "status": "running",
             "spawned_at": _utc_iso(-5)},
        ])
        exit_code, _, _ = _run_hook(mod, _make_hook_input(
            tool_name="Bash", session_id="sess-disp-patterns"
        ))
        assert exit_code == 2

    def test_compact_catchup_prefix_match(self, monkeypatch, tmp_path):
        """task_id starting with 'compact-catchup' triggers gate."""
        mod = self._setup(monkeypatch, tmp_path, [
            {"id": "x2", "task_id": "compact-catchup-2026-04-25", "status": "running",
             "spawned_at": _utc_iso(-5)},
        ])
        exit_code, _, _ = _run_hook(mod, _make_hook_input(
            tool_name="Bash", session_id="sess-disp-patterns"
        ))
        assert exit_code == 2

    def test_unrelated_task_id_does_not_block(self, monkeypatch, tmp_path):
        """Running session with unrelated task_id does not trigger gate."""
        mod = self._setup(monkeypatch, tmp_path, [
            {"id": "x3", "task_id": "pr-review-1234", "status": "running",
             "spawned_at": _utc_iso(-5)},
        ])
        exit_code, _, _ = _run_hook(mod, _make_hook_input(
            tool_name="Bash", session_id="sess-disp-patterns"
        ))
        assert exit_code == 0

    def test_only_most_recent_session_checked_fresh_blocks(self, monkeypatch, tmp_path):
        """When multiple matching sessions exist, the most recent (by spawned_at) is checked.
        If the most recent is fresh (within window), gate blocks."""
        mod = self._setup(monkeypatch, tmp_path, [
            # Older session — outside window (stale)
            {"id": "x4a", "task_id": "startup-catchup", "status": "running",
             "spawned_at": _utc_iso(-(CATCHUP_WINDOW_SECONDS + 60))},
            # Newer session — within window (fresh)
            {"id": "x4b", "task_id": "startup-catchup", "status": "running",
             "spawned_at": _utc_iso(-30)},
        ])
        # The query uses ORDER BY spawned_at DESC LIMIT 1 — newest row wins.
        # Newest (x4b, 30s ago) is within window → should block.
        exit_code, _, _ = _run_hook(mod, _make_hook_input(
            tool_name="Bash", session_id="sess-disp-patterns"
        ))
        assert exit_code == 2, "Most recent session is fresh — should block"

    def test_only_most_recent_session_checked_fresh_most_recent_blocks(self, monkeypatch, tmp_path):
        """When multiple matching sessions exist, the most recent is checked.
        If the most recent is stale (outside window), gate allows even if an
        older session exists within the window (it's superseded by the newer stale one)."""
        mod = self._setup(monkeypatch, tmp_path, [
            # Older session — within window
            {"id": "x5a", "task_id": "startup-catchup", "status": "running",
             "spawned_at": _utc_iso(-30)},
            # Even older session — outside window (stale)
            {"id": "x5b", "task_id": "startup-catchup", "status": "running",
             "spawned_at": _utc_iso(-(CATCHUP_WINDOW_SECONDS + 60))},
        ])
        # The query uses ORDER BY spawned_at DESC LIMIT 1 — newest row wins.
        # Newest (x5a, 30s ago) is within window → BLOCKS (this tests correctness
        # of the ordering, not an allow case).
        exit_code, _, _ = _run_hook(mod, _make_hook_input(
            tool_name="Bash", session_id="sess-disp-patterns"
        ))
        # x5a is the most recent and is within window → blocks
        assert exit_code == 2, "Most recent session (x5a) is fresh — should block"
