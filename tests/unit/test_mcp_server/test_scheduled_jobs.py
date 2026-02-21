"""
Tests for MCP Server Scheduled Jobs Tools

Tests create_scheduled_job, list_scheduled_jobs, get_scheduled_job,
update_scheduled_job, delete_scheduled_job, check_task_outputs, write_task_output
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import asyncio


class TestCreateScheduledJob:
    """Tests for create_scheduled_job tool."""

    @pytest.fixture
    def setup_dirs(self, temp_scheduled_tasks_dir: Path):
        """Set up scheduled tasks directories."""
        jobs_file = temp_scheduled_tasks_dir / "jobs.json"
        tasks_dir = temp_scheduled_tasks_dir / "tasks"
        return jobs_file, tasks_dir, temp_scheduled_tasks_dir

    def test_creates_job(self, setup_dirs):
        """Test that job is created successfully."""
        jobs_file, tasks_dir, base_dir = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_create_scheduled_job

            result = asyncio.run(
                handle_create_scheduled_job({
                    "name": "test-job",
                    "schedule": "0 9 * * *",
                    "context": "Run daily tests",
                })
            )

            assert "Created" in result[0].text
            assert "test-job" in result[0].text

            # Verify jobs.json was updated
            data = json.loads(jobs_file.read_text())
            assert "test-job" in data["jobs"]
            assert data["jobs"]["test-job"]["schedule"] == "0 9 * * *"

            # Verify task file was created
            assert (tasks_dir / "test-job.md").exists()

    def test_validates_job_name(self, setup_dirs):
        """Test that job name is validated."""
        jobs_file, tasks_dir, base_dir = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_create_scheduled_job

            # Invalid name (starts with hyphen)
            result = asyncio.run(
                handle_create_scheduled_job({
                    "name": "-invalid",
                    "schedule": "0 9 * * *",
                    "context": "Test",
                })
            )
            assert "Error" in result[0].text or "invalid" in result[0].text.lower()

            # Invalid name (contains spaces)
            result = asyncio.run(
                handle_create_scheduled_job({
                    "name": "invalid name",
                    "schedule": "0 9 * * *",
                    "context": "Test",
                })
            )
            assert "Error" in result[0].text or "invalid" in result[0].text.lower()

    def test_validates_cron_schedule(self, setup_dirs):
        """Test that cron schedule is validated."""
        jobs_file, tasks_dir, base_dir = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
        ):
            from src.mcp.inbox_server import handle_create_scheduled_job

            # Invalid schedule (not 5 parts)
            result = asyncio.run(
                handle_create_scheduled_job({
                    "name": "test-job",
                    "schedule": "0 9 * *",  # Only 4 parts
                    "context": "Test",
                })
            )
            assert "Error" in result[0].text
            assert "5 parts" in result[0].text

    def test_requires_context(self, setup_dirs):
        """Test that context is required."""
        jobs_file, tasks_dir, base_dir = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
        ):
            from src.mcp.inbox_server import handle_create_scheduled_job

            result = asyncio.run(
                handle_create_scheduled_job({
                    "name": "test-job",
                    "schedule": "0 9 * * *",
                })
            )
            assert "Error" in result[0].text
            assert "context" in result[0].text.lower()

    def test_prevents_duplicate_job(self, setup_dirs):
        """Test that duplicate job name is rejected."""
        jobs_file, tasks_dir, base_dir = setup_dirs

        # Create initial job
        jobs_file.write_text(json.dumps({
            "jobs": {"existing-job": {"name": "existing-job"}}
        }))

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
        ):
            from src.mcp.inbox_server import handle_create_scheduled_job

            result = asyncio.run(
                handle_create_scheduled_job({
                    "name": "existing-job",
                    "schedule": "0 9 * * *",
                    "context": "Test",
                })
            )
            assert "Error" in result[0].text
            assert "already exists" in result[0].text


class TestListScheduledJobs:
    """Tests for list_scheduled_jobs tool."""

    @pytest.fixture
    def jobs_file(self, temp_scheduled_tasks_dir: Path, job_generator) -> Path:
        """Create jobs file with sample jobs."""
        jobs = {}
        for i in range(3):
            job = job_generator.generate_job()
            jobs[job["name"]] = job

        jobs_path = temp_scheduled_tasks_dir / "jobs.json"
        jobs_path.write_text(json.dumps({"jobs": jobs}))
        return jobs_path

    def test_returns_all_jobs(self, jobs_file: Path):
        """Test that all jobs are returned."""
        with patch("src.mcp.inbox_server.SCHEDULED_JOBS_FILE", jobs_file):
            from src.mcp.inbox_server import handle_list_scheduled_jobs

            result = asyncio.run(handle_list_scheduled_jobs({}))

            assert "Scheduled Jobs" in result[0].text
            assert "3" in result[0].text  # Total count

    def test_empty_jobs_returns_message(self, temp_scheduled_tasks_dir: Path):
        """Test that empty jobs list returns appropriate message."""
        empty_jobs = temp_scheduled_tasks_dir / "jobs.json"
        empty_jobs.write_text(json.dumps({"jobs": {}}))

        with patch("src.mcp.inbox_server.SCHEDULED_JOBS_FILE", empty_jobs):
            from src.mcp.inbox_server import handle_list_scheduled_jobs

            result = asyncio.run(handle_list_scheduled_jobs({}))

            assert "No scheduled jobs" in result[0].text


class TestGetScheduledJob:
    """Tests for get_scheduled_job tool."""

    @pytest.fixture
    def setup_job(self, temp_scheduled_tasks_dir: Path, job_generator):
        """Create a job with task file."""
        job = job_generator.generate_job(name="test-job")
        jobs_file = temp_scheduled_tasks_dir / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {"test-job": job}}))

        tasks_dir = temp_scheduled_tasks_dir / "tasks"
        (tasks_dir / "test-job.md").write_text("# Test Job\n\nInstructions here")

        return jobs_file, tasks_dir

    def test_returns_job_details(self, setup_job, temp_scheduled_tasks_dir):
        """Test that job details are returned."""
        jobs_file, tasks_dir = setup_job

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
        ):
            from src.mcp.inbox_server import handle_get_scheduled_job

            result = asyncio.run(handle_get_scheduled_job({"name": "test-job"}))

            text = result[0].text
            assert "test-job" in text
            assert "Schedule" in text
            assert "Test Job" in text  # Task file content

    def test_nonexistent_job_returns_error(self, setup_job):
        """Test that nonexistent job returns error."""
        jobs_file, _ = setup_job

        with patch("src.mcp.inbox_server.SCHEDULED_JOBS_FILE", jobs_file):
            from src.mcp.inbox_server import handle_get_scheduled_job

            result = asyncio.run(handle_get_scheduled_job({"name": "nonexistent"}))

            assert "Error" in result[0].text
            assert "not found" in result[0].text.lower()


class TestUpdateScheduledJob:
    """Tests for update_scheduled_job tool."""

    @pytest.fixture
    def setup_job(self, temp_scheduled_tasks_dir: Path, job_generator):
        """Create a job for updating."""
        job = job_generator.generate_job(name="test-job", schedule="0 9 * * *")
        jobs_file = temp_scheduled_tasks_dir / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {"test-job": job}}))

        tasks_dir = temp_scheduled_tasks_dir / "tasks"
        (tasks_dir / "test-job.md").write_text("# Original content")

        return jobs_file, tasks_dir, temp_scheduled_tasks_dir

    def test_updates_schedule(self, setup_job):
        """Test that schedule can be updated."""
        jobs_file, tasks_dir, base_dir = setup_job

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_update_scheduled_job

            result = asyncio.run(
                handle_update_scheduled_job({
                    "name": "test-job",
                    "schedule": "30 8 * * *",
                })
            )

            assert "Updated" in result[0].text

            data = json.loads(jobs_file.read_text())
            assert data["jobs"]["test-job"]["schedule"] == "30 8 * * *"

    def test_updates_enabled_status(self, setup_job):
        """Test that enabled status can be updated."""
        jobs_file, tasks_dir, base_dir = setup_job

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_update_scheduled_job

            asyncio.run(
                handle_update_scheduled_job({
                    "name": "test-job",
                    "enabled": False,
                })
            )

            data = json.loads(jobs_file.read_text())
            assert data["jobs"]["test-job"]["enabled"] is False

    def test_updates_context_rewrites_task_file(self, setup_job):
        """Test that updating context rewrites task file."""
        jobs_file, tasks_dir, base_dir = setup_job

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_update_scheduled_job

            asyncio.run(
                handle_update_scheduled_job({
                    "name": "test-job",
                    "context": "New instructions here",
                })
            )

            task_content = (tasks_dir / "test-job.md").read_text()
            assert "New instructions" in task_content

    def test_no_changes_returns_message(self, setup_job):
        """Test that no changes returns appropriate message."""
        jobs_file, tasks_dir, base_dir = setup_job

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
        ):
            from src.mcp.inbox_server import handle_update_scheduled_job

            result = asyncio.run(
                handle_update_scheduled_job({"name": "test-job"})
            )

            assert "No changes" in result[0].text


class TestDeleteScheduledJob:
    """Tests for delete_scheduled_job tool."""

    @pytest.fixture
    def setup_job(self, temp_scheduled_tasks_dir: Path, job_generator):
        """Create a job for deletion."""
        job = job_generator.generate_job(name="test-job")
        jobs_file = temp_scheduled_tasks_dir / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {"test-job": job}}))

        tasks_dir = temp_scheduled_tasks_dir / "tasks"
        (tasks_dir / "test-job.md").write_text("# Task content")

        return jobs_file, tasks_dir, temp_scheduled_tasks_dir

    def test_deletes_job(self, setup_job):
        """Test that job is deleted."""
        jobs_file, tasks_dir, base_dir = setup_job

        with patch.multiple(
            "src.mcp.inbox_server",
            SCHEDULED_JOBS_FILE=jobs_file,
            SCHEDULED_JOBS_DIR=base_dir,
            SCHEDULED_TASKS_TASKS_DIR=tasks_dir,
            sync_crontab=MagicMock(return_value=(True, "")),
        ):
            from src.mcp.inbox_server import handle_delete_scheduled_job

            result = asyncio.run(handle_delete_scheduled_job({"name": "test-job"}))

            assert "Deleted" in result[0].text

            data = json.loads(jobs_file.read_text())
            assert "test-job" not in data["jobs"]
            assert not (tasks_dir / "test-job.md").exists()

    def test_nonexistent_job_returns_error(self, setup_job):
        """Test that deleting nonexistent job returns error."""
        jobs_file, tasks_dir, base_dir = setup_job

        with patch("src.mcp.inbox_server.SCHEDULED_JOBS_FILE", jobs_file):
            from src.mcp.inbox_server import handle_delete_scheduled_job

            result = asyncio.run(
                handle_delete_scheduled_job({"name": "nonexistent"})
            )

            assert "Error" in result[0].text


class TestCheckTaskOutputs:
    """Tests for check_task_outputs tool."""

    @pytest.fixture
    def outputs_dir(self, temp_messages_dir: Path) -> Path:
        """Create task outputs directory with sample outputs."""
        outputs = temp_messages_dir / "task-outputs"

        # Create some output files
        for i in range(5):
            output = {
                "job_name": f"job-{i % 2}",
                "timestamp": f"2024-01-0{i+1}T09:00:00+00:00",
                "status": "success" if i % 3 != 0 else "failed",
                "output": f"Output from job {i}",
            }
            (outputs / f"2024010{i+1}-090000-job-{i % 2}.json").write_text(
                json.dumps(output)
            )

        return outputs

    def test_returns_recent_outputs(self, outputs_dir: Path):
        """Test that recent outputs are returned."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_check_task_outputs

            result = asyncio.run(handle_check_task_outputs({}))

            assert "Task Outputs" in result[0].text
            assert "Output from job" in result[0].text

    def test_filters_by_job_name(self, outputs_dir: Path):
        """Test that job_name filter works."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_check_task_outputs

            result = asyncio.run(
                handle_check_task_outputs({"job_name": "job-0"})
            )

            assert "job-0" in result[0].text
            # Should not include job-1
            # (This depends on the specific filtering logic)

    def test_respects_limit(self, outputs_dir: Path):
        """Test that limit parameter works."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_check_task_outputs

            result = asyncio.run(handle_check_task_outputs({"limit": 2}))

            # Should show (2) in the count
            assert "(2)" in result[0].text or "2)" in result[0].text

    def test_empty_outputs_returns_message(self, temp_messages_dir: Path):
        """Test that empty outputs returns appropriate message."""
        empty_outputs = temp_messages_dir / "task-outputs"
        # Directory exists but is empty

        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", empty_outputs):
            from src.mcp.inbox_server import handle_check_task_outputs

            result = asyncio.run(handle_check_task_outputs({}))

            assert "No task outputs" in result[0].text


class TestWriteTaskOutput:
    """Tests for write_task_output tool."""

    @pytest.fixture
    def outputs_dir(self, temp_messages_dir: Path) -> Path:
        """Get task outputs directory."""
        return temp_messages_dir / "task-outputs"

    def test_writes_output_file(self, outputs_dir: Path):
        """Test that output file is created."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_write_task_output

            result = asyncio.run(
                handle_write_task_output({
                    "job_name": "test-job",
                    "output": "Test output content",
                    "status": "success",
                })
            )

            assert "recorded" in result[0].text.lower()

            files = list(outputs_dir.glob("*.json"))
            assert len(files) == 1

            content = json.loads(files[0].read_text())
            assert content["job_name"] == "test-job"
            assert content["output"] == "Test output content"
            assert content["status"] == "success"

    def test_requires_job_name(self, outputs_dir: Path):
        """Test that job_name is required."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_write_task_output

            result = asyncio.run(
                handle_write_task_output({"output": "Test"})
            )

            assert "Error" in result[0].text

    def test_requires_output(self, outputs_dir: Path):
        """Test that output is required."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_write_task_output

            result = asyncio.run(
                handle_write_task_output({"job_name": "test"})
            )

            assert "Error" in result[0].text

    def test_defaults_status_to_success(self, outputs_dir: Path):
        """Test that status defaults to success."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_write_task_output

            asyncio.run(
                handle_write_task_output({
                    "job_name": "test-job",
                    "output": "Test",
                })
            )

            files = list(outputs_dir.glob("*.json"))
            content = json.loads(files[0].read_text())
            assert content["status"] == "success"
