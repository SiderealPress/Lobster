"""
Tests for Scheduled Job Execution

Tests cron job execution and output handling.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess


@pytest.mark.integration
class TestScheduledJobCreation:
    """Tests for scheduled job creation and configuration."""

    @pytest.fixture
    def jobs_setup(self, temp_scheduled_tasks_dir: Path):
        """Set up scheduled jobs environment."""
        return {
            "jobs_file": temp_scheduled_tasks_dir / "jobs.json",
            "tasks_dir": temp_scheduled_tasks_dir / "tasks",
            "logs_dir": temp_scheduled_tasks_dir / "logs",
            "base_dir": temp_scheduled_tasks_dir,
        }

    @pytest.mark.asyncio
    async def test_create_job_creates_task_file(self, jobs_setup):
        """Test that creating a job creates the task markdown file."""
        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_setup["jobs_file"],
            SCHEDULED_JOBS_DIR=jobs_setup["base_dir"],
            SCHEDULED_TASKS_TASKS_DIR=jobs_setup["tasks_dir"],
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_create_scheduled_job

            await handle_create_scheduled_job({
                "name": "test-job",
                "schedule": "0 9 * * *",
                "context": "Run daily tests and report results",
            })

            # Verify task file exists
            task_file = jobs_setup["tasks_dir"] / "test-job.md"
            assert task_file.exists()

            content = task_file.read_text()
            assert "test-job" in content.lower() or "Test Job" in content
            assert "Run daily tests" in content

    @pytest.mark.asyncio
    async def test_job_registered_in_jobs_json(self, jobs_setup):
        """Test that job is registered in jobs.json."""
        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_setup["jobs_file"],
            SCHEDULED_JOBS_DIR=jobs_setup["base_dir"],
            SCHEDULED_TASKS_TASKS_DIR=jobs_setup["tasks_dir"],
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_create_scheduled_job

            await handle_create_scheduled_job({
                "name": "daily-backup",
                "schedule": "0 2 * * *",
                "context": "Run daily backup",
            })

            jobs_data = json.loads(jobs_setup["jobs_file"].read_text())

            assert "daily-backup" in jobs_data["jobs"]
            job = jobs_data["jobs"]["daily-backup"]
            assert job["schedule"] == "0 2 * * *"
            assert job["enabled"] is True


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
class TestCrontabSync:
    """Tests for crontab synchronization."""

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

    def test_sync_crontab_function(self, temp_scheduled_tasks_dir: Path):
        """Test the sync_crontab function."""
        # Create a mock jobs file
        jobs_file = temp_scheduled_tasks_dir / "jobs.json"
        jobs_file.write_text(json.dumps({
            "jobs": {
                "test-job": {
                    "name": "test-job",
                    "schedule": "0 9 * * *",
                    "enabled": True,
                }
            }
        }))

        # Note: Actually syncing crontab requires cron to be running
        # and could affect the system, so we skip actual sync

        # Just verify the function doesn't crash
        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            _REPO_DIR=temp_scheduled_tasks_dir.parent.parent,
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="Synced", stderr=""
                )

                from src.mcp.inbox_server import sync_crontab

                success, msg = sync_crontab()

                # Function should complete (actual success depends on system)
                assert isinstance(success, bool)
