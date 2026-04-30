"""
Unit tests for reminder_manager.py

Tests are derived from issue #1788 spec:
- create_reminder validates reminder_type, fire_time_utc
- create_reminder generates unique IDs and delegates to systemd_jobs.create_job
- create_reminder writes to the registry
- list_reminders returns pending-only by default (future fire_time, not cancelled)
- cancel_reminder stops the timer and marks the registry entry cancelled
- cancel_reminder returns cancelled=False for unknown or already-cancelled reminders

All systemd I/O is mocked — no actual systemctl calls are made.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys

_MCP_DIR = Path(__file__).parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

# Patch LOBSTER_HOME before importing reminder_manager so that REMINDERS_DIR
# points to a writable tmp path during all tests.
import reminder_manager as rm


# ---------------------------------------------------------------------------
# Time constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(hours=2)
PAST = NOW - timedelta(hours=2)
FUTURE_ISO = FUTURE.strftime("%Y-%m-%dT%H:%M:%SZ")
PAST_ISO = PAST.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure function tests — no I/O
# ---------------------------------------------------------------------------


class TestValidateReminderType:
    def test_valid_type_passes(self):
        assert rm._validate_reminder_type("interview-prep") is None

    def test_valid_type_with_underscores(self):
        assert rm._validate_reminder_type("daily_standup") is None

    def test_empty_string_fails(self):
        assert rm._validate_reminder_type("") is not None

    def test_too_long_fails(self):
        long_type = "a" * (rm.MAX_REMINDER_TYPE_LEN + 1)
        assert rm._validate_reminder_type(long_type) is not None

    def test_exactly_max_length_passes(self):
        max_type = "a" * rm.MAX_REMINDER_TYPE_LEN
        assert rm._validate_reminder_type(max_type) is None

    def test_spaces_fail(self):
        assert rm._validate_reminder_type("has space") is not None

    def test_special_chars_fail(self):
        assert rm._validate_reminder_type("type@domain") is not None


class TestValidateFireTime:
    def test_valid_future_time_passes(self):
        future = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Patch datetime.now to return NOW so "future" is actually future
        with patch("reminder_manager.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            # Use the real _validate_fire_time but override now check
            err = rm._validate_fire_time.__wrapped__(future) if hasattr(rm._validate_fire_time, '__wrapped__') else None

        # Simpler: just call with a real future date
        far_future = "2099-01-01T00:00:00Z"
        assert rm._validate_fire_time(far_future) is None

    def test_past_time_fails(self):
        past = "2020-01-01T00:00:00Z"
        err = rm._validate_fire_time(past)
        assert err is not None
        assert "future" in err

    def test_empty_string_fails(self):
        assert rm._validate_fire_time("") is not None

    def test_invalid_format_fails(self):
        assert rm._validate_fire_time("not-a-date") is not None

    def test_z_suffix_accepted(self):
        # 2099 is definitely in the future
        assert rm._validate_fire_time("2099-06-01T09:00:00Z") is None

    def test_plus00_suffix_accepted(self):
        assert rm._validate_fire_time("2099-06-01T09:00:00+00:00") is None

    def test_naive_datetime_fails(self):
        # No timezone info — should fail
        err = rm._validate_fire_time("2099-06-01T09:00:00")
        assert err is not None


class TestParseFireTime:
    def test_z_suffix_parsed_as_utc(self):
        dt = rm._parse_fire_time("2026-05-01T09:00:00Z")
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2026

    def test_plus00_suffix_parsed(self):
        dt = rm._parse_fire_time("2026-05-01T09:00:00+00:00")
        assert dt.tzinfo == timezone.utc

    def test_invalid_raises_valueerror(self):
        with pytest.raises(ValueError):
            rm._parse_fire_time("not-a-date")

    def test_naive_raises_valueerror(self):
        with pytest.raises(ValueError):
            rm._parse_fire_time("2026-05-01T09:00:00")


class TestMakeReminderId:
    def test_includes_reminder_type(self):
        reminder_id = rm._make_reminder_id("interview-prep", NOW)
        assert "interview-prep" in reminder_id

    def test_includes_epoch_ms(self):
        reminder_id = rm._make_reminder_id("test", NOW)
        epoch_ms = str(int(NOW.timestamp() * 1000))
        assert epoch_ms in reminder_id

    def test_different_times_produce_different_ids(self):
        id1 = rm._make_reminder_id("test", NOW)
        id2 = rm._make_reminder_id("test", NOW + timedelta(seconds=1))
        assert id1 != id2

    def test_different_types_produce_different_ids(self):
        id1 = rm._make_reminder_id("type-a", NOW)
        id2 = rm._make_reminder_id("type-b", NOW)
        assert id1 != id2


class TestMakeJobName:
    def test_starts_with_reminder_prefix(self):
        name = rm._make_job_name("interview-prep-1234567890123")
        assert name.startswith(rm.REMINDER_NAME_PREFIX)

    def test_within_max_name_len(self):
        # MAX_NAME_LEN in systemd_jobs is 50
        MAX_NAME_LEN = 50
        name = rm._make_job_name("a" * 40)
        assert len(name) <= MAX_NAME_LEN

    def test_deterministic(self):
        """Same reminder_id always produces the same job name."""
        name1 = rm._make_job_name("some-reminder-id-123")
        name2 = rm._make_job_name("some-reminder-id-123")
        assert name1 == name2

    def test_different_ids_produce_different_names(self):
        name1 = rm._make_job_name("id-one")
        name2 = rm._make_job_name("id-two")
        assert name1 != name2


class TestFormatSystemdCalendar:
    def test_formats_as_systemd_calendar(self):
        dt = datetime(2026, 5, 1, 9, 30, 0, tzinfo=timezone.utc)
        result = rm._format_systemd_calendar(dt)
        assert result == "2026-05-01 09:30:00 UTC"

    def test_zero_padded_month_and_day(self):
        dt = datetime(2026, 1, 5, 8, 5, 0, tzinfo=timezone.utc)
        result = rm._format_systemd_calendar(dt)
        assert "01-05" in result
        assert "08:05:00" in result


class TestBuildCommand:
    def test_includes_reminder_type(self):
        cmd = rm._build_command("interview-prep", "interview-prep-1000")
        assert "interview-prep" in cmd
        assert "post-reminder.sh" in cmd

    def test_includes_reminder_id(self):
        cmd = rm._build_command("test", "test-1234567890")
        assert "test-1234567890" in cmd

    def test_absolute_path(self):
        cmd = rm._build_command("test", "test-id")
        assert cmd.startswith("/")


# ---------------------------------------------------------------------------
# Registry I/O tests — use tmp_path to avoid touching real filesystem
# ---------------------------------------------------------------------------


class TestRegistry:
    def _patch_registry(self, tmp_path: Path):
        """Redirect REGISTRY_FILE and REMINDERS_DIR to tmp_path."""
        registry_file = tmp_path / "reminders.json"
        return (
            patch.object(rm, "REGISTRY_FILE", registry_file),
            patch.object(rm, "REMINDERS_DIR", tmp_path),
        )

    def test_load_registry_empty_when_file_missing(self, tmp_path: Path):
        p1, p2 = self._patch_registry(tmp_path)
        with p1, p2:
            entries = rm._load_registry()
        assert entries == []

    def test_save_and_load_round_trip(self, tmp_path: Path):
        p1, p2 = self._patch_registry(tmp_path)
        entry = {"reminder_id": "test-123", "reminder_type": "test", "cancelled": False}
        with p1, p2:
            rm._save_registry([entry])
            loaded = rm._load_registry()
        assert len(loaded) == 1
        assert loaded[0]["reminder_id"] == "test-123"

    def test_registry_add_appends(self, tmp_path: Path):
        p1, p2 = self._patch_registry(tmp_path)
        e1 = {"reminder_id": "a", "reminder_type": "x", "cancelled": False}
        e2 = {"reminder_id": "b", "reminder_type": "y", "cancelled": False}
        with p1, p2:
            rm._registry_add(e1)
            rm._registry_add(e2)
            loaded = rm._load_registry()
        assert len(loaded) == 2

    def test_registry_mark_cancelled_returns_true_when_found(self, tmp_path: Path):
        p1, p2 = self._patch_registry(tmp_path)
        entry = {"reminder_id": "to-cancel", "reminder_type": "x", "cancelled": False}
        with p1, p2:
            rm._registry_add(entry)
            result = rm._registry_mark_cancelled("to-cancel")
            assert result is True
            loaded = rm._load_registry()
        assert loaded[0]["cancelled"] is True

    def test_registry_mark_cancelled_returns_false_when_not_found(self, tmp_path: Path):
        p1, p2 = self._patch_registry(tmp_path)
        with p1, p2:
            result = rm._registry_mark_cancelled("nonexistent")
        assert result is False

    def test_registry_get_returns_entry(self, tmp_path: Path):
        p1, p2 = self._patch_registry(tmp_path)
        entry = {"reminder_id": "xyz", "reminder_type": "test", "cancelled": False}
        with p1, p2:
            rm._registry_add(entry)
            found = rm._registry_get("xyz")
        assert found is not None
        assert found["reminder_id"] == "xyz"

    def test_registry_get_returns_none_for_missing(self, tmp_path: Path):
        p1, p2 = self._patch_registry(tmp_path)
        with p1, p2:
            result = rm._registry_get("does-not-exist")
        assert result is None

    def test_load_registry_returns_empty_on_malformed_json(self, tmp_path: Path):
        bad_file = tmp_path / "reminders.json"
        bad_file.write_text("not json {")
        p1, p2 = self._patch_registry(tmp_path)
        with p1, p2:
            entries = rm._load_registry()
        assert entries == []


# ---------------------------------------------------------------------------
# create_reminder — integration with registry and mocked systemd
# ---------------------------------------------------------------------------


class TestCreateReminder:
    def _call(self, reminder_type: str, fire_time_utc: str,
              metadata=None, tmp_path: Path = None,
              mock_create=None):
        """Helper: call create_reminder with mocked systemd and tmp registry."""
        if mock_create is None:
            from systemd_jobs import CreateResult
            mock_result = CreateResult(name="rem-testjob", status="created")
            mock_create = AsyncMock(return_value=mock_result)

        patches = [
            patch("reminder_manager.create_job", mock_create),
        ]
        if tmp_path is not None:
            from systemd_jobs import CreateResult
            registry_file = tmp_path / "reminders.json"
            patches += [
                patch.object(rm, "REGISTRY_FILE", registry_file),
                patch.object(rm, "REMINDERS_DIR", tmp_path),
            ]

        ctx = patches[0]
        for p in patches[1:]:
            ctx = ctx.__enter__() if hasattr(ctx, '__enter__') else ctx

        # Use patch as context managers individually
        with patch("reminder_manager.create_job", mock_create):
            if tmp_path:
                with patch.object(rm, "REGISTRY_FILE", tmp_path / "reminders.json"), \
                     patch.object(rm, "REMINDERS_DIR", tmp_path):
                    return _run(rm.create_reminder(
                        reminder_type, fire_time_utc, metadata=metadata, now_utc=NOW
                    ))
            else:
                return _run(rm.create_reminder(
                    reminder_type, fire_time_utc, metadata=metadata, now_utc=NOW
                ))

    def test_returns_create_reminder_result(self, tmp_path: Path):
        from systemd_jobs import CreateResult
        mock_create = AsyncMock(return_value=CreateResult("rem-abc", "created"))
        result = self._call("interview-prep", FUTURE_ISO, tmp_path=tmp_path,
                            mock_create=mock_create)
        assert result.reminder_id is not None
        assert "interview-prep" in result.reminder_id
        assert result.fire_time_utc is not None
        assert result.job_name is not None

    def test_calls_create_job_with_one_shot_schedule(self, tmp_path: Path):
        from systemd_jobs import CreateResult
        captured = {}

        async def fake_create(name, schedule, command, description=""):
            captured["schedule"] = schedule
            captured["command"] = command
            return CreateResult(name, "created")

        result = self._call("test-type", FUTURE_ISO, tmp_path=tmp_path,
                            mock_create=fake_create)
        # Schedule should include the future date's year and UTC
        assert "UTC" in captured["schedule"]
        assert "2026" in captured["schedule"]

    def test_calls_create_job_with_post_reminder_command(self, tmp_path: Path):
        from systemd_jobs import CreateResult
        captured = {}

        async def fake_create(name, schedule, command, description=""):
            captured["command"] = command
            return CreateResult(name, "created")

        self._call("my-reminder", FUTURE_ISO, tmp_path=tmp_path,
                   mock_create=fake_create)
        assert "post-reminder.sh" in captured["command"]
        assert "my-reminder" in captured["command"]
        # reminder_id is also passed so post-reminder.sh can include it in the inbox message
        assert "my-reminder-" in captured["command"]

    def test_reminder_written_to_registry(self, tmp_path: Path):
        from systemd_jobs import CreateResult
        mock_create = AsyncMock(return_value=CreateResult("rem-abc", "created"))
        self._call("reg-test", FUTURE_ISO, tmp_path=tmp_path, mock_create=mock_create)

        with patch.object(rm, "REGISTRY_FILE", tmp_path / "reminders.json"), \
             patch.object(rm, "REMINDERS_DIR", tmp_path):
            entries = rm._load_registry()

        assert len(entries) == 1
        assert entries[0]["reminder_type"] == "reg-test"
        assert entries[0]["cancelled"] is False

    def test_metadata_persisted_in_registry(self, tmp_path: Path):
        from systemd_jobs import CreateResult
        mock_create = AsyncMock(return_value=CreateResult("rem-abc", "created"))
        meta = {"context": "interview with ACME", "urgency": "high"}
        self._call("test", FUTURE_ISO, metadata=meta, tmp_path=tmp_path,
                   mock_create=mock_create)

        with patch.object(rm, "REGISTRY_FILE", tmp_path / "reminders.json"), \
             patch.object(rm, "REMINDERS_DIR", tmp_path):
            entries = rm._load_registry()

        assert entries[0]["metadata"] == meta

    def test_invalid_reminder_type_raises_valueerror(self, tmp_path: Path):
        with pytest.raises(ValueError, match="reminder_type"):
            _run(rm.create_reminder("", FUTURE_ISO, now_utc=NOW))

    def test_past_fire_time_raises_valueerror(self, tmp_path: Path):
        # Use a date that is definitively in the past (before this codebase existed)
        truly_past = "2020-01-01T00:00:00Z"
        with pytest.raises(ValueError, match="future"):
            _run(rm.create_reminder("test", truly_past, now_utc=NOW))

    def test_invalid_fire_time_format_raises_valueerror(self):
        with pytest.raises(ValueError):
            _run(rm.create_reminder("test", "not-a-date", now_utc=NOW))

    def test_systemd_failure_propagates(self, tmp_path: Path):
        """If create_job raises, the error propagates to the caller."""
        async def failing_create(name, schedule, command, description=""):
            raise RuntimeError("systemctl failed")

        with pytest.raises(RuntimeError, match="systemctl"):
            with patch("reminder_manager.create_job", failing_create), \
                 patch.object(rm, "REGISTRY_FILE", tmp_path / "reminders.json"), \
                 patch.object(rm, "REMINDERS_DIR", tmp_path):
                _run(rm.create_reminder("test", FUTURE_ISO, now_utc=NOW))


# ---------------------------------------------------------------------------
# list_reminders
# ---------------------------------------------------------------------------


class TestListReminders:
    def _with_registry(self, tmp_path: Path, entries: list):
        registry_file = tmp_path / "reminders.json"
        registry_file.write_text(json.dumps(entries))
        return (
            patch.object(rm, "REGISTRY_FILE", registry_file),
            patch.object(rm, "REMINDERS_DIR", tmp_path),
        )

    def test_returns_pending_future_reminders(self, tmp_path: Path):
        entries = [
            {
                "reminder_id": "interview-prep-1000",
                "reminder_type": "interview-prep",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
            }
        ]
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2:
            result = rm.list_reminders(pending_only=True, now_utc=NOW)
        assert len(result) == 1
        assert result[0].reminder_type == "interview-prep"

    def test_filters_out_already_fired_reminders(self, tmp_path: Path):
        entries = [
            {
                "reminder_id": "old-type-1000",
                "reminder_type": "old-type",
                "fire_time_utc": PAST_ISO,
                "metadata": {},
                "created_at": PAST_ISO,
                "cancelled": False,
            }
        ]
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2:
            result = rm.list_reminders(pending_only=True, now_utc=NOW)
        assert result == []

    def test_filters_out_cancelled_reminders(self, tmp_path: Path):
        entries = [
            {
                "reminder_id": "cancelled-type-1000",
                "reminder_type": "cancelled-type",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": True,
            }
        ]
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2:
            result = rm.list_reminders(pending_only=True, now_utc=NOW)
        assert result == []

    def test_pending_only_false_includes_all(self, tmp_path: Path):
        entries = [
            {
                "reminder_id": "r1",
                "reminder_type": "active",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
            },
            {
                "reminder_id": "r2",
                "reminder_type": "cancelled",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": True,
            },
        ]
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2:
            result = rm.list_reminders(pending_only=False, now_utc=NOW)
        assert len(result) == 2

    def test_returns_empty_when_no_registry(self, tmp_path: Path):
        nonexistent = tmp_path / "no-reminders.json"
        with patch.object(rm, "REGISTRY_FILE", nonexistent), \
             patch.object(rm, "REMINDERS_DIR", tmp_path):
            result = rm.list_reminders(pending_only=True, now_utc=NOW)
        assert result == []

    def test_sorted_by_fire_time_ascending(self, tmp_path: Path):
        later = (FUTURE + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [
            {
                "reminder_id": "r2",
                "reminder_type": "later",
                "fire_time_utc": later,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
            },
            {
                "reminder_id": "r1",
                "reminder_type": "earlier",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
            },
        ]
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2:
            result = rm.list_reminders(pending_only=True, now_utc=NOW)
        assert result[0].reminder_type == "earlier"
        assert result[1].reminder_type == "later"

    def test_result_fields_populated(self, tmp_path: Path):
        entries = [
            {
                "reminder_id": "test-id",
                "reminder_type": "test-type",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {"key": "value"},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
            }
        ]
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2:
            result = rm.list_reminders(pending_only=True, now_utc=NOW)
        r = result[0]
        assert r.reminder_id == "test-id"
        assert r.reminder_type == "test-type"
        assert r.fire_time_utc == FUTURE_ISO
        assert r.metadata == {"key": "value"}


# ---------------------------------------------------------------------------
# cancel_reminder
# ---------------------------------------------------------------------------


class TestCancelReminder:
    def _with_registry(self, tmp_path: Path, entries: list):
        registry_file = tmp_path / "reminders.json"
        registry_file.write_text(json.dumps(entries))
        return (
            patch.object(rm, "REGISTRY_FILE", registry_file),
            patch.object(rm, "REMINDERS_DIR", tmp_path),
        )

    def test_cancel_known_reminder_returns_cancelled_true(self, tmp_path: Path):
        from systemd_jobs import DeleteResult
        entries = [
            {
                "reminder_id": "test-123",
                "reminder_type": "interview-prep",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
                "job_name": "rem-somehash12",
            }
        ]
        mock_delete = AsyncMock(return_value=DeleteResult("rem-somehash12", "deleted"))
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2, patch("reminder_manager.delete_job", mock_delete):
            result = _run(rm.cancel_reminder("test-123"))
        assert result.cancelled is True
        assert result.reminder_id == "test-123"

    def test_cancel_marks_registry_entry_cancelled(self, tmp_path: Path):
        from systemd_jobs import DeleteResult
        entries = [
            {
                "reminder_id": "test-456",
                "reminder_type": "test",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
                "job_name": "rem-abc123",
            }
        ]
        mock_delete = AsyncMock(return_value=DeleteResult("rem-abc123", "deleted"))
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2, patch("reminder_manager.delete_job", mock_delete):
            _run(rm.cancel_reminder("test-456"))

        with p1, p2:
            loaded = rm._load_registry()
        assert loaded[0]["cancelled"] is True

    def test_cancel_calls_delete_job(self, tmp_path: Path):
        from systemd_jobs import DeleteResult
        entries = [
            {
                "reminder_id": "test-789",
                "reminder_type": "test",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
                "job_name": "rem-testjobname",
            }
        ]
        mock_delete = AsyncMock(return_value=DeleteResult("rem-testjobname", "deleted"))
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2, patch("reminder_manager.delete_job", mock_delete):
            _run(rm.cancel_reminder("test-789"))
        mock_delete.assert_called_once_with("rem-testjobname")

    def test_cancel_unknown_reminder_returns_cancelled_false(self, tmp_path: Path):
        p1, p2 = self._with_registry(tmp_path, [])
        with p1, p2:
            result = _run(rm.cancel_reminder("nonexistent-id"))
        assert result.cancelled is False

    def test_cancel_already_cancelled_reminder_returns_false(self, tmp_path: Path):
        entries = [
            {
                "reminder_id": "already-done",
                "reminder_type": "test",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": True,  # already cancelled
                "job_name": "rem-abc",
            }
        ]
        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2:
            result = _run(rm.cancel_reminder("already-done"))
        assert result.cancelled is False

    def test_systemd_delete_failure_propagates(self, tmp_path: Path):
        entries = [
            {
                "reminder_id": "fail-test",
                "reminder_type": "test",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {},
                "created_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancelled": False,
                "job_name": "rem-abc",
            }
        ]
        async def failing_delete(name):
            raise RuntimeError("sudo failed")

        p1, p2 = self._with_registry(tmp_path, entries)
        with p1, p2, patch("reminder_manager.delete_job", failing_delete):
            with pytest.raises(RuntimeError, match="sudo failed"):
                _run(rm.cancel_reminder("fail-test"))
