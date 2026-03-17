"""
Unit tests for hooks/auto-register-agent.py

Tests cover:
- Non-Agent tool calls are ignored (exit 0, no DB write)
- YAML frontmatter: task_id, chat_id, source, reply_to_message_id parsed correctly
- Legacy text format: task_id extracted from "task_id is: X"
- agentId extracted from tool_response dict and list forms
- output_file extracted from tool_response
- DB row inserted with correct values
- INSERT OR IGNORE: existing row not overwritten
- Missing agentId: exits 0 without DB write
- DB failure: logs to hook-failures.log and exits 0
- Malformed stdin JSON: logs and exits 0
"""

import importlib.util
import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "auto-register-agent.py"


# ---------------------------------------------------------------------------
# Direct imports of pure functions (no side effects)
# ---------------------------------------------------------------------------

def _load_module():
    spec = importlib.util.spec_from_file_location("auto_register_agent", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
extract_metadata = _mod.extract_metadata
extract_agent_id = _mod.extract_agent_id
extract_output_file = _mod.extract_output_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_hook(hook_input: dict, tmp_path: Path) -> tuple[int, str, str]:
    """Run the hook via exec, capturing stdout/stderr and exit code."""
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
    tool_response: object = None,
    session_id: str = "sess-123",
) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": {"prompt": prompt},
        "tool_response": tool_response,
    }


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "messages" / "config" / "agent_sessions.db"
    return sqlite3.connect(str(db_path))


def _get_row(tmp_path: Path, agent_id: str) -> dict | None:
    conn = _open_db(tmp_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (agent_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# extract_metadata: pure function tests
# ---------------------------------------------------------------------------

class TestExtractMetadata:
    def test_yaml_frontmatter_all_fields(self):
        prompt = "---\ntask_id: my-task\nchat_id: 8305714125\nsource: telegram\nreply_to_message_id: 10924\n---\nsome content"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "my-task"
        assert meta["chat_id"] == "8305714125"
        assert meta["source"] == "telegram"
        assert meta["reply_to_message_id"] == "10924"

    def test_yaml_frontmatter_minimal(self):
        prompt = "---\ntask_id: slim-task\n---\n"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "slim-task"
        assert meta["source"] == "telegram"  # default
        assert meta["chat_id"] is None
        assert meta["reply_to_message_id"] is None

    def test_yaml_frontmatter_leading_whitespace(self):
        """Prompt may have leading whitespace before the ---."""
        prompt = "\n  \n---\ntask_id: ws-task\n---\n"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "ws-task"

    def test_legacy_text_format(self):
        prompt = "Your task_id is: legacy-task\nDo some work."
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "legacy-task"
        assert meta["chat_id"] is None

    def test_legacy_text_case_insensitive(self):
        prompt = "TASK_ID IS: upper-task"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "upper-task"

    def test_no_task_id(self):
        prompt = "Just a plain prompt with no id."
        meta = extract_metadata(prompt)
        assert meta["task_id"] is None

    def test_yaml_wins_over_legacy_text(self):
        """When both formats are present, frontmatter task_id wins."""
        prompt = "---\ntask_id: yaml-id\n---\nYour task_id is: legacy-id"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "yaml-id"

    def test_no_closing_delimiter_falls_back(self):
        """Unclosed frontmatter (no closing ---) is not treated as frontmatter."""
        prompt = "---\ntask_id: unclosed\n\nYour task_id is: textid"
        meta = extract_metadata(prompt)
        # Frontmatter parse fails, legacy text used
        assert meta["task_id"] == "textid"


# ---------------------------------------------------------------------------
# extract_agent_id: pure function tests
# ---------------------------------------------------------------------------

class TestExtractAgentId:
    def test_dict_response(self):
        assert extract_agent_id({"agentId": "agt-001"}) == "agt-001"

    def test_list_response(self):
        assert extract_agent_id([{"agentId": "agt-002"}]) == "agt-002"

    def test_list_first_match(self):
        response = [{"type": "text", "text": "..."}, {"agentId": "agt-003"}]
        assert extract_agent_id(response) == "agt-003"

    def test_missing_agent_id(self):
        assert extract_agent_id({"result": "ok"}) is None

    def test_none_response(self):
        assert extract_agent_id(None) is None

    def test_empty_list(self):
        assert extract_agent_id([]) is None


# ---------------------------------------------------------------------------
# extract_output_file: pure function tests
# ---------------------------------------------------------------------------

class TestExtractOutputFile:
    def test_snake_case_key(self):
        assert extract_output_file({"output_file": "/tmp/out.txt"}) == "/tmp/out.txt"

    def test_camel_case_key(self):
        assert extract_output_file({"outputFile": "/tmp/out2.txt"}) == "/tmp/out2.txt"

    def test_missing(self):
        assert extract_output_file({"agentId": "x"}) is None

    def test_in_list(self):
        assert extract_output_file([{"output_file": "/tmp/f.txt"}]) == "/tmp/f.txt"


# ---------------------------------------------------------------------------
# Integration: hook execution
# ---------------------------------------------------------------------------

class TestHookNonAgentTool:
    def test_non_agent_exits_0_no_db(self, tmp_path):
        """Non-Agent tool calls are ignored entirely."""
        hook_input = _make_hook_input(
            tool_name="Bash",
            prompt="ls",
            tool_response={"output": "file.txt"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0
        db_path = tmp_path / "messages" / "config" / "agent_sessions.db"
        assert not db_path.exists()


class TestHookAgentWithAgentId:
    def test_inserts_row_with_frontmatter(self, tmp_path):
        """Full frontmatter inserts a complete row."""
        prompt = "---\ntask_id: t-001\nchat_id: 99999\nsource: slack\n---\nDo stuff."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-abc"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-abc")
        assert row is not None
        assert row["task_id"] == "t-001"
        assert row["chat_id"] == "99999"
        assert row["source"] == "slack"
        assert row["status"] == "starting"

    def test_inserts_row_with_legacy_text(self, tmp_path):
        """Legacy task_id text format inserts a row."""
        prompt = "Your task_id is: t-legacy\nDo work."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-legacy"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-legacy")
        assert row is not None
        assert row["task_id"] == "t-legacy"

    def test_insert_or_ignore_preserves_existing_row(self, tmp_path):
        """A pre-existing row (from register_agent) is NOT overwritten."""
        # Pre-populate with a richer row
        db_dir = tmp_path / "messages" / "config"
        db_dir.mkdir(parents=True)
        conn = sqlite3.connect(str(db_dir / "agent_sessions.db"))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id TEXT PRIMARY KEY, task_id TEXT, agent_type TEXT,
                description TEXT NOT NULL, chat_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'telegram',
                status TEXT NOT NULL DEFAULT 'running',
                output_file TEXT, timeout_minutes INTEGER,
                input_summary TEXT, result_summary TEXT, parent_id TEXT,
                spawned_at TEXT NOT NULL, completed_at TEXT,
                last_seen_at TEXT, notified_at TEXT,
                trigger_message_id TEXT, trigger_snippet TEXT,
                reply_message_ids TEXT
            )
        """)
        conn.execute(
            "INSERT INTO agent_sessions (id, description, chat_id, status, spawned_at)"
            " VALUES ('agent-dup', 'richer description', '12345', 'running', '2026-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        prompt = "---\ntask_id: dup-task\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-dup"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-dup")
        # Description should still be the original richer one
        assert row["description"] == "richer description"
        assert row["status"] == "running"  # not overwritten to 'starting'

    def test_output_file_stored(self, tmp_path):
        """output_file from tool response is stored in DB."""
        prompt = "---\ntask_id: t-of\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-of", "output_file": "/tmp/result.json"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-of")
        assert row["output_file"] == "/tmp/result.json"

    def test_no_chat_id_defaults_to_zero(self, tmp_path):
        """Missing chat_id stores '0' to satisfy NOT NULL constraint."""
        prompt = "---\ntask_id: t-nochat\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-nochat"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-nochat")
        assert row["chat_id"] == "0"

    def test_spawned_at_is_sqlite_compatible_format(self, tmp_path):
        """spawned_at must use 'YYYY-MM-DD HH:MM:SS' so SQLite datetime() comparisons work.

        SQLite's datetime('now', '-30 minutes') produces a timezone-naive string
        like '2026-03-17 20:00:00'. If spawned_at uses ISO 8601 with a timezone
        suffix (e.g. '2026-03-17T20:00:00+00:00'), string comparison with SQLite's
        output fails silently and stale-row cleanup never fires.
        """
        import re

        prompt = "---\ntask_id: t-ts\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-ts"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-ts")
        spawned_at = row["spawned_at"]

        # Must match 'YYYY-MM-DD HH:MM:SS' exactly — no 'T' separator, no timezone offset
        sqlite_naive_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        assert sqlite_naive_pattern.match(spawned_at), (
            f"spawned_at '{spawned_at}' does not match SQLite-compatible "
            f"'YYYY-MM-DD HH:MM:SS' format"
        )

        # Verify SQLite itself can compare it against datetime('now', '-30 minutes')
        conn = _open_db(tmp_path)
        try:
            result = conn.execute(
                "SELECT spawned_at > datetime('now', '-30 minutes') FROM agent_sessions"
                " WHERE id = 'agent-ts'"
            ).fetchone()
            # The just-inserted row was spawned within the last 30 minutes
            assert result is not None and result[0] == 1, (
                "SQLite age comparison returned unexpected result — format mismatch likely"
            )
        finally:
            conn.close()


class TestHookNoAgentId:
    def test_missing_agent_id_exits_0_no_write(self, tmp_path):
        """If tool response has no agentId, exit 0 without touching DB."""
        prompt = "---\ntask_id: t-noid\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"result": "some output"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0
        db_path = tmp_path / "messages" / "config" / "agent_sessions.db"
        assert not db_path.exists()

    def test_none_response_exits_0(self, tmp_path):
        prompt = "---\ntask_id: t-none\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response=None,
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0


class TestHookFailureSafety:
    def test_malformed_json_exits_0(self, tmp_path):
        """Malformed stdin JSON must never crash the hook (exit 0)."""
        stdout_cap = StringIO()
        stderr_cap = StringIO()
        exit_code = None
        with (
            patch("sys.stdin", StringIO("not-valid-json{")),
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

        assert exit_code == 0

    def test_db_failure_logs_and_exits_0(self, tmp_path):
        """DB write failure is logged to hook-failures.log, not re-raised."""
        # Point DB to a read-only directory to force failure
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        (ro_dir / "config").mkdir()
        import os
        os.chmod(str(ro_dir / "config"), 0o444)

        prompt = "---\ntask_id: t-fail\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-fail"},
        )
        exit_code = None
        stdout_cap = StringIO()
        stderr_cap = StringIO()
        with (
            patch("sys.stdin", StringIO(json.dumps(hook_input))),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
            patch.dict("os.environ", {
                "LOBSTER_MESSAGES": str(ro_dir),
                "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
            }),
        ):
            try:
                hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
                exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
            except SystemExit as e:
                exit_code = e.code

        os.chmod(str(ro_dir / "config"), 0o755)  # restore for cleanup
        assert exit_code == 0

        log_path = tmp_path / "workspace" / "logs" / "hook-failures.log"
        assert log_path.exists(), "Expected failure to be logged"
        log_content = log_path.read_text()
        assert "auto-register-agent" in log_content
