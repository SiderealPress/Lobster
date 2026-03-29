"""
Unit tests for systemd_jobs.py — the systemd timer backend for scheduled jobs.

All systemctl calls are mocked. No real systemd interaction occurs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

import systemd_jobs as sj


# ---------------------------------------------------------------------------
# Validation (pure functions — no mocking needed)
# ---------------------------------------------------------------------------

class TestValidateName:
    def test_valid_simple(self):
        assert sj.validate_name("test") is None

    def test_valid_with_hyphens(self):
        assert sj.validate_name("morning-weather") is None

    def test_valid_alphanumeric(self):
        assert sj.validate_name("job1") is None

    def test_empty_is_invalid(self):
        assert sj.validate_name("") is not None

    def test_starts_with_hyphen(self):
        assert sj.validate_name("-invalid") is not None

    def test_ends_with_hyphen(self):
        assert sj.validate_name("invalid-") is not None

    def test_uppercase_invalid(self):
        assert sj.validate_name("MyJob") is not None

    def test_spaces_invalid(self):
        assert sj.validate_name("my job") is not None

    def test_too_long(self):
        long_name = "a" * 51
        assert sj.validate_name(long_name) is not None

    def test_exactly_max_length(self):
        name = "a" * 50
        assert sj.validate_name(name) is None

    def test_single_char_valid(self):
        assert sj.validate_name("a") is None


class TestValidateCommand:
    def test_absolute_path_valid(self):
        assert sj.validate_command("/bin/echo") is None

    def test_relative_path_invalid(self):
        assert sj.validate_command("bin/echo") is not None

    def test_no_leading_slash_invalid(self):
        assert sj.validate_command("echo") is not None

    def test_empty_invalid(self):
        assert sj.validate_command("") is not None

    def test_path_with_args(self):
        assert sj.validate_command("/bin/echo hello") is None


class TestValidateSchedule:
    def test_nonempty_valid(self):
        assert sj.validate_schedule("*-*-* 09:00:00") is None

    def test_cron_style_valid(self):
        assert sj.validate_schedule("0 9 * * *") is None

    def test_empty_invalid(self):
        assert sj.validate_schedule("") is not None


# ---------------------------------------------------------------------------
# Unit file generation (pure functions)
# ---------------------------------------------------------------------------

class TestTimerUnit:
    def test_contains_on_calendar(self):
        content = sj._timer_unit("myjob", "*-*-* 09:00:00", "My Job")
        assert "OnCalendar=*-*-* 09:00:00" in content

    def test_contains_lobster_marker(self):
        content = sj._timer_unit("myjob", "daily", "")
        assert sj.LOBSTER_MARKER in content

    def test_contains_description(self):
        content = sj._timer_unit("myjob", "daily", "Custom desc")
        assert "Custom desc" in content

    def test_default_description(self):
        content = sj._timer_unit("myjob", "daily", "")
        assert "myjob" in content

    def test_persistent_true(self):
        content = sj._timer_unit("myjob", "daily", "")
        assert "Persistent=true" in content

    def test_wanted_by_timers_target(self):
        content = sj._timer_unit("myjob", "daily", "")
        assert "WantedBy=timers.target" in content


class TestServiceUnit:
    def test_contains_exec_start(self):
        content = sj._service_unit("myjob", "/bin/echo test", "")
        assert "ExecStart=/bin/echo test" in content

    def test_contains_lobster_marker(self):
        content = sj._service_unit("myjob", "/bin/echo", "")
        assert sj.LOBSTER_MARKER in content

    def test_type_oneshot(self):
        content = sj._service_unit("myjob", "/bin/echo", "")
        assert "Type=oneshot" in content

    def test_user_lobster(self):
        content = sj._service_unit("myjob", "/bin/echo", "")
        assert f"User={sj.LOBSTER_USER}" in content


class TestUnitPaths:
    def test_timer_path(self):
        p = sj._timer_path("test-job")
        assert p == sj.SYSTEMD_DIR / "lobster-test-job.timer"

    def test_service_path(self):
        p = sj._service_path("test-job")
        assert p == sj.SYSTEMD_DIR / "lobster-test-job.service"

    def test_unit_name(self):
        assert sj._unit_name("test-job") == "lobster-test-job"


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

class TestIsLobsterUnit:
    def test_returns_true_when_marker_present(self, tmp_path):
        p = tmp_path / "test.timer"
        p.write_text(f"[Unit]\n{sj.LOBSTER_MARKER}\n")
        assert sj._is_lobster_unit(p) is True

    def test_returns_false_when_no_marker(self, tmp_path):
        p = tmp_path / "test.timer"
        p.write_text("[Unit]\nDescription=Something\n")
        assert sj._is_lobster_unit(p) is False

    def test_returns_false_for_missing_file(self, tmp_path):
        p = tmp_path / "nonexistent.timer"
        assert sj._is_lobster_unit(p) is False


class TestReadUnitField:
    def test_reads_existing_field(self, tmp_path):
        p = tmp_path / "test.timer"
        p.write_text("[Timer]\nOnCalendar=daily\nPersistent=true\n")
        assert sj._read_unit_field(p, "OnCalendar") == "daily"

    def test_returns_none_for_missing_field(self, tmp_path):
        p = tmp_path / "test.timer"
        p.write_text("[Timer]\nPersistent=true\n")
        assert sj._read_unit_field(p, "OnCalendar") is None

    def test_returns_none_for_missing_file(self, tmp_path):
        p = tmp_path / "nonexistent.timer"
        assert sj._read_unit_field(p, "OnCalendar") is None


# ---------------------------------------------------------------------------
# Core operations (systemctl calls mocked)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_systemctl():
    """Patch _run_systemctl to succeed without calling sudo."""
    with patch.object(sj, "_run_systemctl", new_callable=AsyncMock) as m:
        m.return_value = (0, "", "")
        yield m


@pytest.fixture
def systemd_dir(tmp_path):
    """Redirect SYSTEMD_DIR to a temp path."""
    d = tmp_path / "systemd_system"
    d.mkdir()
    with patch.object(sj, "SYSTEMD_DIR", d):
        yield d


class TestCreateJob:
    def test_creates_unit_files(self, mock_systemctl, systemd_dir):
        asyncio.run(sj.create_job("test-job", "daily", "/bin/echo hi", "Test job"))

        assert (systemd_dir / "lobster-test-job.timer").exists()
        assert (systemd_dir / "lobster-test-job.service").exists()

    def test_returns_created_status(self, mock_systemctl, systemd_dir):
        result = asyncio.run(sj.create_job("test-job", "daily", "/bin/echo hi"))
        assert result.status == "created"
        assert result.name == "test-job"

    def test_calls_daemon_reload_and_enable(self, mock_systemctl, systemd_dir):
        asyncio.run(sj.create_job("test-job", "daily", "/bin/echo hi"))

        calls = [call.args for call in mock_systemctl.call_args_list]
        assert ("daemon-reload",) in calls
        # enable --now lobster-test-job.timer
        assert any("enable" in args and "lobster-test-job.timer" in args for args in calls)

    def test_idempotent_same_schedule_command(self, mock_systemctl, systemd_dir):
        """Second call with same args returns already_exists without rewriting."""
        asyncio.run(sj.create_job("test-job", "daily", "/bin/echo hi"))
        mock_systemctl.reset_mock()

        result = asyncio.run(sj.create_job("test-job", "daily", "/bin/echo hi"))
        assert result.status == "already_exists"
        # No systemctl calls on idempotent path
        mock_systemctl.assert_not_called()

    def test_recreates_if_schedule_differs(self, mock_systemctl, systemd_dir):
        asyncio.run(sj.create_job("test-job", "daily", "/bin/echo hi"))
        mock_systemctl.reset_mock()

        result = asyncio.run(sj.create_job("test-job", "weekly", "/bin/echo hi"))
        assert result.status == "created"
        mock_systemctl.assert_called()

    def test_timer_content(self, mock_systemctl, systemd_dir):
        asyncio.run(sj.create_job("ping", "*-*-* *:*:00", "/bin/ping localhost"))
        timer_content = (systemd_dir / "lobster-ping.timer").read_text()
        assert "OnCalendar=*-*-* *:*:00" in timer_content
        assert sj.LOBSTER_MARKER in timer_content

    def test_service_content(self, mock_systemctl, systemd_dir):
        asyncio.run(sj.create_job("ping", "daily", "/bin/ping localhost"))
        service_content = (systemd_dir / "lobster-ping.service").read_text()
        assert "ExecStart=/bin/ping localhost" in service_content
        assert sj.LOBSTER_MARKER in service_content


class TestUpdateJob:
    def _create_job_files(self, systemd_dir, name, schedule, command):
        timer = systemd_dir / f"lobster-{name}.timer"
        service = systemd_dir / f"lobster-{name}.service"
        timer.write_text(sj._timer_unit(name, schedule, ""))
        service.write_text(sj._service_unit(name, command, ""))

    def test_updates_schedule(self, mock_systemctl, systemd_dir):
        self._create_job_files(systemd_dir, "test-job", "daily", "/bin/echo hi")
        result = asyncio.run(sj.update_job("test-job", schedule="weekly"))
        assert "schedule" in result.updated_fields
        timer = systemd_dir / "lobster-test-job.timer"
        assert "OnCalendar=weekly" in timer.read_text()

    def test_updates_command(self, mock_systemctl, systemd_dir):
        self._create_job_files(systemd_dir, "test-job", "daily", "/bin/echo hi")
        result = asyncio.run(sj.update_job("test-job", command="/bin/echo bye"))
        assert "command" in result.updated_fields
        service = systemd_dir / "lobster-test-job.service"
        assert "ExecStart=/bin/echo bye" in service.read_text()

    def test_no_changes_returns_empty_fields(self, mock_systemctl, systemd_dir):
        self._create_job_files(systemd_dir, "test-job", "daily", "/bin/echo hi")
        result = asyncio.run(sj.update_job("test-job"))
        assert result.updated_fields == []

    def test_raises_if_not_found(self, mock_systemctl, systemd_dir):
        with pytest.raises(FileNotFoundError):
            asyncio.run(sj.update_job("nonexistent"))

    def test_reloads_and_restarts_on_change(self, mock_systemctl, systemd_dir):
        self._create_job_files(systemd_dir, "test-job", "daily", "/bin/echo hi")
        asyncio.run(sj.update_job("test-job", schedule="weekly"))
        calls = [call.args for call in mock_systemctl.call_args_list]
        assert ("daemon-reload",) in calls
        assert any("restart" in args for args in calls)


class TestDeleteJob:
    def test_deletes_unit_files(self, mock_systemctl, systemd_dir):
        timer = systemd_dir / "lobster-test-job.timer"
        service = systemd_dir / "lobster-test-job.service"
        timer.write_text(sj._timer_unit("test-job", "daily", ""))
        service.write_text(sj._service_unit("test-job", "/bin/echo", ""))

        result = asyncio.run(sj.delete_job("test-job"))
        assert result.status == "deleted"
        assert not timer.exists()
        assert not service.exists()

    def test_idempotent_when_not_found(self, mock_systemctl, systemd_dir):
        result = asyncio.run(sj.delete_job("nonexistent"))
        assert result.status == "not_found"

    def test_calls_stop_disable_reload(self, mock_systemctl, systemd_dir):
        timer = systemd_dir / "lobster-test-job.timer"
        service = systemd_dir / "lobster-test-job.service"
        timer.write_text(sj._timer_unit("test-job", "daily", ""))
        service.write_text(sj._service_unit("test-job", "/bin/echo", ""))

        asyncio.run(sj.delete_job("test-job"))
        calls = [call.args for call in mock_systemctl.call_args_list]
        assert ("daemon-reload",) in calls
        assert any("stop" in args for args in calls)
        assert any("disable" in args for args in calls)

    def test_refuses_to_delete_non_lobster_unit(self, mock_systemctl, systemd_dir):
        timer = systemd_dir / "lobster-foreign-job.timer"
        timer.write_text("[Unit]\nDescription=Not managed by lobster\n")

        with pytest.raises(PermissionError):
            asyncio.run(sj.delete_job("foreign-job"))


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------

class TestGetScaffold:
    def test_returns_inline_template_when_no_file(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            content = sj.get_scaffold("poller")
        assert "#!/usr/bin/env python3" in content
        assert "JOB_NAME" in content

    def test_returns_file_template_when_present(self, tmp_path):
        templates_dir = tmp_path / "lobster" / "scheduled-tasks" / "templates"
        templates_dir.mkdir(parents=True)
        template_file = templates_dir / "poller.py.template"
        template_file.write_text("# custom template\nprint('hello')\n")

        with patch.object(Path, "home", return_value=tmp_path):
            content = sj.get_scaffold("poller")
        assert "custom template" in content
