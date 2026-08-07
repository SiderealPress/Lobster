"""Regression tests: the dispatcher's own agent_sessions.db row must never be
classified as a ghost by scripts/agent-monitor.py (issue #2176).

Root cause (verified against PR #2152's diff and git history — see issue
#2176 for the full writeup): #2152 added a PID-ground-truth classification
path to classify() that returns GHOST_CONFIRMED whenever a row's recorded
`pid` is not alive — evaluated *before* the legacy output_file heuristic that
previously made it structurally impossible for the dispatcher's row (which
always has output_file=NULL) to land anywhere but STALE_NO_FILE. The existing
dispatcher guard in mark_failed_all_ghosts() only filters the stale_no_file
list (by a static agent_id string match) — it was never extended to the new
GHOST_CONFIRMED path, and send_alert() has no dispatcher guard at all.

This is the 5th occurrence of the recurring "dispatcher exclusion" bug class
documented in docs/engineering-lessons-learned.md (issue #781, PR #2099,
PR #2103). The fix applies the same established pattern used everywhere else
in the codebase: exclude agent_type='dispatcher' rows at the query boundary
(utils.agent_types.DISPATCHER_EXCLUSION_SQL), in load_running_agents() —
mirroring session_store.cleanup_stale_running_sessions()'s query exactly.

These tests reproduce the exact restart-race scenario: a dispatcher row with
a real, confirmed-dead PID (the state that exists in the gap between the old
dispatcher process dying and the new session re-registering) must never reach
classify_agent(), and therefore never GHOST_CONFIRMED, regardless of the PID
ground truth path.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# agent-monitor.py has a hyphenated filename so it can't be imported via
# normal sys.path manipulation. Use importlib to load it directly (same
# pattern as the sibling test files in this directory).
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "agent-monitor.py"
_spec = importlib.util.spec_from_file_location("ghost_detector_dispatcher_exclusion", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
gd = importlib.util.module_from_spec(_spec)
sys.modules["ghost_detector_dispatcher_exclusion"] = gd
_spec.loader.exec_module(gd)  # type: ignore[arg-type]

NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def _spawn_and_kill_pid() -> int:
    """Spawn a real subprocess, kill and reap it, return its now-dead PID.

    Used to simulate the exact restart-race state: the dispatcher's DB row
    still recording the OLD process's PID after that process has actually
    exited, before the new session has re-registered with a fresh PID.
    """
    proc = subprocess.Popen(["sleep", "30"])
    pid = proc.pid
    proc.kill()
    proc.wait()
    return pid


def _make_agent_sessions_db(db_path: Path) -> None:
    """Create a minimal agent_sessions table with exactly the columns
    load_running_agents() queries, matching the real schema's column names.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE agent_sessions (
            id             TEXT PRIMARY KEY,
            task_id        TEXT,
            agent_type     TEXT,
            description    TEXT,
            chat_id        TEXT,
            status         TEXT NOT NULL DEFAULT 'running',
            spawned_at     TEXT,
            output_file    TEXT,
            last_seen_at   TEXT,
            pid            INTEGER,
            dispatcher_pid INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_row(
    db_path: Path,
    *,
    agent_id: str,
    agent_type: str | None,
    status: str = "running",
    spawned_at: str,
    output_file: str | None = None,
    pid: int | None = None,
    dispatcher_pid: int | None = None,
    description: str = "test row",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO agent_sessions
            (id, task_id, agent_type, description, chat_id, status,
             spawned_at, output_file, last_seen_at, pid, dispatcher_pid)
        VALUES (?, NULL, ?, ?, '12345', ?, ?, ?, NULL, ?, ?)
        """,
        (agent_id, agent_type, description, status, spawned_at, output_file, pid, dispatcher_pid),
    )
    conn.commit()
    conn.close()


class TestLoadRunningAgentsExcludesDispatcher:
    """load_running_agents() must exclude agent_type='dispatcher' rows at the
    query boundary — the same pattern session_store.cleanup_stale_running_sessions()
    already applies via DISPATCHER_EXCLUSION_SQL.
    """

    def test_dispatcher_row_excluded_even_with_dead_pid(self, tmp_path: Path) -> None:
        """The exact restart-race reproduction: dispatcher row + confirmed-dead
        real PID must not be returned by load_running_agents() at all.

        Pre-fix, this row would be returned, then classify_agent() would
        resolve pid_alive=False and classify() would return GHOST_CONFIRMED —
        the literal false ghost-kill alarm.
        """
        db_path = tmp_path / "agent_sessions.db"
        _make_agent_sessions_db(db_path)

        dead_pid = _spawn_and_kill_pid()
        _insert_row(
            db_path,
            agent_id="lobster-dispatcher",
            agent_type="dispatcher",
            spawned_at=(NOW - timedelta(minutes=1)).isoformat(),
            output_file=None,
            pid=dead_pid,
            description="Lobster dispatcher main loop",
        )

        rows = gd.load_running_agents(db_path)

        assert rows == [], (
            "the dispatcher's row must be excluded at the query boundary, "
            "regardless of whether its recorded pid is alive or dead"
        )

    def test_dispatcher_row_excluded_alongside_real_subagents(self, tmp_path: Path) -> None:
        """A dispatcher row and a genuine subagent row both exist — only the
        subagent row should be returned."""
        db_path = tmp_path / "agent_sessions.db"
        _make_agent_sessions_db(db_path)

        dead_pid = _spawn_and_kill_pid()
        _insert_row(
            db_path,
            agent_id="lobster-dispatcher",
            agent_type="dispatcher",
            spawned_at=(NOW - timedelta(minutes=1)).isoformat(),
            pid=dead_pid,
        )
        _insert_row(
            db_path,
            agent_id="real-subagent-001",
            agent_type="subagent",
            spawned_at=(NOW - timedelta(minutes=5)).isoformat(),
            output_file="/tmp/real-subagent-001.output",
        )

        rows = gd.load_running_agents(db_path)

        assert [r.agent_id for r in rows] == ["real-subagent-001"]

    def test_null_agent_type_row_still_included(self, tmp_path: Path) -> None:
        """Pre-migration rows with agent_type=NULL are not dispatcher rows and
        must still be returned (COALESCE guard in DISPATCHER_EXCLUSION_SQL)."""
        db_path = tmp_path / "agent_sessions.db"
        _make_agent_sessions_db(db_path)

        _insert_row(
            db_path,
            agent_id="legacy-row",
            agent_type=None,
            spawned_at=(NOW - timedelta(minutes=200)).isoformat(),
        )

        rows = gd.load_running_agents(db_path)

        assert [r.agent_id for r in rows] == ["legacy-row"]


class TestFullPipelineDispatcherRaceReproduction:
    """End-to-end reproduction of the restart race through the same sequence
    main() runs: load_running_agents() -> classify_agent() -> confirmed list.
    """

    def test_dispatcher_with_dead_pid_never_reaches_ghost_confirmed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "agent_sessions.db"
        _make_agent_sessions_db(db_path)

        dead_pid = _spawn_and_kill_pid()
        _insert_row(
            db_path,
            agent_id="lobster-dispatcher",
            agent_type="dispatcher",
            spawned_at=(NOW - timedelta(minutes=1)).isoformat(),
            pid=dead_pid,
        )

        running_agents = gd.load_running_agents(db_path)
        classified = [
            gd.classify_agent(row, NOW, threshold_minutes=90.0, output_file_threshold_minutes=10.0)
            for row in running_agents
        ]
        confirmed = [a for a in classified if a.classification == "GHOST_CONFIRMED"]

        assert confirmed == [], (
            "the dispatcher's own row must never appear in the GHOST_CONFIRMED "
            "list produced by the same pipeline main() runs"
        )


# ---------------------------------------------------------------------------
# Defense-in-depth: mark_failed_all_ghosts() and send_alert() must also guard
# the GHOST_CONFIRMED (`confirmed`) list directly, not just rely on
# load_running_agents() excluding the row upstream. Neither guard existed
# before issue #2176 — TestLiveDispatcherGuard in test_ghost_detector_filesystem.py
# only ever covered the STALE_NO_FILE list, and send_alert() had no dispatcher
# guard on any path. These callers are also reachable with a hand-built
# ClassifiedAgent list (as in these tests, and in any future call site that
# doesn't route through load_running_agents()), so the guard must hold even
# when agent_type is unset — matching the belt-and-suspenders agent_id check
# _is_dispatcher_agent() falls back to.
# ---------------------------------------------------------------------------


def _make_confirmed_dispatcher(agent_type: str | None = "dispatcher") -> "gd.ClassifiedAgent":
    row = gd.AgentRow(
        agent_id="lobster-dispatcher",
        task_id=None,
        description="Lobster dispatcher (registered by SessionStart hook)",
        chat_id="0",
        status="running",
        spawned_at="2026-03-15T11:59:00+00:00",
        output_file=None,
        last_seen_at=None,
        pid=999999,
        agent_type=agent_type,
    )
    return gd.ClassifiedAgent(
        row=row,
        classification="GHOST_CONFIRMED",
        age_minutes=1.0,
        output_file_age_minutes=None,
    )


def _make_confirmed_subagent(agent_id: str = "real-subagent-001") -> "gd.ClassifiedAgent":
    row = gd.AgentRow(
        agent_id=agent_id,
        task_id="some-task",
        description="a real dead subagent",
        chat_id="12345",
        status="running",
        spawned_at="2026-03-15T11:00:00+00:00",
        output_file="/tmp/does-not-exist.output",
        last_seen_at=None,
        pid=888888,
        agent_type="subagent",
    )
    return gd.ClassifiedAgent(
        row=row,
        classification="GHOST_CONFIRMED",
        age_minutes=60.0,
        output_file_age_minutes=120.0,
    )


class TestMarkFailedAllGhostsExcludesConfirmedDispatcher:
    """mark_failed_all_ghosts() must never mark the dispatcher's own row failed
    even when it arrives via the GHOST_CONFIRMED list (not just STALE_NO_FILE).
    """

    def test_dispatcher_confirmed_row_not_marked_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marked: list[str] = []
        monkeypatch.setattr(gd, "mark_agent_failed", lambda db_path, agent_id: marked.append(agent_id))
        monkeypatch.setattr(gd, "drop_inbox_message", lambda payload: None)

        fake_db = tmp_path / "agent_sessions.db"
        gd.mark_failed_all_ghosts([_make_confirmed_dispatcher()], fake_db)

        assert marked == []

    def test_real_ghost_still_marked_failed_alongside_dispatcher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marked: list[str] = []
        monkeypatch.setattr(gd, "mark_agent_failed", lambda db_path, agent_id: marked.append(agent_id))
        monkeypatch.setattr(gd, "drop_inbox_message", lambda payload: None)

        fake_db = tmp_path / "agent_sessions.db"
        gd.mark_failed_all_ghosts(
            [_make_confirmed_dispatcher(), _make_confirmed_subagent()], fake_db
        )

        assert marked == ["real-subagent-001"]

    def test_dispatcher_confirmed_row_excluded_even_without_agent_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt-and-suspenders: a hand-built row with agent_id='lobster-dispatcher'
        but no agent_type set (e.g. an older test, or a caller bypassing
        load_running_agents()) must still be excluded via the agent_id fallback.
        """
        marked: list[str] = []
        monkeypatch.setattr(gd, "mark_agent_failed", lambda db_path, agent_id: marked.append(agent_id))
        monkeypatch.setattr(gd, "drop_inbox_message", lambda payload: None)

        fake_db = tmp_path / "agent_sessions.db"
        gd.mark_failed_all_ghosts([_make_confirmed_dispatcher(agent_type=None)], fake_db)

        assert marked == []


class TestSendAlertExcludesConfirmedDispatcher:
    """send_alert() must never include the dispatcher's own row in a ghost
    alert, on the GHOST_CONFIRMED path (previously had zero dispatcher guard).
    """

    def test_dispatcher_only_confirmed_sends_no_alert(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # No delivery method configured — send_alert() falls through to the
        # stderr/"no delivery method" branch only if it decides to alert at
        # all. Assert it does *not* reach that branch's alert-text output.
        monkeypatch.delenv("LOBSTER_MCP_SOCKET", raising=False)
        monkeypatch.delenv("LOBSTER_INBOX_SOCKET", raising=False)
        alert_sh = gd.Path(__file__).parent  # any non-existent path stand-in
        monkeypatch.setattr(gd.Path, "exists", lambda self: False)

        gd.send_alert([_make_confirmed_dispatcher()], [], report="")

        captured = capsys.readouterr()
        assert "GHOST_CONFIRMED agent(s)" not in captured.err
        assert "lobster-dispatcher" not in captured.err

    def test_real_ghost_still_alerted_alongside_dispatcher(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.delenv("LOBSTER_MCP_SOCKET", raising=False)
        monkeypatch.delenv("LOBSTER_INBOX_SOCKET", raising=False)
        monkeypatch.setattr(gd.Path, "exists", lambda self: False)

        gd.send_alert(
            [_make_confirmed_dispatcher(), _make_confirmed_subagent()], [], report=""
        )

        captured = capsys.readouterr()
        assert "some-task" in captured.err  # the real ghost's task_id, from the alert line
        assert "lobster-dispatcher" not in captured.err
