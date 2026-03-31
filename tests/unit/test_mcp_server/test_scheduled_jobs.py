"""
Tests for scheduled job MCP tool handlers (systemd backend).

The create/list/get/update/delete handlers are covered by
tests/unit/test_scheduled_jobs_handlers.py, which mocks the
systemd_jobs module directly.

This file covers the task output tools (check_task_outputs,
write_task_output) which are not part of the systemd migration
and remain file-based.
"""

import json
import pytest
import asyncio
from pathlib import Path
from unittest.mock import patch


class TestCheckTaskOutputs:
    """Tests for check_task_outputs tool."""

    @pytest.fixture
    def outputs_dir(self, temp_messages_dir: Path) -> Path:
        """Create task outputs directory with sample outputs."""
        outputs = temp_messages_dir / "task-outputs"

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

    def test_respects_limit(self, outputs_dir: Path):
        """Test that limit parameter works."""
        with patch("src.mcp.inbox_server.TASK_OUTPUTS_DIR", outputs_dir):
            from src.mcp.inbox_server import handle_check_task_outputs

            result = asyncio.run(handle_check_task_outputs({"limit": 2}))

            assert "(2)" in result[0].text or "2)" in result[0].text

    def test_empty_outputs_returns_message(self, temp_messages_dir: Path):
        """Test that empty outputs returns appropriate message."""
        empty_outputs = temp_messages_dir / "task-outputs"

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
