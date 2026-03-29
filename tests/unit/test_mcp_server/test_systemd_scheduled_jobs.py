"""
Tests for refactored scheduled job MCP tools (systemd-backed).

Covers: create_scheduled_job, delete_scheduled_job, list_scheduled_jobs,
get_scheduled_job, update_scheduled_job, get_job_scaffold,
and the private helper functions _validate_timer_name, _timer_unit_content,
_service_unit_content, _create_systemd_timer, _delete_systemd_timer.

No real systemctl calls are made — all subprocess interactions are mocked.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Synchronously drive an async coroutine for test purposes."""
    return asyncio.run(coro)


def _ok(stdout="", stderr=""):
    """Fake _run_systemctl success return value."""
    return (0, stdout, stderr)


def _fail(returncode=1, stderr="error"):
    """Fake _run_systemctl failure return value."""
    return (returncode, "", stderr)


# ---------------------------------------------------------------------------
# _validate_timer_name
# ---------------------------------------------------------------------------

class TestValidateTimerName:

    def test_valid_simple_name(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, _ = _validate_timer_name("my-poller")
        assert valid

    def test_valid_single_char(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, _ = _validate_timer_name("a")
        assert valid

    def test_empty_name_rejected(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, msg = _validate_timer_name("")
        assert not valid
        assert "empty" in msg.lower()

    def test_uppercase_rejected(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, _ = _validate_timer_name("BadName")
        assert not valid

    def test_leading_hyphen_rejected(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, _ = _validate_timer_name("-bad")
        assert not valid

    def test_trailing_hyphen_rejected(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, _ = _validate_timer_name("bad-")
        assert not valid

    def test_path_traversal_rejected(self):
        from src.mcp.inbox_server import _validate_timer_name
        for bad in ["../evil", "foo/bar", "foo bar", "foo.timer"]:
            valid, _ = _validate_timer_name(bad)
            assert not valid, f"Expected {bad!r} to be rejected"

    def test_too_long_rejected(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, _ = _validate_timer_name("a" * 51)
        assert not valid

    def test_exactly_50_chars_accepted(self):
        from src.mcp.inbox_server import _validate_timer_name
        valid, _ = _validate_timer_name("a" * 50)
        assert valid


# ---------------------------------------------------------------------------
# Unit content helpers (pure functions)
# ---------------------------------------------------------------------------

class TestUnitContentHelpers:

    def test_timer_unit_content_includes_oncalendar(self):
        from src.mcp.inbox_server import _timer_unit_content
        content = _timer_unit_content("my-job", "*-*-* *:00/5:00", "My job description")
        assert "OnCalendar=*-*-* *:00/5:00" in content
        assert "LOBSTER-MANAGED" in content
        assert "lobster-my-job.service" in content

    def test_service_unit_content_includes_execstart(self):
        from src.mcp.inbox_server import _service_unit_content
        content = _service_unit_content("my-job", "/home/lobster/scripts/script.py", "My job description")
        assert "ExecStart=/home/lobster/scripts/script.py" in content
        assert "LOBSTER-MANAGED" in content
        assert "Type=oneshot" in content

    def test_timer_and_service_contents_are_pure(self):
        """Same inputs produce same outputs — no side effects."""
        from src.mcp.inbox_server import _timer_unit_content, _service_unit_content
        t1 = _timer_unit_content("foo", "daily", "desc")
        t2 = _timer_unit_content("foo", "daily", "desc")
        s1 = _service_unit_content("foo", "/bin/true", "desc")
        s2 = _service_unit_content("foo", "/bin/true", "desc")
        assert t1 == t2
        assert s1 == s2


# ---------------------------------------------------------------------------
# create_scheduled_job
# ---------------------------------------------------------------------------

class TestCreateScheduledJob:

    def _make_args(self, **overrides):
        base = {
            "name": "test-poller",
            "schedule": "*-*-* *:00/5:00",
            "context": "/home/lobster/scripts/my-script.py",
        }
        base.update(overrides)
        return base

    def test_requires_name(self):
        from src.mcp.inbox_server import handle_create_scheduled_job
        result = run(handle_create_scheduled_job(
            {"schedule": "*-*-* *:00/5:00", "context": "/bin/true"}
        ))
        assert "Error" in result[0].text

    def test_requires_schedule(self):
        from src.mcp.inbox_server import handle_create_scheduled_job
        result = run(handle_create_scheduled_job(
            {"name": "test-poller", "context": "/bin/true"}
        ))
        assert "Error" in result[0].text

    def test_requires_context(self):
        from src.mcp.inbox_server import handle_create_scheduled_job
        result = run(handle_create_scheduled_job(
            {"name": "test-poller", "schedule": "*-*-* *:00/5:00"}
        ))
        assert "Error" in result[0].text

    def test_rejects_relative_path(self):
        from src.mcp.inbox_server import handle_create_scheduled_job
        result = run(handle_create_scheduled_job(
            self._make_args(context="scripts/my-script.py")
        ))
        assert "Error" in result[0].text
        assert "absolute" in result[0].text.lower()

    def test_accepts_uv_run_absolute(self):
        """'uv run /absolute/path' is accepted."""
        from src.mcp.inbox_server import handle_create_scheduled_job
        # A relative 'uv run scripts/...' must be rejected
        result = run(handle_create_scheduled_job(
            self._make_args(context="uv run scripts/my-script.py")
        ))
        assert "absolute" in result[0].text.lower()

    def test_idempotent_when_unit_files_match(self, tmp_path):
        """Returns 'already exists' without recreating when unit files match."""
        from src.mcp.inbox_server import (
            handle_create_scheduled_job,
            _timer_unit_content,
            _service_unit_content,
        )
        name = "test-poller"
        schedule = "*-*-* *:00/5:00"
        command = "/home/lobster/scripts/my-script.py"
        description = f"Lobster scheduled job: {name}"

        # Pre-populate unit files to match what create would produce
        (tmp_path / f"lobster-{name}.timer").write_text(
            _timer_unit_content(name, schedule, description)
        )
        (tmp_path / f"lobster-{name}.service").write_text(
            _service_unit_content(name, command, description)
        )

        with patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path):
            result = run(handle_create_scheduled_job({
                "name": name,
                "schedule": schedule,
                "context": command,
            }))

        assert "already exists" in result[0].text
        assert "no changes" in result[0].text.lower()

    def test_creates_unit_files_and_enables(self, tmp_path):
        """create_scheduled_job writes unit files, calls daemon-reload, enables timer."""
        from src.mcp.inbox_server import handle_create_scheduled_job

        written_files = {}
        tee_calls = []
        systemctl_calls = []

        class FakeTeeProc:
            def __init__(self, path):
                self._path = path
                self.returncode = 0

            async def communicate(self, input=None):
                if input is not None:
                    written_files[self._path] = input.decode()
                return b"", b""

            def kill(self):
                pass

        class FakeSystemctlProc:
            returncode = 0

            async def communicate(self, input=None):
                return b"", b""

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*args, **kwargs):
            if args[0] == "sudo" and args[1] == "tee":
                tee_calls.append(args[2])
                return FakeTeeProc(args[2])
            return FakeSystemctlProc()

        async def fake_run_systemctl(*args):
            systemctl_calls.append(list(args))
            return (0, "", "")

        with (
            patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path),
            patch("src.mcp.inbox_server._run_systemctl", side_effect=fake_run_systemctl),
            patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
        ):
            result = run(handle_create_scheduled_job({
                "name": "test-poller",
                "schedule": "*-*-* *:00/5:00",
                "context": "/home/lobster/scripts/my-script.py",
            }))

        assert "Error" not in result[0].text, result[0].text
        assert "test-poller" in result[0].text

        # daemon-reload and enable --now must have been called
        sc_flat = [" ".join(c) for c in systemctl_calls]
        assert any("daemon-reload" in c for c in sc_flat), f"daemon-reload not called: {sc_flat}"
        assert any(
            "enable" in c and "lobster-test-poller.timer" in c
            for c in sc_flat
        ), f"enable not called: {sc_flat}"


# ---------------------------------------------------------------------------
# delete_scheduled_job
# ---------------------------------------------------------------------------

class TestDeleteScheduledJob:

    def test_requires_name(self):
        from src.mcp.inbox_server import handle_delete_scheduled_job
        result = run(handle_delete_scheduled_job({}))
        assert "Error" in result[0].text

    def test_idempotent_when_not_found(self, tmp_path):
        """Returns a clean message (not an error) when timer does not exist."""
        from src.mcp.inbox_server import handle_delete_scheduled_job
        with patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path):
            result = run(handle_delete_scheduled_job({"name": "nonexistent"}))
        # Should say 'does not exist' — not raise or return "Error:"
        assert "does not exist" in result[0].text.lower()
        assert "Error" not in result[0].text

    def test_removes_unit_files(self, tmp_path):
        """delete_scheduled_job removes .timer and .service files, calls daemon-reload."""
        from src.mcp.inbox_server import handle_delete_scheduled_job

        (tmp_path / "lobster-my-job.timer").write_text("# LOBSTER-MANAGED\n")
        (tmp_path / "lobster-my-job.service").write_text("# LOBSTER-MANAGED\n")

        removed_files = []
        systemctl_calls = []

        class FakeRmProc:
            returncode = 0

            async def communicate(self, input=None):
                return b"", b""

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            if cmd[0] == "sudo" and cmd[1] == "rm":
                removed_files.append(cmd[2])
                Path(cmd[2]).unlink(missing_ok=True)
            return FakeRmProc()

        async def fake_run_systemctl(*args):
            systemctl_calls.append(list(args))
            return (0, "", "")

        with (
            patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path),
            patch("src.mcp.inbox_server._run_systemctl", side_effect=fake_run_systemctl),
            patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
        ):
            result = run(handle_delete_scheduled_job({"name": "my-job"}))

        assert "Error" not in result[0].text, result[0].text
        assert "my-job" in result[0].text
        assert not (tmp_path / "lobster-my-job.timer").exists()
        assert not (tmp_path / "lobster-my-job.service").exists()
        sc_flat = [" ".join(c) for c in systemctl_calls]
        assert any("daemon-reload" in c for c in sc_flat)


# ---------------------------------------------------------------------------
# list_scheduled_jobs
# ---------------------------------------------------------------------------

class TestListScheduledJobs:

    def test_no_timers(self):
        from src.mcp.inbox_server import handle_list_scheduled_jobs
        with patch("src.mcp.inbox_server._run_systemctl", new=AsyncMock(return_value=_ok())):
            result = run(handle_list_scheduled_jobs({}))
        assert "no lobster" in result[0].text.lower()

    def test_filters_to_lobster_timers(self):
        """Only lines containing 'lobster-' are returned."""
        from src.mcp.inbox_server import handle_list_scheduled_jobs

        systemd_output = (
            "Sun 2026-03-29 02:00:00 UTC 30min ago Sun 2026-03-29 01:00:00 UTC 1h systemd-tmpfiles-clean.timer\n"
            "Sun 2026-03-29 03:00:00 UTC 1h        Sun 2026-03-29 02:00:00 UTC 1h lobster-github-poller.timer lobster-github-poller.service\n"
        )

        with patch("src.mcp.inbox_server._run_systemctl", new=AsyncMock(return_value=_ok(stdout=systemd_output))):
            result = run(handle_list_scheduled_jobs({}))

        assert "github-poller" in result[0].text
        assert "systemd-tmpfiles" not in result[0].text

    def test_systemctl_error_returns_error(self):
        from src.mcp.inbox_server import handle_list_scheduled_jobs
        with patch("src.mcp.inbox_server._run_systemctl", new=AsyncMock(return_value=_fail())):
            result = run(handle_list_scheduled_jobs({}))
        assert "Error" in result[0].text


# ---------------------------------------------------------------------------
# get_scheduled_job
# ---------------------------------------------------------------------------

class TestGetScheduledJob:

    def test_requires_name(self):
        from src.mcp.inbox_server import handle_get_scheduled_job
        result = run(handle_get_scheduled_job({}))
        assert "Error" in result[0].text

    def test_not_found(self):
        from src.mcp.inbox_server import handle_get_scheduled_job
        with patch(
            "src.mcp.inbox_server._run_systemctl",
            new=AsyncMock(return_value=_fail(returncode=4, stderr="not found")),
        ):
            result = run(handle_get_scheduled_job({"name": "nonexistent"}))
        assert "Error" in result[0].text

    def test_returns_status_output(self):
        from src.mcp.inbox_server import handle_get_scheduled_job

        status_output = (
            "● lobster-test.timer - Lobster scheduled job: test\n"
            "   Active: active (waiting)"
        )

        class FakeJournalProc:
            returncode = 0

            async def communicate(self, input=None):
                return b"Mar 29 01:00:00 lobster systemd[1]: test.service: Succeeded.", b""

            def kill(self):
                pass

        with (
            patch(
                "src.mcp.inbox_server._run_systemctl",
                new=AsyncMock(return_value=_ok(stdout=status_output)),
            ),
            patch("asyncio.create_subprocess_exec", return_value=FakeJournalProc()),
        ):
            result = run(handle_get_scheduled_job({"name": "test"}))

        assert "lobster-test" in result[0].text
        assert "Active" in result[0].text


# ---------------------------------------------------------------------------
# update_scheduled_job
# ---------------------------------------------------------------------------

class TestUpdateScheduledJob:

    def test_requires_name(self):
        from src.mcp.inbox_server import handle_update_scheduled_job
        result = run(handle_update_scheduled_job({}))
        assert "Error" in result[0].text

    def test_not_found(self, tmp_path):
        from src.mcp.inbox_server import handle_update_scheduled_job
        with patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path):
            result = run(handle_update_scheduled_job({"name": "nonexistent", "schedule": "daily"}))
        assert "Error" in result[0].text
        assert "not found" in result[0].text.lower()

    def test_no_changes_message(self, tmp_path):
        """When no fields are provided, returns 'no changes' message."""
        from src.mcp.inbox_server import handle_update_scheduled_job, _timer_unit_content, _service_unit_content
        name = "my-job"
        (tmp_path / f"lobster-{name}.timer").write_text(
            _timer_unit_content(name, "daily", "desc")
        )
        (tmp_path / f"lobster-{name}.service").write_text(
            _service_unit_content(name, "/bin/true", "desc")
        )
        with patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path):
            result = run(handle_update_scheduled_job({"name": name}))
        assert "no changes" in result[0].text.lower()

    def test_enable_calls_systemctl(self, tmp_path):
        """Setting enabled=True calls 'systemctl enable --now'."""
        from src.mcp.inbox_server import handle_update_scheduled_job, _timer_unit_content, _service_unit_content
        name = "my-job"
        (tmp_path / f"lobster-{name}.timer").write_text(
            _timer_unit_content(name, "daily", "desc")
        )
        (tmp_path / f"lobster-{name}.service").write_text(
            _service_unit_content(name, "/bin/true", "desc")
        )
        systemctl_calls = []

        async def fake_run_systemctl(*args):
            systemctl_calls.append(list(args))
            return (0, "", "")

        with (
            patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path),
            patch("src.mcp.inbox_server._run_systemctl", side_effect=fake_run_systemctl),
        ):
            result = run(handle_update_scheduled_job({"name": name, "enabled": True}))

        assert "Error" not in result[0].text
        sc_flat = [" ".join(c) for c in systemctl_calls]
        assert any("enable" in c and "--now" in c for c in sc_flat), sc_flat

    def test_disable_calls_systemctl(self, tmp_path):
        """Setting enabled=False calls 'systemctl disable --now'."""
        from src.mcp.inbox_server import handle_update_scheduled_job, _timer_unit_content, _service_unit_content
        name = "my-job"
        (tmp_path / f"lobster-{name}.timer").write_text(
            _timer_unit_content(name, "daily", "desc")
        )
        (tmp_path / f"lobster-{name}.service").write_text(
            _service_unit_content(name, "/bin/true", "desc")
        )
        systemctl_calls = []

        async def fake_run_systemctl(*args):
            systemctl_calls.append(list(args))
            return (0, "", "")

        with (
            patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path),
            patch("src.mcp.inbox_server._run_systemctl", side_effect=fake_run_systemctl),
        ):
            result = run(handle_update_scheduled_job({"name": name, "enabled": False}))

        assert "Error" not in result[0].text
        sc_flat = [" ".join(c) for c in systemctl_calls]
        assert any("disable" in c and "--now" in c for c in sc_flat), sc_flat


# ---------------------------------------------------------------------------
# get_job_scaffold
# ---------------------------------------------------------------------------

class TestGetJobScaffold:

    def test_returns_template_content(self, tmp_path):
        from src.mcp.inbox_server import handle_get_job_scaffold

        template_dir = tmp_path / "scheduled-tasks" / "templates"
        template_dir.mkdir(parents=True)
        template_file = template_dir / "poller.py.template"
        template_file.write_text("#!/usr/bin/env python3\n# REPLACE_WITH_YOUR_JOB_NAME\n")

        with patch("src.mcp.inbox_server._REPO_DIR", tmp_path):
            result = run(handle_get_job_scaffold({}))

        assert "REPLACE_WITH_YOUR_JOB_NAME" in result[0].text
        assert "create_scheduled_job" in result[0].text

    def test_unknown_kind_returns_error(self):
        from src.mcp.inbox_server import handle_get_job_scaffold
        result = run(handle_get_job_scaffold({"kind": "unknown"}))
        assert "Error" in result[0].text

    def test_missing_template_returns_error(self, tmp_path):
        from src.mcp.inbox_server import handle_get_job_scaffold
        with patch("src.mcp.inbox_server._REPO_DIR", tmp_path):
            result = run(handle_get_job_scaffold({"kind": "poller"}))
        assert "Error" in result[0].text
        assert "not found" in result[0].text.lower()


# ---------------------------------------------------------------------------
# Private helper: _create_systemd_timer integration
# ---------------------------------------------------------------------------

class TestCreateSystemdTimer:

    def test_returns_already_exists_when_unit_files_match(self, tmp_path):
        from src.mcp.inbox_server import (
            _create_systemd_timer,
            _timer_unit_content,
            _service_unit_content,
        )
        name = "my-job"
        schedule = "daily"
        command = "/bin/true"
        description = f"Lobster scheduled job: {name}"

        (tmp_path / f"lobster-{name}.timer").write_text(
            _timer_unit_content(name, schedule, description)
        )
        (tmp_path / f"lobster-{name}.service").write_text(
            _service_unit_content(name, command, description)
        )

        with patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path):
            success, result = run(_create_systemd_timer(name, schedule, command, description))

        assert success
        assert result == "already_exists"

    def test_calls_daemon_reload_and_enable(self, tmp_path):
        from src.mcp.inbox_server import _create_systemd_timer

        tee_calls = []
        systemctl_calls = []

        class FakeTeeProc:
            def __init__(self, path):
                self._path = path
                self.returncode = 0

            async def communicate(self, input=None):
                if input:
                    tee_calls.append(self._path)
                return b"", b""

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeTeeProc(args[2] if len(args) > 2 else "")

        async def fake_run_systemctl(*args):
            systemctl_calls.append(list(args))
            return (0, "", "")

        with (
            patch("src.mcp.inbox_server._SYSTEMD_UNIT_DIR", tmp_path),
            patch("src.mcp.inbox_server._run_systemctl", side_effect=fake_run_systemctl),
            patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
        ):
            success, result = run(
                _create_systemd_timer("my-job", "daily", "/bin/true", "My job")
            )

        assert success
        assert result == "created"
        sc_flat = [" ".join(c) for c in systemctl_calls]
        assert any("daemon-reload" in c for c in sc_flat)
        assert any("enable" in c for c in sc_flat)


# ---------------------------------------------------------------------------
# lobster_script_utils
# ---------------------------------------------------------------------------

class TestLobsterScriptUtils:

    def _load_utils(self):
        """Load lobster_script_utils from the worktree path."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "lobster_script_utils",
            "/home/lobster/lobster-workspace/projects/feature-scheduling-api-refactor"
            "/scheduled-tasks/lobster_script_utils.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_write_to_inbox_rejects_empty_chat_id(self):
        import pytest
        mod = self._load_utils()
        with pytest.raises(ValueError, match="chat_id"):
            mod.write_to_inbox("hello", "", "test-job")
        with pytest.raises(ValueError, match="chat_id"):
            mod.write_to_inbox("hello", 0, "test-job")

    def test_write_to_inbox_creates_json_file(self, tmp_path):
        import json
        mod = self._load_utils()
        mod.INBOX_DIR = tmp_path / "inbox"
        mod.LOGS_DIR = tmp_path / "logs"

        path = mod.write_to_inbox("test message", 12345, "test-job")

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["text"] == "test message"
        assert data["chat_id"] == 12345
        assert data["job_name"] == "test-job"
        assert data["type"] == "scheduled_job_output"

    def test_log_result_creates_jsonl_file(self, tmp_path):
        import json
        mod = self._load_utils()
        mod.INBOX_DIR = tmp_path / "inbox"
        mod.LOGS_DIR = tmp_path / "logs"
        mod.LOGS_DIR.mkdir(parents=True)

        path = mod.log_result("test-job", "success", "all done")

        assert path.exists()
        record = json.loads(path.read_text().strip())
        assert record["job_name"] == "test-job"
        assert record["status"] == "success"
        assert record["text"] == "all done"

    def test_get_config_reads_from_env(self, monkeypatch):
        mod = self._load_utils()
        monkeypatch.setenv("MY_TEST_KEY_LOBSTER_XYZ", "hello-world")
        assert mod.get_config("MY_TEST_KEY_LOBSTER_XYZ") == "hello-world"

    def test_get_config_returns_default_when_missing(self, monkeypatch):
        mod = self._load_utils()
        monkeypatch.delenv("NONEXISTENT_KEY_LOBSTER_999", raising=False)
        assert mod.get_config("NONEXISTENT_KEY_LOBSTER_999", "fallback") == "fallback"
        assert mod.get_config("NONEXISTENT_KEY_LOBSTER_999") == ""
