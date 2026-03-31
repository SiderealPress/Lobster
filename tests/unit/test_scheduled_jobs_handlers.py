"""
Tests for the systemd-backed scheduled job MCP handlers.

Tests handle_create_scheduled_job, handle_list_scheduled_jobs,
handle_get_scheduled_job, handle_update_scheduled_job,
handle_delete_scheduled_job, and handle_get_job_scaffold.

All systemd I/O and subprocess calls are mocked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
# Add lobster venv and src/mcp to path so inbox_server and systemd_jobs are importable
_VENV_SITE = Path("/home/lobster/lobster/.venv/lib/python3.12/site-packages")
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

_MCP_DIR = Path(__file__).parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

# Needed for patch("inbox_server._sj_...") to work — must import the module
# via the same name used in the patch target.
import inbox_server as _inbox_server_module  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _text(result):
    return result[0].text


# Lightweight stubs matching systemd_jobs dataclasses
class _CreateResult:
    def __init__(self, name, status):
        self.name = name
        self.status = status


class _UpdateResult:
    def __init__(self, name, updated_fields):
        self.name = name
        self.updated_fields = updated_fields


class _DeleteResult:
    def __init__(self, name, status):
        self.name = name
        self.status = status


class _JobInfo:
    def __init__(self, name, schedule, command, active, last_run=None, next_run=None):
        self.name = name
        self.schedule = schedule
        self.command = command
        self.description = f"Lobster job: {name}"
        self.active = active
        self.last_run = last_run
        self.next_run = next_run


# ---------------------------------------------------------------------------
# create_scheduled_job
# ---------------------------------------------------------------------------

class TestHandleCreateScheduledJob:
    def _call(self, args, *, create_return=None):
        if create_return is None:
            create_return = _CreateResult("test-job", "created")
        with patch("inbox_server._sj_validate_name", return_value=None), \
             patch("inbox_server._sj_normalize_schedule", return_value=("daily", None)), \
             patch("inbox_server._sj_validate_command", return_value=None), \
             patch("inbox_server._sj_create_job", new=AsyncMock(return_value=create_return)):
            from inbox_server import handle_create_scheduled_job
            return _run(handle_create_scheduled_job(args))

    def test_success_created(self):
        result = self._call({"name": "test-job", "schedule": "daily", "command": "/bin/echo hi"})
        assert "Created" in _text(result)
        assert "test-job" in _text(result)

    def test_success_already_exists(self):
        result = self._call(
            {"name": "test-job", "schedule": "daily", "command": "/bin/echo hi"},
            create_return=_CreateResult("test-job", "already_exists"),
        )
        assert "already exists" in _text(result).lower()

    def test_invalid_name_returns_error(self):
        with patch("inbox_server._sj_validate_name", return_value="name cannot be empty"):
            from inbox_server import handle_create_scheduled_job
            result = _run(handle_create_scheduled_job({"name": "", "schedule": "daily", "command": "/bin/echo"}))
        assert "Error" in _text(result)
        assert "name cannot be empty" in _text(result)

    def test_invalid_command_returns_error(self):
        with patch("inbox_server._sj_validate_name", return_value=None), \
             patch("inbox_server._sj_normalize_schedule", return_value=("daily", None)), \
             patch("inbox_server._sj_validate_command", return_value="command must be an absolute path"):
            from inbox_server import handle_create_scheduled_job
            result = _run(handle_create_scheduled_job({
                "name": "job", "schedule": "daily", "command": "relative/path"
            }))
        assert "Error" in _text(result)

    def test_create_job_exception_returns_error(self):
        with patch("inbox_server._sj_validate_name", return_value=None), \
             patch("inbox_server._sj_normalize_schedule", return_value=("daily", None)), \
             patch("inbox_server._sj_validate_command", return_value=None), \
             patch("inbox_server._sj_create_job", new=AsyncMock(side_effect=RuntimeError("systemctl failed"))):
            from inbox_server import handle_create_scheduled_job
            result = _run(handle_create_scheduled_job({
                "name": "job", "schedule": "daily", "command": "/bin/echo"
            }))
        assert "Error" in _text(result)

    def test_invalid_schedule_returns_error(self):
        with patch("inbox_server._sj_validate_name", return_value=None), \
             patch("inbox_server._sj_normalize_schedule",
                   return_value=("bad", "Invalid schedule 'bad': ...")):
            from inbox_server import handle_create_scheduled_job
            result = _run(handle_create_scheduled_job({
                "name": "job", "schedule": "bad", "command": "/bin/echo"
            }))
        assert "Error" in _text(result)

    def test_cron_expression_is_normalized(self):
        """Passing a cron expression should trigger normalize_schedule."""
        captured = {}

        def fake_normalize(schedule):
            captured["schedule"] = schedule
            return ("*-*-* 09:00:00", None)

        with patch("inbox_server._sj_validate_name", return_value=None), \
             patch("inbox_server._sj_normalize_schedule", side_effect=fake_normalize), \
             patch("inbox_server._sj_validate_command", return_value=None), \
             patch("inbox_server._sj_create_job",
                   new=AsyncMock(return_value=_CreateResult("job", "created"))):
            from inbox_server import handle_create_scheduled_job
            _run(handle_create_scheduled_job({
                "name": "job", "schedule": "0 9 * * *", "command": "/bin/echo"
            }))
        assert captured["schedule"] == "0 9 * * *"

    def test_output_includes_unit_file_paths(self):
        result = self._call({"name": "my-job", "schedule": "daily", "command": "/bin/echo hi"})
        text = _text(result)
        assert "lobster-my-job.timer" in text
        assert "lobster-my-job.service" in text


# ---------------------------------------------------------------------------
# list_scheduled_jobs
# ---------------------------------------------------------------------------

class TestHandleListScheduledJobs:
    def _call(self, jobs):
        with patch("inbox_server._sj_list_jobs", new=AsyncMock(return_value=jobs)):
            from inbox_server import handle_list_scheduled_jobs
            return _run(handle_list_scheduled_jobs({}))

    def test_empty_returns_no_jobs_message(self):
        result = self._call([])
        assert "No lobster-managed" in _text(result)

    def test_lists_jobs(self):
        jobs = [
            _JobInfo("daily-check", "daily", "/bin/check", active=True, next_run="tomorrow"),
            _JobInfo("weekly-backup", "weekly", "/bin/backup", active=False),
        ]
        result = self._call(jobs)
        text = _text(result)
        assert "daily-check" in text
        assert "weekly-backup" in text
        assert "active" in text

    def test_shows_count(self):
        jobs = [_JobInfo("job1", "daily", "/bin/echo", active=True)]
        result = self._call(jobs)
        assert "1 job" in _text(result)

    def test_exception_returns_error(self):
        with patch("inbox_server._sj_list_jobs", new=AsyncMock(side_effect=Exception("systemd error"))):
            from inbox_server import handle_list_scheduled_jobs
            result = _run(handle_list_scheduled_jobs({}))
        assert "Error" in _text(result)


# ---------------------------------------------------------------------------
# get_scheduled_job
# ---------------------------------------------------------------------------

class TestHandleGetScheduledJob:
    def test_missing_name_returns_error(self):
        from inbox_server import handle_get_scheduled_job
        result = _run(handle_get_scheduled_job({}))
        assert "Error" in _text(result)
        assert "name is required" in _text(result)

    def test_not_found_returns_error(self, tmp_path):
        with patch("inbox_server._sj_timer_path", return_value=tmp_path / "nonexistent.timer"), \
             patch("inbox_server._sj_is_lobster_unit", return_value=False):
            from inbox_server import handle_get_scheduled_job
            result = _run(handle_get_scheduled_job({"name": "ghost-job"}))
        assert "Error" in _text(result)
        assert "not found" in _text(result).lower()

    def test_found_returns_details(self, tmp_path):
        import systemd_jobs as sj
        timer = tmp_path / "lobster-test-job.timer"
        service = tmp_path / "lobster-test-job.service"
        timer.write_text(sj._timer_unit("test-job", "daily", "Test job"))
        service.write_text(sj._service_unit("test-job", "/bin/echo hi", "Test job"))

        with patch("inbox_server._sj_timer_path", return_value=timer), \
             patch("inbox_server._sj_service_path", return_value=service), \
             patch("inbox_server._sj_is_lobster_unit", return_value=True), \
             patch("inbox_server._sj_read_unit_field") as mock_field:
            mock_field.side_effect = lambda p, f: "daily" if f == "OnCalendar" else "/bin/echo hi"
            from inbox_server import handle_get_scheduled_job
            result = _run(handle_get_scheduled_job({"name": "test-job"}))

        text = _text(result)
        assert "test-job" in text
        assert "Schedule" in text
        assert "Command" in text


# ---------------------------------------------------------------------------
# update_scheduled_job
# ---------------------------------------------------------------------------

class TestHandleUpdateScheduledJob:
    def test_missing_name_returns_error(self):
        from inbox_server import handle_update_scheduled_job
        result = _run(handle_update_scheduled_job({}))
        assert "Error" in _text(result)

    def test_not_found_returns_error(self):
        with patch("inbox_server._sj_normalize_schedule", return_value=("daily", None)), \
             patch("inbox_server._sj_update_job", new=AsyncMock(side_effect=FileNotFoundError("not found"))):
            from inbox_server import handle_update_scheduled_job
            result = _run(handle_update_scheduled_job({"name": "ghost", "schedule": "daily"}))
        assert "Error" in _text(result)

    def test_no_changes_returns_message(self):
        with patch("inbox_server._sj_update_job", new=AsyncMock(return_value=_UpdateResult("job", []))):
            from inbox_server import handle_update_scheduled_job
            result = _run(handle_update_scheduled_job({"name": "job"}))
        assert "No changes" in _text(result)

    def test_update_schedule_success(self):
        with patch("inbox_server._sj_normalize_schedule", return_value=("weekly", None)), \
             patch("inbox_server._sj_update_job",
                   new=AsyncMock(return_value=_UpdateResult("job", ["schedule"]))):
            from inbox_server import handle_update_scheduled_job
            result = _run(handle_update_scheduled_job({"name": "job", "schedule": "weekly"}))
        assert "Updated" in _text(result)
        assert "schedule" in _text(result)

    def test_invalid_schedule_returns_error(self):
        with patch("inbox_server._sj_normalize_schedule",
                   return_value=("bad", "Invalid schedule 'bad': ...")):
            from inbox_server import handle_update_scheduled_job
            result = _run(handle_update_scheduled_job({"name": "job", "schedule": "bad"}))
        assert "Error" in _text(result)

    def test_invalid_command_returns_error(self):
        with patch("inbox_server._sj_validate_command", return_value="command must be an absolute path"):
            from inbox_server import handle_update_scheduled_job
            result = _run(handle_update_scheduled_job({"name": "job", "command": "relative"}))
        assert "Error" in _text(result)

    def test_disable_job(self):
        """enabled=False should call update_job with enabled=False."""
        captured = {}

        async def fake_update(name, schedule=None, command=None, enabled=None):
            captured["enabled"] = enabled
            return _UpdateResult(name, ["enabled"])

        with patch("inbox_server._sj_update_job", side_effect=fake_update):
            from inbox_server import handle_update_scheduled_job
            result = _run(handle_update_scheduled_job({"name": "job", "enabled": False}))
        assert "Updated" in _text(result)
        assert captured["enabled"] is False

    def test_enable_job(self):
        """enabled=True should call update_job with enabled=True."""
        captured = {}

        async def fake_update(name, schedule=None, command=None, enabled=None):
            captured["enabled"] = enabled
            return _UpdateResult(name, ["enabled"])

        with patch("inbox_server._sj_update_job", side_effect=fake_update):
            from inbox_server import handle_update_scheduled_job
            result = _run(handle_update_scheduled_job({"name": "job", "enabled": True}))
        assert "Updated" in _text(result)
        assert captured["enabled"] is True

    def test_cron_expression_is_normalized_to_systemd_format(self):
        """Cron expressions passed to update_scheduled_job must be converted to
        systemd calendar format before the unit file is written. If normalize_schedule
        is not called, a raw cron string (e.g. '0 9 * * *') is written directly into
        OnCalendar= and systemd silently rejects it at daemon-reload time."""
        captured = {}

        async def fake_update_job(name, schedule=None, command=None, enabled=None):
            captured["schedule"] = schedule
            return _UpdateResult(name, ["schedule"] if schedule else [])

        with patch("inbox_server._sj_normalize_schedule", return_value=("*-*-* 09:00:00", None)), \
             patch("inbox_server._sj_update_job", side_effect=fake_update_job):
            from inbox_server import handle_update_scheduled_job
            _run(handle_update_scheduled_job({"name": "job", "schedule": "0 9 * * *"}))

        # The schedule passed to the backend must be in systemd format, not cron
        assert captured["schedule"] == "*-*-* 09:00:00", (
            f"Expected systemd calendar format '*-*-* 09:00:00', got: {captured['schedule']!r}"
        )


# ---------------------------------------------------------------------------
# delete_scheduled_job
# ---------------------------------------------------------------------------

class TestHandleDeleteScheduledJob:
    def test_missing_name_returns_error(self):
        from inbox_server import handle_delete_scheduled_job
        result = _run(handle_delete_scheduled_job({}))
        assert "Error" in _text(result)

    def test_not_found_returns_message(self):
        with patch("inbox_server._sj_delete_job",
                   new=AsyncMock(return_value=_DeleteResult("ghost", "not_found"))):
            from inbox_server import handle_delete_scheduled_job
            result = _run(handle_delete_scheduled_job({"name": "ghost"}))
        assert "not found" in _text(result).lower()

    def test_deleted_returns_success(self):
        with patch("inbox_server._sj_delete_job",
                   new=AsyncMock(return_value=_DeleteResult("job", "deleted"))):
            from inbox_server import handle_delete_scheduled_job
            result = _run(handle_delete_scheduled_job({"name": "job"}))
        assert "Deleted" in _text(result)

    def test_permission_error_returns_error(self):
        with patch("inbox_server._sj_delete_job",
                   new=AsyncMock(side_effect=PermissionError("not lobster-managed"))):
            from inbox_server import handle_delete_scheduled_job
            result = _run(handle_delete_scheduled_job({"name": "job"}))
        assert "Error" in _text(result)


# ---------------------------------------------------------------------------
# get_job_scaffold
# ---------------------------------------------------------------------------

class TestHandleGetJobScaffold:
    def test_returns_scaffold_content(self):
        with patch("inbox_server._sj_get_scaffold", return_value="# scaffold content\n"):
            from inbox_server import handle_get_job_scaffold
            result = _run(handle_get_job_scaffold({}))
        text = _text(result)
        assert "scaffold content" in text
        assert "```python" in text

    def test_default_kind_is_poller(self):
        captured = {}

        def fake_scaffold(kind):
            captured["kind"] = kind
            return "# scaffold"

        with patch("inbox_server._sj_get_scaffold", side_effect=fake_scaffold):
            from inbox_server import handle_get_job_scaffold
            _run(handle_get_job_scaffold({}))

        assert captured["kind"] == "poller"

    def test_custom_kind_passed_through(self):
        captured = {}

        def fake_scaffold(kind):
            captured["kind"] = kind
            return "# scaffold"

        with patch("inbox_server._sj_get_scaffold", side_effect=fake_scaffold):
            from inbox_server import handle_get_job_scaffold
            _run(handle_get_job_scaffold({"kind": "api-poll"}))

        assert captured["kind"] == "api-poll"
