"""
Tests for MCP Server Validation Functions

Tests schedule validation, job name validation, and other validators.
These tests target the systemd-backed scheduling API introduced in
feature/scheduling-api-clean (PR #1105). The old cron-specific helpers
(validate_cron_schedule, validate_job_name, cron_to_human) are gone;
equivalent functionality lives in systemd_jobs.py and is exposed via
the _sj_* aliases in inbox_server.
"""

import pytest
import sys
from pathlib import Path

# Ensure systemd_jobs is importable by name (tests that import it directly)
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))


class TestValidateSchedule:
    """Tests for systemd schedule validation via _sj_validate_schedule.

    Bug 3 fix: validate_schedule was a no-op stub that returned None for any
    non-empty string. It now delegates to normalize_schedule so that invalid
    expressions are caught before the unit file is written.
    """

    def test_valid_cron_schedules_pass(self):
        """Valid cron expressions are accepted (and will be converted)."""
        from systemd_jobs import validate_schedule

        valid_schedules = [
            "* * * * *",
            "0 0 * * *",
            "30 8 * * *",
            "0 9 * * 1",
            "*/5 * * * *",
            "0 */2 * * *",
            "0 0 1 * *",
            "0 0 * * 0",
        ]

        for schedule in valid_schedules:
            err = validate_schedule(schedule)
            assert err is None, f"Schedule '{schedule}' should be valid, got: {err}"

    def test_empty_schedule_is_rejected(self):
        """Empty string must be rejected."""
        from systemd_jobs import validate_schedule

        err = validate_schedule("")
        assert err is not None
        assert "empty" in err.lower()

    def test_invalid_systemd_expression_rejected(self):
        """Strings that are neither valid cron nor valid systemd OnCalendar
        expressions must be rejected — not silently accepted."""
        from systemd_jobs import validate_schedule

        invalid = [
            "not-a-schedule",
            "garbage string here",
            "99 99 99 99 99",   # out-of-range cron that systemd will reject
        ]

        for schedule in invalid:
            err = validate_schedule(schedule)
            assert err is not None, (
                f"validate_schedule('{schedule}') returned None — "
                "the stub was not replaced with real validation (Bug 3)"
            )

    def test_valid_systemd_calendar_expressions_pass(self):
        """Native systemd OnCalendar expressions pass validation."""
        from systemd_jobs import validate_schedule

        valid = ["daily", "hourly", "*-*-* 09:00:00", "*:0/30:00", "weekly"]

        for expr in valid:
            err = validate_schedule(expr)
            assert err is None, f"Expression '{expr}' should be valid, got: {err}"


class TestValidateJobName:
    """Tests for job name validation via systemd_jobs.validate_name.

    The old validate_job_name helper in inbox_server returned (bool, str).
    The new validate_name in systemd_jobs returns Optional[str] (None = valid).
    """

    def test_valid_job_names(self):
        """Valid names return None."""
        from systemd_jobs import validate_name

        valid_names = [
            "a",
            "test",
            "my-job",
            "daily-backup",
            "job-123",
            "a1b2c3",
        ]

        for name in valid_names:
            err = validate_name(name)
            assert err is None, f"Name '{name}' should be valid, got: {err}"

    def test_empty_name(self):
        """Empty name returns an error mentioning 'empty'."""
        from systemd_jobs import validate_name

        err = validate_name("")
        assert err is not None
        assert "empty" in err.lower()

    def test_uppercase_rejected(self):
        """Names with uppercase letters are rejected."""
        from systemd_jobs import validate_name

        for name in ("MyJob", "UPPERCASE", "camelCase"):
            err = validate_name(name)
            assert err is not None, f"Name '{name}' should be invalid"

    def test_starts_with_hyphen_rejected(self):
        from systemd_jobs import validate_name

        assert validate_name("-invalid") is not None

    def test_ends_with_hyphen_rejected(self):
        from systemd_jobs import validate_name

        assert validate_name("invalid-") is not None

    def test_special_characters_rejected(self):
        from systemd_jobs import validate_name

        for name in ("job_name", "job.name", "job/name", "job name", "job@name"):
            err = validate_name(name)
            assert err is not None, f"Name '{name}' should be invalid"

    def test_too_long_name_rejected(self):
        """Names over MAX_NAME_LEN are rejected."""
        from systemd_jobs import validate_name, MAX_NAME_LEN

        long_name = "a" * (MAX_NAME_LEN + 1)
        err = validate_name(long_name)
        assert err is not None


class TestCronToSystemdConversion:
    """Tests for cron-to-systemd calendar conversion.

    The old cron_to_human helper returned human-readable text (e.g. "every 5
    minutes"). The new backend converts cron expressions to systemd OnCalendar
    strings via convert_cron_to_systemd/normalize_schedule. These tests verify
    that the conversions produce the correct systemd format (Bug 1 coverage).
    """

    def test_every_minute(self):
        """'* * * * *' converts to '*-*-* *:*:00'."""
        from systemd_jobs import convert_cron_to_systemd

        result = convert_cron_to_systemd("* * * * *")
        assert result == "*-*-* *:*:00"

    def test_every_5_minutes(self):
        """'*/5 * * * *' converts to '*-*-* *:0/05:00'."""
        from systemd_jobs import convert_cron_to_systemd

        result = convert_cron_to_systemd("*/5 * * * *")
        assert result == "*-*-* *:0/05:00"

    def test_every_30_minutes(self):
        """'*/30 * * * *' converts to '*-*-* *:0/30:00'."""
        from systemd_jobs import convert_cron_to_systemd

        result = convert_cron_to_systemd("*/30 * * * *")
        assert result == "*-*-* *:0/30:00"

    def test_every_2_hours(self):
        """'0 */2 * * *' converts to '*-*-* 0/2:00:00'."""
        from systemd_jobs import convert_cron_to_systemd

        result = convert_cron_to_systemd("0 */2 * * *")
        assert result == "*-*-* 0/2:00:00"

    def test_daily_at_9am(self):
        """'0 9 * * *' converts to '*-*-* 09:00:00'."""
        from systemd_jobs import convert_cron_to_systemd

        result = convert_cron_to_systemd("0 9 * * *")
        assert result == "*-*-* 09:00:00"

    def test_daily_at_2_30pm(self):
        """'30 14 * * *' converts to a string containing '14:30'."""
        from systemd_jobs import convert_cron_to_systemd

        result = convert_cron_to_systemd("30 14 * * *")
        assert result is not None
        assert "14" in result and "30" in result

    def test_weekly_monday(self):
        """'0 9 * * 1' converts to 'Mon *-*-* 09:00:00'."""
        from systemd_jobs import convert_cron_to_systemd

        result = convert_cron_to_systemd("0 9 * * 1")
        assert result == "Mon *-*-* 09:00:00"

    def test_complex_expression_returns_none(self):
        """Expressions that can't be cleanly expressed in systemd format
        return None (caller must reject with a helpful error)."""
        from systemd_jobs import convert_cron_to_systemd

        # */15 9 * * * cannot be expressed in OnCalendar without losing
        # the hour constraint — should return None
        result = convert_cron_to_systemd("*/15 9 * * *")
        assert result is None, (
            f"Expected None for '*/15 9 * * *' (unsupported mixed step+hour), got {result!r}"
        )


class TestToolListing:
    """Tests for tool listing functionality."""

    def test_list_tools_returns_all_tools(self):
        """Test that all 19 tools are listed."""
        import asyncio
        from src.mcp.inbox_server import list_tools

        tools = asyncio.run(list_tools())

        # Verify we have all expected tools
        tool_names = {tool.name for tool in tools}

        expected_tools = {
            "wait_for_messages",
            "check_inbox",
            "send_reply",
            "mark_processed",
            "list_sources",
            "get_stats",
            "list_tasks",
            "create_task",
            "update_task",
            "get_task",
            "delete_task",
            "transcribe_audio",
            "create_scheduled_job",
            "list_scheduled_jobs",
            "get_scheduled_job",
            "update_scheduled_job",
            "delete_scheduled_job",
            "check_task_outputs",
            "write_task_output",
        }

        assert expected_tools <= tool_names, f"Missing tools: {expected_tools - tool_names}"

    def test_tools_have_descriptions(self):
        """Test that all tools have descriptions."""
        import asyncio
        from src.mcp.inbox_server import list_tools

        tools = asyncio.run(list_tools())

        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"
            assert len(tool.description) > 10, f"Tool {tool.name} has too short description"

    def test_tools_have_input_schemas(self):
        """Test that all tools have input schemas."""
        import asyncio
        from src.mcp.inbox_server import list_tools

        tools = asyncio.run(list_tools())

        for tool in tools:
            assert tool.inputSchema, f"Tool {tool.name} has no inputSchema"
            assert "type" in tool.inputSchema, f"Tool {tool.name} schema missing type"
