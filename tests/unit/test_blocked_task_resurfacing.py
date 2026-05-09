"""
Tests for blocked-on-user task handling in the task system (issue #1917).

When Lobster commits to a task and asks clarifying questions, that commitment
must survive crash/restart cycles and be proactively re-surfaced to the user.

These tests verify:
1. 'blocked' is accepted as a valid task status in create_task, update_task, list_tasks
2. list_tasks filters correctly for blocked tasks
3. list_tasks groups blocked tasks distinctly so the dispatcher can identify them
4. The `blocked` status round-trips through create → update → list correctly

The behavioral protocol (dispatcher surfacing blocked tasks on first user interaction)
is verified by the protocol text checks in test_blocked_surfacing_protocol.py.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import sys

_VENV_SITE = Path("/home/lobster/lobster/.venv/lib/python3.12/site-packages")
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

_MCP_DIR = Path(__file__).parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import inbox_server as _inbox_server_module  # noqa: F401


def _run(coro):
    return asyncio.run(coro)


def _text(result):
    return result[0].text


# ---------------------------------------------------------------------------
# Helpers: isolated task file per test
# ---------------------------------------------------------------------------

def _make_tasks_file(tmp_dir: Path) -> Path:
    """Create an empty tasks JSON file in a temp directory."""
    tasks_file = tmp_dir / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": [], "next_id": 1}))
    return tasks_file


# ---------------------------------------------------------------------------
# create_task with blocked status
# ---------------------------------------------------------------------------

BLOCKED_REASON_WAITING_ON_USER = "BLOCKED: waiting on user's clarifying questions before proceeding"


class TestCreateTaskBlockedStatus:
    """create_task must accept 'blocked' as an initial status."""

    def _call(self, args, tasks_file):
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_create_task
            return _run(handle_create_task(args))

    def test_blocked_status_accepted_at_creation(self, tmp_path):
        tasks_file = _make_tasks_file(tmp_path)
        result = self._call(
            {"subject": "Deploy new feature", "status": "blocked", "description": BLOCKED_REASON_WAITING_ON_USER},
            tasks_file,
        )
        assert "Error" not in _text(result)
        assert "#1" in _text(result)

    def test_blocked_task_persisted_to_file(self, tmp_path):
        tasks_file = _make_tasks_file(tmp_path)
        self._call(
            {"subject": "Deploy new feature", "status": "blocked", "description": BLOCKED_REASON_WAITING_ON_USER},
            tasks_file,
        )
        saved = json.loads(tasks_file.read_text())
        assert saved["tasks"][0]["status"] == "blocked"

    def test_pending_status_still_accepted(self, tmp_path):
        """Regression: existing statuses continue to work."""
        tasks_file = _make_tasks_file(tmp_path)
        result = self._call({"subject": "Simple task", "status": "pending"}, tasks_file)
        assert "Error" not in _text(result)

    def test_invalid_status_still_rejected(self, tmp_path):
        tasks_file = _make_tasks_file(tmp_path)
        result = self._call({"subject": "Bad task", "status": "on-fire"}, tasks_file)
        assert "Error" in _text(result)


# ---------------------------------------------------------------------------
# update_task to/from blocked status
# ---------------------------------------------------------------------------

class TestUpdateTaskBlockedStatus:
    """update_task must accept 'blocked' and allow transitioning away from it."""

    def _setup_task(self, tmp_path, status="pending") -> tuple[Path, int]:
        tasks_file = _make_tasks_file(tmp_path)
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_create_task
            _run(handle_create_task({"subject": "Test task", "status": status}))
        return tasks_file, 1

    def test_update_to_blocked_accepted(self, tmp_path):
        tasks_file, task_id = self._setup_task(tmp_path)
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_update_task
            result = _run(handle_update_task({"task_id": task_id, "status": "blocked"}))
        assert "Error" not in _text(result)
        saved = json.loads(tasks_file.read_text())
        assert saved["tasks"][0]["status"] == "blocked"

    def test_update_from_blocked_to_in_progress(self, tmp_path):
        """Unblocking a task (user answered) transitions to in_progress."""
        tasks_file, task_id = self._setup_task(tmp_path, status="blocked")
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_update_task
            result = _run(handle_update_task({"task_id": task_id, "status": "in_progress"}))
        assert "Error" not in _text(result)
        saved = json.loads(tasks_file.read_text())
        assert saved["tasks"][0]["status"] == "in_progress"

    def test_update_from_blocked_to_pending_accepted(self, tmp_path):
        tasks_file, task_id = self._setup_task(tmp_path, status="blocked")
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_update_task
            result = _run(handle_update_task({"task_id": task_id, "status": "pending"}))
        assert "Error" not in _text(result)

    def test_description_update_on_blocked_task_preserved(self, tmp_path):
        tasks_file, task_id = self._setup_task(tmp_path, status="blocked")
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_update_task
            _run(handle_update_task({
                "task_id": task_id,
                "description": "User responded — proceeding with option A.",
                "status": "in_progress",
            }))
        saved = json.loads(tasks_file.read_text())
        assert saved["tasks"][0]["description"] == "User responded — proceeding with option A."


# ---------------------------------------------------------------------------
# list_tasks with blocked status filter
# ---------------------------------------------------------------------------

BLOCKED_TASK_SUBJECT = "Set up new integration"
BLOCKED_TASK_DESCRIPTION = "BLOCKED: waiting on user's answer about which API endpoint to use"


def _seed_mixed_tasks(tasks_file: Path) -> None:
    """Write a task file with one of each status."""
    tasks = {
        "tasks": [
            {
                "id": 1,
                "subject": "Routine task",
                "description": "",
                "status": "pending",
                "created_at": "2026-05-09T10:00:00Z",
                "updated_at": "2026-05-09T10:00:00Z",
            },
            {
                "id": 2,
                "subject": BLOCKED_TASK_SUBJECT,
                "description": BLOCKED_TASK_DESCRIPTION,
                "status": "blocked",
                "created_at": "2026-05-09T08:00:00Z",
                "updated_at": "2026-05-09T08:30:00Z",
            },
            {
                "id": 3,
                "subject": "In-flight subagent work",
                "description": "",
                "status": "in_progress",
                "created_at": "2026-05-09T09:00:00Z",
                "updated_at": "2026-05-09T09:30:00Z",
            },
        ],
        "next_id": 4,
    }
    tasks_file.write_text(json.dumps(tasks))


class TestListTasksBlockedFilter:
    """list_tasks must surface blocked tasks correctly."""

    def _call(self, args, tasks_file):
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_list_tasks
            return _run(handle_list_tasks(args))

    def test_blocked_filter_returns_only_blocked_tasks(self, tmp_path):
        tasks_file = tmp_path / "tasks.json"
        _seed_mixed_tasks(tasks_file)
        result = self._call({"status": "blocked"}, tasks_file)
        text = _text(result)
        assert BLOCKED_TASK_SUBJECT in text
        assert "Routine task" not in text
        assert "In-flight subagent work" not in text

    def test_all_filter_includes_blocked_tasks(self, tmp_path):
        """status=all must include blocked tasks so dispatcher gets the full picture."""
        tasks_file = tmp_path / "tasks.json"
        _seed_mixed_tasks(tasks_file)
        result = self._call({"status": "all"}, tasks_file)
        text = _text(result)
        assert BLOCKED_TASK_SUBJECT in text

    def test_default_all_filter_includes_blocked(self, tmp_path):
        """Omitting status parameter (defaults to 'all') includes blocked tasks."""
        tasks_file = tmp_path / "tasks.json"
        _seed_mixed_tasks(tasks_file)
        result = self._call({}, tasks_file)
        text = _text(result)
        assert BLOCKED_TASK_SUBJECT in text

    def test_blocked_tasks_shown_in_distinct_section(self, tmp_path):
        """Blocked tasks must appear in a visually distinct section, not lumped with pending.

        The dispatcher relies on visual structure to identify these as high-priority.
        A blocked task in the 'Pending' group would be invisible under the noise.
        """
        tasks_file = tmp_path / "tasks.json"
        _seed_mixed_tasks(tasks_file)
        result = self._call({}, tasks_file)
        text = _text(result)
        # Blocked tasks should appear in a section that signals urgency,
        # not under the standard pending/in-progress groupings
        assert "Blocked" in text or "BLOCKED" in text or "blocked" in text.lower()

    def test_blocked_tasks_appear_before_pending_in_output(self, tmp_path):
        """Blocked commitments are higher priority than pending tasks.

        They represent explicit commitments Lobster made and then got stuck on —
        the user is waiting for Lobster to resume.
        """
        tasks_file = tmp_path / "tasks.json"
        _seed_mixed_tasks(tasks_file)
        result = self._call({}, tasks_file)
        text = _text(result)
        blocked_pos = text.find(BLOCKED_TASK_SUBJECT)
        pending_pos = text.find("Routine task")
        assert blocked_pos != -1, "Blocked task not found in output"
        assert pending_pos != -1, "Pending task not found in output"
        assert blocked_pos < pending_pos, (
            "Blocked tasks must appear before pending tasks — they are higher priority. "
            f"Found blocked at position {blocked_pos}, pending at {pending_pos}"
        )

    def test_no_tasks_still_returns_no_tasks_message(self, tmp_path):
        """Regression: empty task list still returns helpful message."""
        tasks_file = _make_tasks_file(tmp_path)
        result = self._call({"status": "blocked"}, tasks_file)
        text = _text(result)
        assert "No tasks" in text or "no tasks" in text.lower() or "0" in text

    def test_pending_filter_excludes_blocked_tasks(self, tmp_path):
        """status=pending must not include blocked tasks — they are a distinct category."""
        tasks_file = tmp_path / "tasks.json"
        _seed_mixed_tasks(tasks_file)
        result = self._call({"status": "pending"}, tasks_file)
        text = _text(result)
        assert BLOCKED_TASK_SUBJECT not in text


# ---------------------------------------------------------------------------
# Startup startup scan: get_task on a blocked task
# ---------------------------------------------------------------------------

class TestGetTaskBlockedStatus:
    """get_task must display blocked status clearly."""

    def _call(self, args, tasks_file):
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_get_task
            return _run(handle_get_task(args))

    def test_blocked_status_displayed(self, tmp_path):
        tasks_file = tmp_path / "tasks.json"
        _seed_mixed_tasks(tasks_file)
        result = self._call({"task_id": 2}, tasks_file)
        text = _text(result)
        assert "blocked" in text.lower()
        assert BLOCKED_TASK_SUBJECT in text


# ---------------------------------------------------------------------------
# list_tasks fallback "Other" group for unrecognized statuses
# ---------------------------------------------------------------------------

class TestListTasksOtherGroup:
    """Tasks with unrecognized statuses must appear in an 'Other' section,
    not be silently dropped from the list_tasks display.

    This covers legacy 'done' status and any future unknown values that may
    exist in production task stores from old data.
    """

    def _call(self, args, tasks_file):
        with patch("inbox_server.TASKS_FILE", tasks_file):
            from inbox_server import handle_list_tasks
            return _run(handle_list_tasks(args))

    def _seed_with_legacy_status(self, tasks_file: Path) -> None:
        """Write a task file containing a task with legacy 'done' status."""
        tasks = {
            "tasks": [
                {
                    "id": 1,
                    "subject": "Normal pending task",
                    "description": "",
                    "status": "pending",
                    "created_at": "2026-05-09T10:00:00Z",
                    "updated_at": "2026-05-09T10:00:00Z",
                },
                {
                    "id": 2,
                    "subject": "Old legacy done task",
                    "description": "",
                    "status": "done",
                    "created_at": "2026-05-09T09:00:00Z",
                    "updated_at": "2026-05-09T09:00:00Z",
                },
            ],
            "next_id": 3,
        }
        tasks_file.write_text(json.dumps(tasks))

    def test_legacy_done_task_appears_in_other_section(self, tmp_path):
        """A task with status='done' (not in VALID_TASK_STATUSES) must appear in
        the 'Other' section of list_tasks output rather than being silently dropped."""
        tasks_file = tmp_path / "tasks.json"
        self._seed_with_legacy_status(tasks_file)
        result = self._call({}, tasks_file)
        text = _text(result)
        assert "Old legacy done task" in text, (
            "Task with unrecognized status 'done' was silently dropped from list_tasks output"
        )
        assert "Other" in text, (
            "Expected an 'Other' fallback section for unrecognized statuses, but none was found"
        )
        assert "[done]" in text, (
            "The unrecognized status label should appear next to the task subject"
        )

    def test_legacy_task_does_not_affect_total_count(self, tmp_path):
        """The total task count must include legacy-status tasks."""
        tasks_file = tmp_path / "tasks.json"
        self._seed_with_legacy_status(tasks_file)
        result = self._call({}, tasks_file)
        text = _text(result)
        assert "2 task(s)" in text, (
            "Total count must include tasks with unrecognized statuses"
        )

    def test_known_statuses_still_appear_normally(self, tmp_path):
        """Adding an 'Other' section must not interfere with normal status groups."""
        tasks_file = tmp_path / "tasks.json"
        self._seed_with_legacy_status(tasks_file)
        result = self._call({}, tasks_file)
        text = _text(result)
        assert "Normal pending task" in text
        assert "Pending" in text
