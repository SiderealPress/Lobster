"""
Tests for scheduled-tasks/dispatch-job.sh

Verifies:
1. Disabled jobs (systemctl is-enabled returns non-zero) exit 0 without inbox message
2. Enabled jobs (systemctl is-enabled returns 0) write a scheduled_reminder to inbox
3. Missing task file exits non-zero
4. No claude -p invocation under any path
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent.parent
RUN_JOB = REPO_DIR / "scheduled-tasks" / "dispatch-job.sh"


def _make_env(workspace: Path, config_dir: Path, messages_dir: Path, fake_bin: Path | None = None) -> dict:
    """Build a minimal environment for dispatch-job.sh."""
    env = os.environ.copy()
    env["LOBSTER_WORKSPACE"] = str(workspace)
    env["LOBSTER_CONFIG_DIR"] = str(config_dir)
    env["LOBSTER_MESSAGES"] = str(messages_dir)
    env["LOBSTER_INSTALL_DIR"] = str(REPO_DIR)
    base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin") + ":" + str(Path.home() / ".local" / "bin")
    if fake_bin:
        env["PATH"] = str(fake_bin) + ":" + base_path
    else:
        env["PATH"] = base_path
    return env


def _setup_workspace(tmp: Path, job_name: str, has_task_file: bool = True) -> tuple[Path, Path, Path]:
    """Create a temporary workspace with optional task file (no jobs.json)."""
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

    if has_task_file:
        task_content = f"# {job_name}\n\nDo the thing and call write_task_output.\n"
        (tasks_dir / f"{job_name}.md").write_text(task_content)

    return workspace, messages_dir, config_dir


def _make_fake_systemctl(bin_dir: Path, job_name: str, enabled: bool) -> None:
    """Create a fake systemctl script that simulates is-enabled for the given timer."""
    exit_code = 0 if enabled else 1
    script = f"""#!/bin/bash
# Fake systemctl for testing dispatch-job.sh
if [ "$1" = "is-enabled" ] && [ "$3" = "lobster-{job_name}.timer" ]; then
    exit {exit_code}
fi
# Default: call real systemctl for anything else
exec /usr/bin/systemctl "$@"
"""
    fake = bin_dir / "systemctl"
    fake.write_text(script)
    fake.chmod(0o755)


class TestRunJobShDisabledFlag:
    """Disabled jobs must exit 0 silently without touching the inbox.

    dispatch-job.sh now checks systemctl is-enabled instead of jobs.json.
    A fake systemctl is injected into PATH to control the enabled state.
    """

    def test_disabled_job_exits_zero(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "test-job")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "test-job", enabled=False)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "test-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_disabled_job_writes_no_inbox_message(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "test-job")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "test-job", enabled=False)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)
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
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "test-job")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "test-job", enabled=False)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)

        subprocess.run(
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
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "my-poller")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "my-poller", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_enabled_job_writes_inbox_message(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "my-poller")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "my-poller", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)
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
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "my-poller")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "my-poller", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)
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
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "my-poller")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "my-poller", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)
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

    def test_inbox_message_has_system_source_and_zero_chat_id(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "my-poller")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "my-poller", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)
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
        # chat_id is always 0 — dispatcher knows the configured user, no lookup needed
        assert msg["chat_id"] == 0

    def test_enabled_job_does_not_modify_any_json_registry(self, tmp_path):
        """dispatch-job.sh must not write to any jobs.json file."""
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "my-poller")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "my-poller", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        # No jobs.json should have been created
        jobs_json = workspace / "scheduled-jobs" / "jobs.json"
        assert not jobs_json.exists(), "dispatch-job.sh must not create jobs.json"


class TestRunJobShMissingTaskFile:
    """Missing task file: job must be auto-disabled and exit 0 (not spam cron errors)."""

    def test_missing_task_file_exits_zero(self, tmp_path):
        """After fix #1200: missing task file must exit 0 so cron stops marking failures."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", has_task_file=False
        )
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "no-task-job", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"

    def test_missing_task_file_writes_no_inbox_message(self, tmp_path):
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", has_task_file=False
        )
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "no-task-job", enabled=True)
        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)
        inbox_dir = messages_dir / "inbox"

        subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        assert inbox_files == []

    def test_missing_task_file_auto_disables_job_in_jobs_json(self, tmp_path):
        """After fix #1200: job must be set to enabled=false in jobs.json when task file is missing."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", enabled=True, has_task_file=False
        )
        env = _make_env(workspace, config_dir, messages_dir)

        subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        jobs_json_path = workspace / "scheduled-jobs" / "jobs.json"
        data = json.loads(jobs_json_path.read_text())
        assert data["jobs"]["no-task-job"]["enabled"] is False, (
            "Job must be auto-disabled when task file is missing"
        )

    def test_missing_task_file_logs_auto_disable(self, tmp_path):
        """Log must mention auto-disable so operator knows what happened."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", enabled=True, has_task_file=False
        )
        env = _make_env(workspace, config_dir, messages_dir)

        subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        log_dir = workspace / "scheduled-jobs" / "logs"
        log_files = list(log_dir.glob("no-task-job-*.log"))
        assert len(log_files) == 1
        log_content = log_files[0].read_text()
        assert "auto-disab" in log_content.lower(), (
            f"Expected 'auto-disab' in log, got: {log_content!r}"
        )

    def test_auto_disabled_job_does_not_dispatch_on_second_run(self, tmp_path):
        """Once auto-disabled, a second cron fire must skip silently with no inbox message."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", enabled=True, has_task_file=False
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        # First run: detects missing task file, auto-disables
        subprocess.run(["bash", str(RUN_JOB), "no-task-job"], env=env, capture_output=True, text=True)

        # Second run: job is now disabled, must skip silently
        result = subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        inbox_files = list(inbox_dir.glob("*.json"))
        assert inbox_files == [], f"Expected no inbox messages after auto-disable, got: {inbox_files}"


class TestRunJobShDedupGuard:
    """Dedup guard: job must be skipped if a pending dispatch already exists in inbox (#1201)."""

    def test_skips_if_pending_dispatch_in_inbox(self, tmp_path):
        """When a matching *_scheduled_<job>.json file already exists in inbox, skip dispatch."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        # Simulate a pending dispatch already in inbox
        pending = inbox_dir / "1700000000000_scheduled_my-poller.json"
        pending.write_text('{"id": "1700000000000_scheduled_my-poller", "type": "scheduled_reminder"}\n')

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"
        # The existing pending file should be unchanged, and no new file added
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, f"Expected only the pre-existing file, got {len(inbox_files)}"

    def test_dedup_skips_logs_message(self, tmp_path):
        """When dedup guard fires, a log entry must be written."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        pending = inbox_dir / "1700000000000_scheduled_my-poller.json"
        pending.write_text('{"id": "1700000000000_scheduled_my-poller", "type": "scheduled_reminder"}\n')

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        log_dir = workspace / "scheduled-jobs" / "logs"
        log_files = list(log_dir.glob("my-poller-*.log"))
        assert len(log_files) == 1
        log_content = log_files[0].read_text()
        assert "pending" in log_content.lower() or "skipping" in log_content.lower(), (
            f"Expected dedup message in log, got: {log_content!r}"
        )

    def test_dispatches_when_no_pending_in_inbox(self, tmp_path):
        """Without a pending dispatch, job must dispatch normally."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, f"Expected 1 dispatched message, got {len(inbox_files)}"

    def test_dedup_does_not_match_different_job(self, tmp_path):
        """Pending dispatch for a different job must not block the current job."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        # Pending dispatch for a DIFFERENT job
        other_pending = inbox_dir / "1700000000000_scheduled_other-job.json"
        other_pending.write_text('{"id": "1700000000000_scheduled_other-job", "type": "scheduled_reminder"}\n')

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # my-poller should have dispatched, so total is 2 (other-job + my-poller)
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 2, f"Expected 2 inbox files (other + dispatched), got {len(inbox_files)}"


        subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        jobs_json_path = workspace / "scheduled-jobs" / "jobs.json"
        data = json.loads(jobs_json_path.read_text())
        assert data["jobs"]["no-task-job"]["enabled"] is False, (
            "Job must be auto-disabled when task file is missing"
        )

    def test_missing_task_file_logs_auto_disable(self, tmp_path):
        """Log must mention auto-disable so operator knows what happened."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", enabled=True, has_task_file=False
        )
        env = _make_env(workspace, config_dir, messages_dir)

        subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        log_dir = workspace / "scheduled-jobs" / "logs"
        log_files = list(log_dir.glob("no-task-job-*.log"))
        assert len(log_files) == 1
        log_content = log_files[0].read_text()
        assert "auto-disab" in log_content.lower(), (
            f"Expected 'auto-disab' in log, got: {log_content!r}"
        )

    def test_auto_disabled_job_does_not_dispatch_on_second_run(self, tmp_path):
        """Once auto-disabled, a second cron fire must skip silently with no inbox message."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "no-task-job", enabled=True, has_task_file=False
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        # First run: detects missing task file, auto-disables
        subprocess.run(["bash", str(RUN_JOB), "no-task-job"], env=env, capture_output=True, text=True)

        # Second run: job is now disabled, must skip silently
        result = subprocess.run(
            ["bash", str(RUN_JOB), "no-task-job"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        inbox_files = list(inbox_dir.glob("*.json"))
        assert inbox_files == [], f"Expected no inbox messages after auto-disable, got: {inbox_files}"


class TestRunJobShDedupGuard:
    """Dedup guard: job must be skipped if a pending dispatch already exists in inbox (#1201)."""

    def test_skips_if_pending_dispatch_in_inbox(self, tmp_path):
        """When a matching *_scheduled_<job>.json file already exists in inbox, skip dispatch."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        # Simulate a pending dispatch already in inbox
        pending = inbox_dir / "1700000000000_scheduled_my-poller.json"
        pending.write_text('{"id": "1700000000000_scheduled_my-poller", "type": "scheduled_reminder"}\n')

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"
        # The existing pending file should be unchanged, and no new file added
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, f"Expected only the pre-existing file, got {len(inbox_files)}"

    def test_dedup_skips_logs_message(self, tmp_path):
        """When dedup guard fires, a log entry must be written."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        pending = inbox_dir / "1700000000000_scheduled_my-poller.json"
        pending.write_text('{"id": "1700000000000_scheduled_my-poller", "type": "scheduled_reminder"}\n')

        subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        log_dir = workspace / "scheduled-jobs" / "logs"
        log_files = list(log_dir.glob("my-poller-*.log"))
        assert len(log_files) == 1
        log_content = log_files[0].read_text()
        assert "pending" in log_content.lower() or "skipping" in log_content.lower(), (
            f"Expected dedup message in log, got: {log_content!r}"
        )

    def test_dispatches_when_no_pending_in_inbox(self, tmp_path):
        """Without a pending dispatch, job must dispatch normally."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, f"Expected 1 dispatched message, got {len(inbox_files)}"

    def test_dedup_does_not_match_different_job(self, tmp_path):
        """Pending dispatch for a different job must not block the current job."""
        workspace, messages_dir, config_dir = _setup_workspace(
            tmp_path, "my-poller", enabled=True
        )
        env = _make_env(workspace, config_dir, messages_dir)
        inbox_dir = messages_dir / "inbox"

        # Pending dispatch for a DIFFERENT job
        other_pending = inbox_dir / "1700000000000_scheduled_other-job.json"
        other_pending.write_text('{"id": "1700000000000_scheduled_other-job", "type": "scheduled_reminder"}\n')

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # my-poller should have dispatched, so total is 2 (other-job + my-poller)
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 2, f"Expected 2 inbox files (other + dispatched), got {len(inbox_files)}"


class TestRunJobShNoClaude:
    """dispatch-job.sh must never invoke claude -p under any condition."""

    def test_script_does_not_exec_claude(self):
        """The script source must not exec 'claude' as a subprocess (e.g. claude -p ...)."""
        content = RUN_JOB.read_text()
        import re
        # Allow 'claude' in comments, but not as an executable command.
        lines = content.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if re.search(r'(?<![/#"])claude\s+-', stripped):
                msg = "dispatch-job.sh invokes claude as a command on line: " + repr(stripped)
                pytest.fail(msg + " -- jobs must be dispatched via inbox reminders")

    def test_no_claude_invocation_on_enabled_job(self, tmp_path):
        """Running an enabled job must not spawn any claude process."""
        workspace, messages_dir, config_dir = _setup_workspace(tmp_path, "my-poller")
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        _make_fake_systemctl(fake_bin, "my-poller", enabled=True)

        # Intercept any 'claude' calls by shadowing it with a script that fails loudly
        fake_claude = fake_bin / "claude"
        fake_claude.write_text("#!/bin/bash\necho 'ERROR: claude was called from dispatch-job.sh' >&2\nexit 99\n")
        fake_claude.chmod(0o755)

        env = _make_env(workspace, config_dir, messages_dir, fake_bin=fake_bin)

        result = subprocess.run(
            ["bash", str(RUN_JOB), "my-poller"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected success, got {result.returncode}. stderr: {result.stderr}"
        assert "ERROR: claude was called" not in result.stderr
