"""Unit tests for PID ground truth in scripts/agent-monitor.py's classify() (issue #2148, Phase 1).

Mirrors the acceptance criteria already covered for session_store.cleanup_stale_running_sessions()
in tests/test_agent_sessions.py:
  - a confirmed-dead pid overrides the age/output-file heuristics → GHOST_CONFIRMED
  - a confirmed-live pid overrides a stale/missing output file → HEALTHY
  - a confirmed-dead dispatcher_pid marks an in-process subagent row GHOST_CONFIRMED
  - a confirmed-live dispatcher_pid does NOT itself prove aliveness — falls back
    to the existing heuristics unchanged
  - zero-pid (pre-migration) rows behave byte-for-byte identically to before

classify() itself stays a pure function (takes precomputed pid_alive/
dispatcher_pid_alive booleans, no OS calls) — classify_agent() is the boundary
that resolves real PIDs via agents.pid_liveness.is_pid_alive(). Both layers are
tested here: classify() with synthetic booleans, and classify_agent() with real
spawned/killed subprocesses for the full acceptance-criteria proof.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# agent-monitor.py has a hyphenated filename so it can't be imported via
# normal sys.path manipulation. Use importlib to load it directly (same
# pattern as tests/unit/test_ghost_detector_filesystem.py).
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "agent-monitor.py"
_spec = importlib.util.spec_from_file_location("agent_monitor_pid_liveness", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
gd = importlib.util.module_from_spec(_spec)
sys.modules["agent_monitor_pid_liveness"] = gd
_spec.loader.exec_module(gd)  # type: ignore[arg-type]

NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def _spawn_and_kill_pid() -> int:
    """Spawn a real subprocess, kill and reap it, return its now-dead PID."""
    proc = subprocess.Popen(["sleep", "30"])
    pid = proc.pid
    proc.kill()
    proc.wait()
    return pid


def _make_row(agent_id: str, **overrides) -> "gd.AgentRow":
    defaults = dict(
        agent_id=agent_id,
        task_id=None,
        description="test agent",
        chat_id="12345",
        status="running",
        spawned_at=(NOW - timedelta(minutes=200)).isoformat(),
        output_file=None,
        last_seen_at=None,
        pid=None,
        dispatcher_pid=None,
    )
    defaults.update(overrides)
    return gd.AgentRow(**defaults)


# ---------------------------------------------------------------------------
# classify() — pure function, synthetic booleans
# ---------------------------------------------------------------------------


class TestClassifyPidPrecedence:
    def test_pid_alive_true_overrides_stale_age_and_missing_file(self):
        """A confirmed-alive pid is HEALTHY even though age/file would say GHOST_CONFIRMED."""
        label = gd.classify(
            age_minutes=500.0,
            output_file=None,
            output_file_age_minutes=None,
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
            pid_alive=True,
        )
        assert label == "HEALTHY"

    def test_pid_alive_false_overrides_fresh_age_and_fresh_file(self):
        """A confirmed-dead pid is GHOST_CONFIRMED even though age/file would say HEALTHY."""
        label = gd.classify(
            age_minutes=1.0,
            output_file="/tmp/whatever",
            output_file_age_minutes=0.1,
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
            pid_alive=False,
        )
        assert label == "GHOST_CONFIRMED"

    def test_dispatcher_pid_dead_marks_ghost_confirmed(self):
        """No pid of its own, but dispatcher confirmed dead → subagent necessarily dead too."""
        label = gd.classify(
            age_minutes=1.0,
            output_file="/tmp/whatever",
            output_file_age_minutes=0.1,
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
            pid_alive=None,
            dispatcher_pid_alive=False,
        )
        assert label == "GHOST_CONFIRMED"

    def test_dispatcher_pid_alive_falls_back_to_legacy_heuristic(self):
        """Dispatcher alive proves nothing about this subagent — falls through unchanged."""
        # Same inputs as the pre-existing GHOST_CONFIRMED legacy case (stale age,
        # output file missing) — dispatcher_pid_alive=True must not rescue it.
        label = gd.classify(
            age_minutes=500.0,
            output_file="/tmp/gone",
            output_file_age_minutes=None,
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
            pid_alive=None,
            dispatcher_pid_alive=True,
        )
        assert label == "GHOST_CONFIRMED"  # via the legacy "file path recorded but missing" rule

    def test_zero_pid_regression_matches_legacy_behavior(self):
        """Both pid_alive and dispatcher_pid_alive omitted (pre-migration row) — unchanged."""
        healthy = gd.classify(
            age_minutes=5.0,
            output_file=None,
            output_file_age_minutes=None,
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
        )
        assert healthy == "HEALTHY"

        stale_no_file = gd.classify(
            age_minutes=200.0,
            output_file=None,
            output_file_age_minutes=None,
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
        )
        assert stale_no_file == "STALE_NO_FILE"

        ghost_suspected = gd.classify(
            age_minutes=200.0,
            output_file="/tmp/f",
            output_file_age_minutes=2.0,
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
        )
        assert ghost_suspected == "GHOST_SUSPECTED"


# ---------------------------------------------------------------------------
# classify_agent() — real subprocess spawn/kill (the required acceptance-
# criteria proof: a real before/after demonstration, not just mocked booleans)
# ---------------------------------------------------------------------------


class TestClassifyAgentRealPid:
    def test_dead_pid_overrides_healthy_looking_row(self):
        """A row that looks HEALTHY by age/file alone is GHOST_CONFIRMED once its real pid is dead."""
        dead_pid = _spawn_and_kill_pid()
        row = _make_row(
            "dead-pid-agent",
            spawned_at=(NOW - timedelta(minutes=1)).isoformat(),  # very young — would be HEALTHY
            pid=dead_pid,
        )
        classified = gd.classify_agent(row, NOW, threshold_minutes=90.0, output_file_threshold_minutes=10.0)
        assert classified.classification == "GHOST_CONFIRMED"

    def test_live_pid_overrides_ghost_looking_row(self):
        """A row that looks GHOST_CONFIRMED by age/file alone is HEALTHY once its real pid is alive."""
        live_proc = subprocess.Popen(["sleep", "30"])
        try:
            row = _make_row(
                "live-pid-agent",
                spawned_at=(NOW - timedelta(minutes=500)).isoformat(),  # very stale
                output_file=None,  # STALE_NO_FILE territory under the legacy heuristic
                pid=live_proc.pid,
            )
            classified = gd.classify_agent(
                row, NOW, threshold_minutes=90.0, output_file_threshold_minutes=10.0
            )
            assert classified.classification == "HEALTHY"
        finally:
            live_proc.kill()
            live_proc.wait()

    def test_dead_dispatcher_pid_marks_inprocess_subagent_ghost(self):
        """No pid of its own; dispatcher confirmed dead → GHOST_CONFIRMED regardless of age."""
        dead_dispatcher_pid = _spawn_and_kill_pid()
        row = _make_row(
            "orphaned-subagent",
            spawned_at=(NOW - timedelta(minutes=1)).isoformat(),
            dispatcher_pid=dead_dispatcher_pid,
        )
        classified = gd.classify_agent(row, NOW, threshold_minutes=90.0, output_file_threshold_minutes=10.0)
        assert classified.classification == "GHOST_CONFIRMED"

    def test_zero_pid_row_unaffected(self):
        """A row with no pid/dispatcher_pid behaves exactly as before (regression guard)."""
        row = _make_row(
            "zero-pid-stale",
            spawned_at=(NOW - timedelta(minutes=200)).isoformat(),
            output_file=None,
        )
        classified = gd.classify_agent(row, NOW, threshold_minutes=90.0, output_file_threshold_minutes=10.0)
        assert classified.classification == "STALE_NO_FILE"


# ---------------------------------------------------------------------------
# Cross-classifier agreement (issue #2148 required acceptance criterion):
# session_store.cleanup_stale_running_sessions() and agent-monitor.py's
# classify_agent() must agree on the same dead/alive verdict for the same
# synthetic row (same pid, same output_file, same age).
# ---------------------------------------------------------------------------


class TestCrossClassifierAgreement:
    def test_dead_pid_agreement_between_session_store_and_agent_monitor(self, tmp_path):
        """The same dead-pid row is classified 'dead'/GHOST_CONFIRMED by both classifiers."""
        _SRC_DIR = str(Path(__file__).parent.parent.parent / "src")
        if _SRC_DIR not in sys.path:
            sys.path.insert(0, _SRC_DIR)
        from agents import session_store

        dead_pid = _spawn_and_kill_pid()
        db = tmp_path / "cross_classifier.db"
        session_store.init_db(db)

        output_file = tmp_path / "shared.output"
        output_file.write_text('{"stop_reason": "tool_use"}\n')

        spawned_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        session_store._get_connection(db).execute(
            """
            INSERT INTO agent_sessions
                (id, description, chat_id, source, status, spawned_at, output_file, pid)
            VALUES ('cross-agent', 'shared synthetic row', '123', 'telegram',
                    'running', ?, ?, ?)
            """,
            (spawned_at, str(output_file), dead_pid),
        )
        session_store._get_connection(db).commit()

        # Verdict 1: session_store.cleanup_stale_running_sessions()
        server_start = datetime.now(timezone.utc)
        changed = session_store.cleanup_stale_running_sessions(server_start, path=db)
        assert "cross-agent" in changed
        store_result = session_store.find_session("cross-agent", path=db)
        assert store_result["status"] == "dead"

        # Verdict 2: agent-monitor.py's classify_agent(), fed the exact same
        # pid/output_file/spawned_at via an AgentRow.
        row = gd.AgentRow(
            agent_id="cross-agent",
            task_id=None,
            description="shared synthetic row",
            chat_id="123",
            status="running",
            spawned_at=spawned_at,
            output_file=str(output_file),
            last_seen_at=None,
            pid=dead_pid,
            dispatcher_pid=None,
        )
        classified = gd.classify_agent(
            row,
            datetime.now(timezone.utc),
            threshold_minutes=90.0,
            output_file_threshold_minutes=10.0,
        )

        # Both classifiers agree: the process is dead, regardless of each
        # system's own vocabulary for expressing that ('dead' vs GHOST_CONFIRMED).
        assert classified.classification == "GHOST_CONFIRMED"

    def test_live_pid_agreement_between_session_store_and_agent_monitor(self, tmp_path):
        """The same live-pid row is classified 'running'/HEALTHY by both classifiers."""
        _SRC_DIR = str(Path(__file__).parent.parent.parent / "src")
        if _SRC_DIR not in sys.path:
            sys.path.insert(0, _SRC_DIR)
        from agents import session_store

        live_proc = subprocess.Popen(["sleep", "30"])
        try:
            db = tmp_path / "cross_classifier_live.db"
            session_store.init_db(db)

            # Stale by age/missing-file (would be dead/GHOST under legacy heuristics).
            spawned_at = (datetime.now(timezone.utc) - timedelta(minutes=500)).isoformat()
            missing_output = str(tmp_path / "never_created.output")
            session_store._get_connection(db).execute(
                """
                INSERT INTO agent_sessions
                    (id, description, chat_id, source, status, spawned_at,
                     output_file, timeout_minutes, pid)
                VALUES ('cross-agent-live', 'shared synthetic row', '123',
                        'telegram', 'running', ?, ?, 60, ?)
                """,
                (spawned_at, missing_output, live_proc.pid),
            )
            session_store._get_connection(db).commit()

            server_start = datetime.now(timezone.utc)
            changed = session_store.cleanup_stale_running_sessions(server_start, path=db)
            assert "cross-agent-live" not in changed
            store_result = session_store.find_session("cross-agent-live", path=db)
            assert store_result["status"] == "running"

            row = gd.AgentRow(
                agent_id="cross-agent-live",
                task_id=None,
                description="shared synthetic row",
                chat_id="123",
                status="running",
                spawned_at=spawned_at,
                output_file=missing_output,
                last_seen_at=None,
                pid=live_proc.pid,
                dispatcher_pid=None,
            )
            classified = gd.classify_agent(
                row,
                datetime.now(timezone.utc),
                threshold_minutes=90.0,
                output_file_threshold_minutes=10.0,
            )
            assert classified.classification == "HEALTHY"
        finally:
            live_proc.kill()
            live_proc.wait()
