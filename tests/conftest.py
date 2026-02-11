"""
Lobster Test Suite - Shared Fixtures and Configuration

This module provides pytest fixtures shared across all test modules.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator, Generator
from unittest.mock import MagicMock, patch

import pytest

# Add source and tests directories to path
SRC_DIR = Path(__file__).parent.parent / "src"
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))

# Import fixtures module
from tests.fixtures.generators import (
    MessageGenerator,
    TaskGenerator,
    ScheduledJobGenerator,
    FixtureLoader,
)


# =============================================================================
# Directory Fixtures
# =============================================================================


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files."""
    tmp = tempfile.mkdtemp(prefix="lobster_test_")
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def temp_messages_dir(temp_dir: Path) -> Path:
    """Create a temporary messages directory structure."""
    messages_dir = temp_dir / "messages"
    for subdir in ["inbox", "outbox", "processed", "processing", "failed", "config", "audio", "task-outputs"]:
        (messages_dir / subdir).mkdir(parents=True)
    return messages_dir


@pytest.fixture
def temp_scheduled_tasks_dir(temp_dir: Path) -> Path:
    """Create a temporary scheduled tasks directory structure."""
    tasks_dir = temp_dir / "lobster" / "scheduled-tasks"
    (tasks_dir / "tasks").mkdir(parents=True)
    (tasks_dir / "logs").mkdir(parents=True)
    # Initialize jobs.json
    (tasks_dir / "jobs.json").write_text(json.dumps({"jobs": {}}))
    return tasks_dir


@pytest.fixture
def temp_workspace(temp_dir: Path) -> Path:
    """Create a temporary workspace directory."""
    workspace = temp_dir / "lobster-workspace"
    workspace.mkdir(parents=True)
    (workspace / "logs").mkdir()
    return workspace


# =============================================================================
# Generator Fixtures
# =============================================================================


@pytest.fixture
def message_generator() -> MessageGenerator:
    """Create a message generator with fixed seed for reproducibility."""
    return MessageGenerator(seed=42)


@pytest.fixture
def task_generator() -> TaskGenerator:
    """Create a task generator with fixed seed for reproducibility."""
    return TaskGenerator(seed=42)


@pytest.fixture
def job_generator() -> ScheduledJobGenerator:
    """Create a scheduled job generator."""
    return ScheduledJobGenerator()


@pytest.fixture
def fixture_loader() -> FixtureLoader:
    """Create a fixture loader."""
    return FixtureLoader()


# =============================================================================
# Sample Data Fixtures
# =============================================================================


@pytest.fixture
def sample_text_message(message_generator: MessageGenerator) -> dict:
    """Generate a single sample text message."""
    return message_generator.generate_text_message(
        text="Hello, this is a test message",
        source="telegram",
        user_name="TestUser",
        username="testuser",
        user_id=123456,
        chat_id=123456,
    )


@pytest.fixture
def sample_voice_message(message_generator: MessageGenerator) -> dict:
    """Generate a single sample voice message."""
    return message_generator.generate_voice_message(
        duration=10,
        source="telegram",
        user_name="TestUser",
        username="testuser",
        user_id=123456,
        chat_id=123456,
    )


@pytest.fixture
def sample_task(task_generator: TaskGenerator) -> dict:
    """Generate a single sample task."""
    return task_generator.generate_task(
        subject="Test Task",
        description="This is a test task for unit testing",
        status="pending",
    )


@pytest.fixture
def sample_scheduled_job(job_generator: ScheduledJobGenerator) -> dict:
    """Generate a single sample scheduled job."""
    return job_generator.generate_job(
        name="test-job",
        schedule="0 9 * * *",
        context="This is a test scheduled job",
        enabled=True,
    )


@pytest.fixture
def sample_messages_batch(message_generator: MessageGenerator) -> list[dict]:
    """Generate a batch of sample messages."""
    return message_generator.generate_batch(10)


@pytest.fixture
def edge_case_messages(message_generator: MessageGenerator) -> list[dict]:
    """Generate edge case messages for testing."""
    return message_generator.generate_edge_case_messages()


# =============================================================================
# File-based Fixtures
# =============================================================================


@pytest.fixture
def inbox_with_messages(
    temp_messages_dir: Path, sample_messages_batch: list[dict]
) -> Path:
    """Create an inbox directory populated with messages."""
    inbox = temp_messages_dir / "inbox"
    for msg in sample_messages_batch:
        msg_file = inbox / f"{msg['id']}.json"
        msg_file.write_text(json.dumps(msg, indent=2))
    return inbox


@pytest.fixture
def tasks_file(temp_messages_dir: Path, task_generator: TaskGenerator) -> Path:
    """Create a tasks.json file with sample tasks."""
    tasks = task_generator.generate_batch(5)
    tasks_data = {"tasks": tasks, "next_id": 6}
    tasks_file = temp_messages_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_data, indent=2))
    return tasks_file


@pytest.fixture
def jobs_file(
    temp_scheduled_tasks_dir: Path, job_generator: ScheduledJobGenerator
) -> Path:
    """Create a jobs.json file with sample jobs."""
    jobs = {}
    for _ in range(3):
        job = job_generator.generate_job()
        jobs[job["name"]] = job

    jobs_file = temp_scheduled_tasks_dir / "jobs.json"
    jobs_file.write_text(json.dumps({"jobs": jobs}, indent=2))
    return jobs_file


# =============================================================================
# Mock Fixtures
# =============================================================================


@pytest.fixture
def mock_telegram_api():
    """Mock Telegram API responses."""
    with patch("telegram.Bot") as mock_bot:
        mock_bot.return_value.send_message = MagicMock(return_value=MagicMock())
        mock_bot.return_value.get_file = MagicMock(return_value=MagicMock())
        yield mock_bot


@pytest.fixture
def mock_claude_cli():
    """Mock Claude CLI invocation."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_process = MagicMock()
        mock_process.communicate = MagicMock(
            return_value=(b"Mock Claude response", b"")
        )
        mock_process.returncode = 0
        mock_exec.return_value = mock_process
        yield mock_exec


@pytest.fixture
def mock_whisper():
    """Mock Whisper model for transcription."""
    with patch("whisper.load_model") as mock_load:
        mock_model = MagicMock()
        mock_model.transcribe = MagicMock(
            return_value={"text": "This is a mock transcription"}
        )
        mock_load.return_value = mock_model
        yield mock_model


# =============================================================================
# MCP Server Fixtures
# =============================================================================


@pytest.fixture
def mcp_directories(temp_messages_dir: Path, temp_scheduled_tasks_dir: Path):
    """
    Patch MCP server directories to use temporary directories.

    This fixture patches the global directory constants in inbox_server.py.
    """
    with patch.multiple(
        "mcp.inbox_server",
        BASE_DIR=temp_messages_dir,
        INBOX_DIR=temp_messages_dir / "inbox",
        OUTBOX_DIR=temp_messages_dir / "outbox",
        PROCESSED_DIR=temp_messages_dir / "processed",
        PROCESSING_DIR=temp_messages_dir / "processing",
        FAILED_DIR=temp_messages_dir / "failed",
        CONFIG_DIR=temp_messages_dir / "config",
        AUDIO_DIR=temp_messages_dir / "audio",
        TASKS_FILE=temp_messages_dir / "tasks.json",
        TASK_OUTPUTS_DIR=temp_messages_dir / "task-outputs",
        SCHEDULED_TASKS_DIR=temp_scheduled_tasks_dir,
        SCHEDULED_JOBS_FILE=temp_scheduled_tasks_dir / "jobs.json",
        SCHEDULED_TASKS_TASKS_DIR=temp_scheduled_tasks_dir / "tasks",
        SCHEDULED_TASKS_LOGS_DIR=temp_scheduled_tasks_dir / "logs",
    ):
        # Initialize required files
        (temp_messages_dir / "tasks.json").write_text(
            json.dumps({"tasks": [], "next_id": 1})
        )
        yield {
            "base": temp_messages_dir,
            "inbox": temp_messages_dir / "inbox",
            "outbox": temp_messages_dir / "outbox",
            "processed": temp_messages_dir / "processed",
            "processing": temp_messages_dir / "processing",
            "failed": temp_messages_dir / "failed",
            "audio": temp_messages_dir / "audio",
            "tasks_file": temp_messages_dir / "tasks.json",
            "task_outputs": temp_messages_dir / "task-outputs",
            "scheduled_tasks": temp_scheduled_tasks_dir,
            "jobs_file": temp_scheduled_tasks_dir / "jobs.json",
        }


# =============================================================================
# Async Fixtures
# =============================================================================


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# =============================================================================
# Environment Fixtures
# =============================================================================


@pytest.fixture
def clean_env():
    """Provide a clean environment without Lobster-related vars."""
    original_env = os.environ.copy()
    # Remove any Lobster-related environment variables
    for key in list(os.environ.keys()):
        if key.startswith(("TELEGRAM_", "LOBSTER_", "OPENAI_")):
            del os.environ[key]
    yield
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def test_env():
    """Provide test environment variables."""
    original_env = os.environ.copy()
    os.environ.update(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "TELEGRAM_ALLOWED_USERS": "123456,789012",
        }
    )
    yield
    os.environ.clear()
    os.environ.update(original_env)


# =============================================================================
# Utility Fixtures
# =============================================================================


@pytest.fixture
def assert_file_created():
    """Helper to assert a file was created with expected content."""

    def _assert(path: Path, expected_keys: list[str] = None):
        assert path.exists(), f"File {path} was not created"
        if expected_keys:
            content = json.loads(path.read_text())
            for key in expected_keys:
                assert key in content, f"Key '{key}' not found in {path}"

    return _assert


@pytest.fixture
def wait_for_file():
    """Helper to wait for a file to be created."""

    async def _wait(path: Path, timeout: float = 5.0) -> bool:
        import time

        start = time.time()
        while time.time() - start < timeout:
            if path.exists():
                return True
            await asyncio.sleep(0.1)
        return False

    return _wait


# =============================================================================
# Markers
# =============================================================================


def pytest_configure(config):
    """Configure custom pytest markers."""
    config.addinivalue_line("markers", "slow: marks tests as slow")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "stress: marks tests as stress tests")
    config.addinivalue_line("markers", "docker: marks tests requiring Docker")
