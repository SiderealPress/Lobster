"""
Unit tests for hooks/prevent-duplicate-agent-spawn.py

Tests cover (issue #2313 -- pre-spawn dedup gate):
- Non-Agent/Task tool calls pass through (exit 0)
- No DB file yet -> allow (exit 0)
- No task_id and no trigger_message_id in prompt -> allow (exit 0)
- Colliding task_id against a running session -> blocked (exit 2)
- Colliding trigger_message_id against a running session, different task_id
  -> blocked (exit 2) -- this is the Mark Henry incident shape
- Non-colliding task_id -> allow (exit 0)
- Collision outside the time window -> allow (exit 0, treated as stale)
- Collision against a 'completed'/'dead' row -> allow (exit 0, only
  running/starting rows count)
- Self-collision excluded: a subagent's own still-running row (identified via
  agent_id in hook input) never collides with the spawn it is making
- Helper agent types (Explore, Plan) are exempt
- Malformed stdin is tolerated (exit 0, fail-open)
- Block message on stderr names the existing session id and task_id
"""

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "prevent-duplicate-agent-spawn.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prevent_duplicate_agent_spawn", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
find_collision = _mod.find_collision


def _run_hook(hook_input: dict, tmp_path: Path) -> tuple[int, str, str]:
    stdout_cap = StringIO()
    stderr_cap = StringIO()
    stdin_data = json.dumps(hook_input)
    exit_code = None
    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch.dict("os.environ", {
            "LOBSTER_MESSAGES": str(tmp_path / "messages"),
            "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
        }),
    ):
        try:
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code
    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _make_hook_input(
    tool_name: str = "Agent",
    prompt: str = "",
    subagent_type: str | None = None,
    agent_id: str | None = None,
) -> dict:
    tool_input = {"prompt": prompt}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


_SCHEMA = """
CREATE TABLE agent_sessions (
    id TEXT PRIMARY KEY, task_id TEXT, agent_type TEXT,
    description TEXT NOT NULL, chat_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'telegram', status TEXT NOT NULL DEFAULT 'running',
    output_file TEXT, timeout_minutes INTEGER, input_summary TEXT, result_summary TEXT,
    parent_id TEXT, spawned_at TEXT NOT NULL, completed_at TEXT, last_seen_at TEXT,
    notified_at TEXT, trigger_message_id TEXT, trigger_snippet TEXT, reply_message_ids TEXT,
    stop_reason TEXT, idempotency TEXT, task_origin TEXT, pid INTEGER, dispatcher_pid INTEGER,
    pid_captured_at TEXT
);
"""


def _seed_db(
    tmp_path: Path,
    *,
    agent_id: str,
    task_id: str | None = None,
    trigger_message_id: str | None = None,
    status: str = "running",
    spawned_at: str | None = None,
    description: str = "existing agent",
) -> None:
    db_dir = tmp_path / "messages" / "config"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_dir / "agent_sessions.db"))
    conn.execute(_SCHEMA)
    conn.execute(
        """
        INSERT INTO agent_sessions
            (id, task_id, description, chat_id, status, spawned_at, trigger_message_id)
        VALUES (?, ?, ?, '123', ?, ?, ?)
        """,
        (
            agent_id,
            task_id,
            description,
            status,
            spawned_at or datetime.now(timezone.utc).isoformat(),
            trigger_message_id,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Hook-level behaviour
# ---------------------------------------------------------------------------

class TestNonAgentToolsPassThrough:
    def test_non_agent_tool_ignored(self, tmp_path):
        hook_input = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0


class TestNoDbYet:
    def test_missing_db_allows_spawn(self, tmp_path):
        hook_input = _make_hook_input(prompt="---\ntask_id: fresh-task\nchat_id: 1\n---\ndo it")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0


class TestNoIdentifiersToMatch:
    def test_prompt_without_task_id_or_trigger_message_id_allowed(self, tmp_path):
        _seed_db(tmp_path, agent_id="existing-1", task_id="some-task")
        hook_input = _make_hook_input(prompt="Do something with no frontmatter at all.")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0


class TestTaskIdCollision:
    def test_colliding_task_id_blocked(self, tmp_path):
        _seed_db(tmp_path, agent_id="existing-1", task_id="scheduled-job-x-bookmarks-20261021")
        hook_input = _make_hook_input(
            prompt="---\ntask_id: scheduled-job-x-bookmarks-20261021\nchat_id: 123\n---\ndo it"
        )
        exit_code, _, stderr = _run_hook(hook_input, tmp_path)
        assert exit_code == 2
        assert "existing-1" in stderr
        assert "scheduled-job-x-bookmarks-20261021" in stderr

    def test_non_colliding_task_id_allowed(self, tmp_path):
        _seed_db(tmp_path, agent_id="existing-1", task_id="some-other-task")
        hook_input = _make_hook_input(prompt="---\ntask_id: totally-different-task\nchat_id: 123\n---\ndo it")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0


class TestTriggerMessageIdCollision:
    def test_colliding_trigger_message_id_with_different_task_id_blocked(self, tmp_path):
        """Mirrors the Mark Henry incident: three different task_ids, same
        trigger_message_id -- must be caught even though task_id differs."""
        _seed_db(
            tmp_path,
            agent_id="existing-1",
            task_id="mark-henry-agreement-notice-edit-20261020",
            trigger_message_id="1792540426526_15496",
        )
        hook_input = _make_hook_input(
            prompt=(
                "---\ntask_id: gai-notice-period-edit\n"
                "trigger_message_id: 1792540426526_15496\nchat_id: 123\n---\ndo it"
            )
        )
        exit_code, _, stderr = _run_hook(hook_input, tmp_path)
        assert exit_code == 2
        assert "existing-1" in stderr


class TestTimeWindow:
    def test_stale_collision_outside_window_allowed(self, tmp_path):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        _seed_db(
            tmp_path,
            agent_id="existing-1",
            task_id="long-running-task",
            spawned_at=old_ts,
        )
        hook_input = _make_hook_input(prompt="---\ntask_id: long-running-task\nchat_id: 123\n---\ndo it")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

    def test_collision_within_window_blocked(self, tmp_path):
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        _seed_db(
            tmp_path,
            agent_id="existing-1",
            task_id="recent-task",
            spawned_at=recent_ts,
        )
        hook_input = _make_hook_input(prompt="---\ntask_id: recent-task\nchat_id: 123\n---\ndo it")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 2


class TestStatusFiltering:
    def test_completed_row_does_not_collide(self, tmp_path):
        _seed_db(tmp_path, agent_id="existing-1", task_id="done-task", status="completed")
        hook_input = _make_hook_input(prompt="---\ntask_id: done-task\nchat_id: 123\n---\ndo it")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

    def test_dead_row_does_not_collide(self, tmp_path):
        _seed_db(tmp_path, agent_id="existing-1", task_id="dead-task", status="dead")
        hook_input = _make_hook_input(prompt="---\ntask_id: dead-task\nchat_id: 123\n---\ndo it")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

    def test_starting_row_does_collide(self, tmp_path):
        _seed_db(tmp_path, agent_id="existing-1", task_id="starting-task", status="starting")
        hook_input = _make_hook_input(prompt="---\ntask_id: starting-task\nchat_id: 123\n---\ndo it")
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 2


class TestSelfExclusion:
    def test_own_running_row_does_not_collide_with_self(self, tmp_path):
        """A subagent re-entering the same task_id it was itself registered
        under (e.g. re-checking in) must not be blocked by its own row."""
        _seed_db(tmp_path, agent_id="existing-1", task_id="self-task")
        hook_input = _make_hook_input(
            prompt="---\ntask_id: self-task\nchat_id: 123\n---\ndo it",
            agent_id="existing-1",
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

    def test_different_agent_same_task_id_still_collides(self, tmp_path):
        _seed_db(tmp_path, agent_id="existing-1", task_id="shared-task")
        hook_input = _make_hook_input(
            prompt="---\ntask_id: shared-task\nchat_id: 123\n---\ndo it",
            agent_id="some-other-agent",
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 2


class TestHelperAgentExemption:
    def test_explore_type_exempt(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOBSTER_HELPER_AGENT_TYPES", raising=False)
        _seed_db(tmp_path, agent_id="existing-1", task_id="helper-task")
        hook_input = _make_hook_input(
            prompt="---\ntask_id: helper-task\nchat_id: 123\n---\ndo it",
            subagent_type="Explore",
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0


class TestMalformedInput:
    def test_malformed_json_fails_open(self, tmp_path):
        stdout_cap, stderr_cap = StringIO(), StringIO()
        with (
            patch("sys.stdin", StringIO("not json")),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
            patch.dict("os.environ", {
                "LOBSTER_MESSAGES": str(tmp_path / "messages"),
                "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
            }),
        ):
            exit_code = None
            try:
                hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
                exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
            except SystemExit as e:
                exit_code = e.code
        assert exit_code == 0


# ---------------------------------------------------------------------------
# find_collision: pure-ish function tests
# ---------------------------------------------------------------------------

class TestFindCollisionDirect:
    def test_no_match_returns_none(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        conn.execute(_SCHEMA)
        conn.commit()
        result = find_collision(
            conn,
            task_id="x",
            trigger_message_id=None,
            new_agent_hint=None,
            now=datetime.now(timezone.utc),
            window_minutes=30,
        )
        assert result is None
        conn.close()

    def test_no_identifiers_returns_none(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        conn.execute(_SCHEMA)
        conn.commit()
        result = find_collision(
            conn,
            task_id=None,
            trigger_message_id=None,
            new_agent_hint=None,
            now=datetime.now(timezone.utc),
            window_minutes=30,
        )
        assert result is None
        conn.close()
