"""
Tests for stale dispatcher-row retirement in write-dispatcher-session-id.py.

Problem: every new dispatcher Claude session registers a 'running' row with
agent_type='dispatcher' in agent_sessions.db, but nothing ever retired the
previous session's row. The reconciler skips dispatcher rows by design, so they
accumulated forever (125 observed in production) and were printed on every
wait_for_messages call, inflating dispatcher context and forcing compaction.

Spec:
- Registering a new dispatcher session marks every OTHER running
  agent_type='dispatcher' row as dead.
- The static main-loop row (id='lobster-dispatcher', written by the
  dispatcher's own session_start call) is never retired by the hook.
- The current session's row stays (or becomes) running.
- Non-dispatcher rows (subagents) are never touched.
- Already-finished dispatcher rows are left as they are.
- Re-registering the same session (e.g. after compact) is idempotent.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "write-dispatcher-session-id.py"

# Named constants from the spec.
MAIN_LOOP_DISPATCHER_ID = "lobster-dispatcher"
RETIRED_STATUS = "dead"
RUNNING_STATUS = "running"


def _load_hook():
    name = f"write_dispatcher_session_id_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hook():
    return _load_hook()


@pytest.fixture()
def db_path(tmp_path, hook):
    path = tmp_path / "agent_sessions.db"
    hook.session_store.init_db(path)
    yield path
    hook.session_store._close_connection(path)


def _insert(path: Path, id: str, agent_type: str | None, status: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO agent_sessions (id, description, chat_id, status, agent_type, spawned_at)"
        " VALUES (?, 'test row', '0', ?, ?, '2026-10-01T00:00:00+00:00')",
        (id, status, agent_type),
    )
    conn.commit()
    conn.close()


def _row(path: Path, id: str) -> dict:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def _running_dispatcher_ids(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    ids = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM agent_sessions WHERE agent_type='dispatcher' AND status='running'"
        )
    }
    conn.close()
    return ids


def test_new_dispatcher_session_retires_previous_session_rows(hook, db_path):
    for old in ("old-session-1", "old-session-2", "old-session-3"):
        _insert(db_path, old, "dispatcher", RUNNING_STATUS)

    hook._register_dispatcher_session("new-session", db_path=db_path)

    assert _running_dispatcher_ids(db_path) == {"new-session"}
    for old in ("old-session-1", "old-session-2", "old-session-3"):
        row = _row(db_path, old)
        assert row["status"] == RETIRED_STATUS
        assert row["completed_at"], "retired rows must record an end time"
        assert row["stop_reason"] == hook.SUPERSEDED_STOP_REASON


def test_main_loop_dispatcher_row_is_never_retired(hook, db_path):
    _insert(db_path, MAIN_LOOP_DISPATCHER_ID, "dispatcher", RUNNING_STATUS)
    _insert(db_path, "old-session", "dispatcher", RUNNING_STATUS)

    hook._register_dispatcher_session("new-session", db_path=db_path)

    assert _running_dispatcher_ids(db_path) == {MAIN_LOOP_DISPATCHER_ID, "new-session"}


def test_subagent_rows_are_untouched(hook, db_path):
    _insert(db_path, "subagent-a", "functional-engineer", RUNNING_STATUS)
    _insert(db_path, "subagent-b", None, RUNNING_STATUS)

    hook._register_dispatcher_session("new-session", db_path=db_path)

    assert _row(db_path, "subagent-a")["status"] == RUNNING_STATUS
    assert _row(db_path, "subagent-b")["status"] == RUNNING_STATUS


def test_already_finished_dispatcher_rows_are_left_alone(hook, db_path):
    _insert(db_path, "old-failed", "dispatcher", "failed")
    _insert(db_path, "old-completed", "dispatcher", "completed")

    hook._register_dispatcher_session("new-session", db_path=db_path)

    assert _row(db_path, "old-failed")["status"] == "failed"
    assert _row(db_path, "old-failed")["stop_reason"] is None
    assert _row(db_path, "old-completed")["status"] == "completed"


def test_reregistering_same_session_is_idempotent(hook, db_path):
    hook._register_dispatcher_session("same-session", db_path=db_path)
    hook._register_dispatcher_session("same-session", db_path=db_path)

    assert _running_dispatcher_ids(db_path) == {"same-session"}
    assert _row(db_path, "same-session")["completed_at"] is None


def test_retirement_count_is_logged(hook, db_path, capsys):
    for old in ("old-session-1", "old-session-2"):
        _insert(db_path, old, "dispatcher", RUNNING_STATUS)

    hook._register_dispatcher_session("new-session", db_path=db_path)

    assert "retired 2 superseded dispatcher row(s)" in capsys.readouterr().err


def test_nothing_logged_when_no_rows_retired(hook, db_path, capsys):
    hook._register_dispatcher_session("new-session", db_path=db_path)

    assert "retired" not in capsys.readouterr().err
