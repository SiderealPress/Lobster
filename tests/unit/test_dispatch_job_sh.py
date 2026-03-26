"""
Tests for scheduled-tasks/dispatch-job.sh

Verifies:
1. Disabled jobs exit 0 silently without writing any inbox message
2. Enabled jobs write a scheduled_reminder to the inbox with task_content embedded
3. Missing task file exits non-zero
4. No claude -p invocation under any path
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent.parent
RUN_JOB = REPO_DIR / "scheduled-tasks" / "dispatch-job.sh"


def _make_env(workspace: Path, config_dir: Path, messages_dir: Path) -> dict:
    """Build a minimal environment for dispatch-job.sh."""
    env = os.environ.copy()
    env["LOBSTER_WORKSPACE"] = str(workspace)
    env["LOBSTER_CONFIG_DIR"] = str(config_dir)
    env["LOBSTER_MESSAGES"] = str(messages_dir)
    env["LOBSTER_INSTALL_DIR"] = str(REPO_DIR)
    # Prevent cron PATH issues in CI
    env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin") + ":" + str(Path.home() / ".local" / "bin")
    return env


def _setup_workspace(tmp: Path, job_name: str, enabled: bool, has_task_file: bool = True) -> tuple[Path, Path, Path]:
    """Create a temporary workspace with jobs.json and optional task file."""
    workspace = tmp / "lobster-workspace"
    jobs_dir = workspace / "scheduled-jobs"
    tasks_dir = jobs_dir / "tasks"
    logs_dir = jobs_dir / "logs"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    messages_dir = tmp / "messages"
    inbox_dir = messages_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    config_dir = tmp / "lobster-config"
    config_dir.mkdir(exist_ok=True)

    jobs_json = {
        "jobs": {
            job_name: {
                "name": job_name,
                "schedule": "0 * * * *",
                "enabled": enabled,
                "last_run": None,
                "last_status": None,
            }
        }
    }
    (jobs_dir / "jobs.json").write_text(json.dumps(jobs_json, indent=2))

    if has_task_file:
        task_content = f"# {job_name}\n\nDo the thing and call write_task_output.\n"
        (tasks_dir / f"{job_name}.md").write_text(task_content)

    return workspace, messages_dir, config_dir


class TestRunJobShDisabledFlag:
    """Disabled jobs must exit 0 silently without touching the inbox."""

    def test_disabled_job_exits_zero(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "test-job", enabled=False
        )
        env = _make_env(workspace, config_dir, messages_dir)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "test-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_disabled_job_writes_no_inbox_message(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "test-job", enabled=False
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        subprocess.run(
            ["bash", str(RUN_JOB), "test-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        assert inbox_files == [], f"Expected no inbox messages for disabled job, got: {inbox_files}"

    def test_disabled_job_logs_skip_message(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "test-job", enabled=False
        )
        env = _make_env(workspace, config_dir, messages_dir)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "test-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        log_dir = workspace / "scheduled-jobs" / "logs"
        log_files = list(log_dir.glob("test-job-*.log"))
        assert len(log_files) == 1
        log_content = log_files[0].read_text()
        assert "disabled" in log_content.lower()


class TestRunJobShEnabledDispatch:
    """Enabled jobs must write a scheduled_reminder to the inbox."""

    def test_enabled_job_exits_zero(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_enabled_job_writes_inbox_message(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, f"Expected exactly 1 inbox message, got {len(inbox_files)}"

    def test_inbox_message_has_correct_type_and_reminder_type(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        msg = json.loads(inbox_files[0].read_text())

        assert msg["type"] == "scheduled_reminder"
        assert msg["reminder_type"] == "my-poller"
        assert msg["job_name"] == "my-poller"

    def test_inbox_message_embeds_task_content(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        msg = json.loads(inbox_files[0].read_text())

        assert "task_content" in msg
        assert "Do the thing" in msg["task_content"]

    def test_inbox_message_has_system_source(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        msg = json.loads(inbox_files[0].read_text())

        assert msg["source"] == "system"
        assert msg["chat_id"] == 0

    def test_jobs_json_last_run_updated(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        jobs_json_path = workspace / "scheduled-jobs" / "jobs.json"
        data = json.loads(jobs_json_path.read_text())
        assert data["jobs"]["my-poller"]["last_run"] is not None


class TestRunJobShMissingTaskFile:
    """Missing task file should produce a non-zero exit."""

    def test_missing_task_file_exits_nonzero(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", enabled=True, has_task_file=False
        )
        env = _make_env(workspace, config_dir, messages_dir)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0

    def test_missing_task_file_writes_no_inbox_message(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", enabled=True, has_task_file=False
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        assert inbox_files == []


class TestRunJobShNoClaude:
    """dispatch-job.sh must never invoke claude -p under any condition."""

    def test_script_does_not_exec_claude(self):
        """The script source must not exec 'claude' as a subprocess (e.g. claude -p ...)."""
        content = RUN_JOB.read_text()
        # Allow the word 'claude' in comments, but not as an executable command.
        # A call would look like: claude -p or  or `claude ...`
        import re
        # Match 'claude' as a command invocation (not preceded by # or / or inside quotes as a string reference)
        lines = content.splitlines()
        for line in lines:
            stripped = line.strip()
            # Skip comments
            if stripped.startswith('#'):
                continue
            # Check for claude being called as a program
            if re.search(r'(?<![/#"])claude\s+-', stripped):
                msg = "dispatch-job.sh invokes claude as a command on line: " + repr(stripped)
                pytest.fail(msg + " -- jobs must be dispatched via inbox reminders")

    def test_no_claude_invocation_on_enabled_job(self, tmp_path):
        """Running an enabled job must not spawn any claude process."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)

        # Intercept any 'claude' calls by shadowing it with a script that fails loudly
        fake_claude = tmp_path / "claude"
        fake_claude.write_text("#!/bin/bash\necho 'ERROR: claude was called from dispatch-job.sh' >&2\nexit 99\n")
        fake_claude.chmod(0o755)
        env["PATH"] = str(tmp_path) + ":" + env["PATH"]

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected success, got {result.returncode}. stderr: {result.stderr}"
        assert "ERROR: claude was called" not in result.stderr


class TestRunJobShMissingJobsJson:
    """When jobs.json is missing, the job should run (default to enabled)."""

    def test_no_jobs_json_runs_as_enabled(self, tmp_path):
        workspace = tmp_path / "lobster-workspace"
        jobs_dir = workspace / "scheduled-jobs"
        tasks_dir = jobs_dir / "tasks"
        logs_dir = jobs_dir / "logs"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "my-poller.md").write_text("# My Poller\n\nDo stuff.\n")

        messages_dir = tmp_path / "messages"
        inbox_dir = messages_dir / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        config_dir = tmp_path / "lobster-config"
        config_dir.mkdir(exist_ok=True)

        env = _make_env(workspace, config_dir, messages_dir)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1
