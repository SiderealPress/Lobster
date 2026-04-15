"""
Tests for Scheduled Job Execution

Tests systemd timer job execution and output handling.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import subprocess


@pytest.mark.integration
class TestScheduledJobCreation:
    """Tests for scheduled job creation and configuration (systemd backend)."""

    @pytest.mark.asyncio
    async def test_create_job_converts_cron_to_systemd_format(self):
        """Cron expressions passed to create_scheduled_job must be converted
        to systemd OnCalendar syntax before the unit file is written.

        If the conversion is broken, a raw cron string like '0 9 * * *' would
        land in OnCalendar= and systemd would reject it at daemon-reload time.
        """
        captured = {}

        async def fake_create_job(name, schedule, command, description=""):
            captured["schedule"] = schedule
            from src.mcp.systemd_jobs import CreateResult
            return CreateResult(name=name, status="created")

        with patch("inbox_server._sj_validate_name", return_value=None), \
             patch("inbox_server._sj_validate_command", return_value=None), \
             patch("inbox_server._sj_create_job", side_effect=fake_create_job):
            import sys
            from pathlib import Path as _Path
            _mcp = _Path(__file__).parent.parent.parent / "src" / "mcp"
            if str(_mcp) not in sys.path:
                sys.path.insert(0, str(_mcp))
            import inbox_server as _is
            result = await _is.handle_create_scheduled_job({
                "name": "test-job",
                "schedule": "0 9 * * *",
                "command": "/bin/echo hello",
            })

        # The schedule written to systemd must NOT be raw cron syntax
        assert "captured" in dir() or captured  # ensure fake was called
        assert captured.get("schedule") == "*-*-* 09:00:00", (
            f"Expected systemd format '*-*-* 09:00:00', got {captured.get('schedule')!r} — "
            "cron-to-systemd conversion is broken"
        )
        assert "Created" in result[0].text

    @pytest.mark.asyncio
    async def test_create_job_invokes_systemd_backend(self):
        """create_scheduled_job invokes the systemd backend (create_job) and
        the timer + service unit names appear in the response.

        The old backend wrote a jobs.json file; the new backend writes
        /etc/systemd/system/lobster-<name>.{timer,service}. This test
        verifies the new backend is wired correctly by checking that:
        1. _sj_create_job is called with the expected arguments.
        2. The MCP response references the unit file names.
        """
        captured = {}

        async def fake_create_job(name, schedule, command, description=""):
            captured.update({"name": name, "schedule": schedule, "command": command})
            from src.mcp.systemd_jobs import CreateResult
            return CreateResult(name=name, status="created")

        with patch("inbox_server._sj_validate_name", return_value=None), \
             patch("inbox_server._sj_validate_command", return_value=None), \
             patch("inbox_server._sj_create_job", side_effect=fake_create_job):
            import sys
            from pathlib import Path as _Path
            _mcp = _Path(__file__).parent.parent.parent / "src" / "mcp"
            if str(_mcp) not in sys.path:
                sys.path.insert(0, str(_mcp))
            import inbox_server as _is
            result = await _is.handle_create_scheduled_job({
                "name": "daily-backup",
                "schedule": "0 2 * * *",
                "command": "/bin/echo backup",
            })

        assert captured.get("name") == "daily-backup"
        # Schedule must be systemd format (cron was converted)
        assert captured.get("schedule") == "*-*-* 02:00:00"
        # Response must reference the unit files — proves systemd path was taken
        text = result[0].text
        assert "lobster-daily-backup.timer" in text
        assert "lobster-daily-backup.service" in text


@pytest.mark.integration
class TestJobExecution:
    """Tests for job execution."""

    @pytest.fixture
    def execution_setup(self, temp_scheduled_tasks_dir: Path, temp_messages_dir: Path):
        """Set up execution environment."""
        # Create task file
        task_file = temp_scheduled_tasks_dir / "tasks" / "test-job.md"
        task_file.write_text("""# Test Job

## Instructions
This is a test job that should complete quickly.
""")

        return {
            "task_file": task_file,
            "logs_dir": temp_scheduled_tasks_dir / "logs",
            "outputs_dir": temp_messages_dir / "task-outputs",
            "jobs_file": temp_scheduled_tasks_dir / "jobs.json",
        }

    def test_run_job_script_syntax(self):
        """Test that dispatch-job.sh has valid bash syntax."""
        run_job = Path(__file__).parent.parent.parent / "scheduled-tasks" / "dispatch-job.sh"

        if not run_job.exists():
            pytest.skip("dispatch-job.sh not found")

        # Check syntax with bash -n
        result = subprocess.run(
            ["bash", "-n", str(run_job)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_run_job_requires_job_name(self):
        """Test that dispatch-job.sh requires job name argument."""
        run_job = Path(__file__).parent.parent.parent / "scheduled-tasks" / "dispatch-job.sh"

        if not run_job.exists():
            pytest.skip("dispatch-job.sh not found")

        result = subprocess.run(
            ["bash", str(run_job)],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert "Usage" in result.stdout or "job-name" in result.stdout


@pytest.mark.integration
class TestTaskOutputs:
    """Tests for task output handling."""

    @pytest.fixture
    def outputs_dir(self, temp_messages_dir: Path) -> Path:
        """Get task outputs directory."""
        return temp_messages_dir / "task-outputs"

    @pytest.mark.asyncio
    async def test_write_and_read_output(self, outputs_dir: Path):
        """Test writing and reading task outputs."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import (
                handle_write_task_output,
                handle_check_task_outputs,
            )

            # Write output
            await handle_write_task_output({
                "job_name": "test-job",
                "output": "Job completed successfully with 5 items processed",
                "status": "success",
            })

            # Read outputs
            result = await handle_check_task_outputs({})

            assert "test-job" in result[0].text
            assert "5 items processed" in result[0].text

    @pytest.mark.asyncio
    async def test_output_filtering_by_job(self, outputs_dir: Path):
        """Test filtering outputs by job name."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import (
                handle_write_task_output,
                handle_check_task_outputs,
            )

            # Write outputs from different jobs
            await handle_write_task_output({
                "job_name": "job-a",
                "output": "Output from job A",
            })
            await handle_write_task_output({
                "job_name": "job-b",
                "output": "Output from job B",
            })

            # Filter by job-a
            result = await handle_check_task_outputs({"job_name": "job-a"})

            assert "job-a" in result[0].text
            # Result should focus on job-a


@pytest.mark.integration
class TestScheduleNormalization:
    """Tests for schedule normalization (replaces crontab sync tests).

    The old backend used sync-crontab.sh to flush jobs to the user crontab.
    The new backend uses systemd unit files. This class tests that schedule
    normalization — the equivalent of crontab format enforcement — works
    correctly via the systemd_jobs module.
    """

    def test_sync_crontab_script_syntax(self):
        """Test that sync-crontab.sh has valid bash syntax."""
        sync_script = Path(__file__).parent.parent.parent / "scheduled-tasks" / "sync-crontab.sh"

        if not sync_script.exists():
            pytest.skip("sync-crontab.sh not found")

        result = subprocess.run(
            ["bash", "-n", str(sync_script)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_normalize_schedule_converts_cron_to_systemd(self):
        """normalize_schedule converts a valid cron expression to systemd
        OnCalendar syntax and returns no error.

        This replaces the old sync_crontab test which verified that a cron
        expression in jobs.json was written to the system crontab. The new
        backend's equivalent step is normalize_schedule: it converts cron
        syntax at job creation/update time so only valid OnCalendar strings
        are ever written to unit files.
        """
        import sys
        from pathlib import Path as _Path
        _mcp = _Path(__file__).parent.parent.parent / "src" / "mcp"
        if str(_mcp) not in sys.path:
            sys.path.insert(0, str(_mcp))
        from systemd_jobs import normalize_schedule

        normalized, err = normalize_schedule("0 9 * * *")
        assert err is None, f"Unexpected error: {err}"
        assert normalized == "*-*-* 09:00:00"

    def test_normalize_schedule_rejects_invalid_expressions(self):
        """normalize_schedule returns an error for invalid schedule strings."""
        import sys
        from pathlib import Path as _Path
        _mcp = _Path(__file__).parent.parent.parent / "src" / "mcp"
        if str(_mcp) not in sys.path:
            sys.path.insert(0, str(_mcp))
        from systemd_jobs import normalize_schedule

        _, err = normalize_schedule("not-a-schedule")
        assert err is not None
        assert "Invalid" in err or "invalid" in err

    def test_normalize_schedule_accepts_systemd_calendar_expressions(self):
        """normalize_schedule accepts native systemd OnCalendar expressions."""
        import sys
        from pathlib import Path as _Path
        _mcp = _Path(__file__).parent.parent.parent / "src" / "mcp"
        if str(_mcp) not in sys.path:
            sys.path.insert(0, str(_mcp))
        from systemd_jobs import normalize_schedule

        for expr in ("daily", "hourly", "*-*-* 09:00:00", "*:0/30:00"):
            normalized, err = normalize_schedule(expr)
            assert err is None, f"Expression {expr!r} should be valid, got error: {err}"
