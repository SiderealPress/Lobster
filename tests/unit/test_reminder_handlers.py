"""
Tests for the inbox_server MCP handler functions for reminder primitives.

Tests handle_create_reminder, handle_list_reminders, handle_cancel_reminder.
All reminder_manager operations are mocked — these tests verify handler behavior
only: argument parsing, error propagation, and response format.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys

_VENV_SITE = Path("/home/lobster/lobster/.venv/lib/python3.12/site-packages")
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

_MCP_DIR = Path(__file__).parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import inbox_server as _inbox_server_module  # noqa: F401


def _run(coro):
    return asyncio.run(coro)


def _text(result):
    return result[0].text


NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
FUTURE_ISO = (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Stub reminder_manager types
# ---------------------------------------------------------------------------


class _CreateReminderResult:
    def __init__(self, reminder_id, fire_time_utc, job_name):
        self.reminder_id = reminder_id
        self.fire_time_utc = fire_time_utc
        self.job_name = job_name


class _ReminderInfo:
    def __init__(self, reminder_id, reminder_type, fire_time_utc, metadata=None,
                 created_at="2026-05-01T12:00:00Z", cancelled=False):
        self.reminder_id = reminder_id
        self.reminder_type = reminder_type
        self.fire_time_utc = fire_time_utc
        self.metadata = metadata or {}
        self.created_at = created_at
        self.cancelled = cancelled


class _CancelReminderResult:
    def __init__(self, reminder_id, cancelled):
        self.reminder_id = reminder_id
        self.cancelled = cancelled


# ---------------------------------------------------------------------------
# handle_create_reminder
# ---------------------------------------------------------------------------


class TestHandleCreateReminder:
    def _call(self, args, *, create_return=None, create_side_effect=None):
        if create_return is None and create_side_effect is None:
            create_return = _CreateReminderResult(
                "interview-prep-123", FUTURE_ISO, "rem-abc123"
            )
        mock_create = AsyncMock(
            return_value=create_return,
            side_effect=create_side_effect,
        )
        with patch("inbox_server._rm_create_reminder", mock_create):
            from inbox_server import handle_create_reminder
            return _run(handle_create_reminder(args))

    def test_success_returns_reminder_id(self):
        result = self._call({"reminder_type": "interview-prep", "fire_time_utc": FUTURE_ISO})
        text = _text(result)
        assert "interview-prep-123" in text
        assert FUTURE_ISO in text

    def test_success_includes_job_name(self):
        result = self._call({"reminder_type": "test", "fire_time_utc": FUTURE_ISO})
        assert "rem-abc123" in _text(result)

    def test_success_includes_cancel_hint(self):
        result = self._call({"reminder_type": "test", "fire_time_utc": FUTURE_ISO})
        assert "cancel_reminder" in _text(result)

    def test_valueerror_returns_error_message(self):
        result = self._call(
            {"reminder_type": "", "fire_time_utc": FUTURE_ISO},
            create_side_effect=ValueError("reminder_type cannot be empty"),
        )
        assert "Error" in _text(result)
        assert "reminder_type cannot be empty" in _text(result)

    def test_future_time_in_past_returns_error(self):
        result = self._call(
            {"reminder_type": "test", "fire_time_utc": "2020-01-01T00:00:00Z"},
            create_side_effect=ValueError("fire_time_utc must be in the future"),
        )
        assert "Error" in _text(result)

    def test_systemd_failure_returns_error(self):
        result = self._call(
            {"reminder_type": "test", "fire_time_utc": FUTURE_ISO},
            create_side_effect=RuntimeError("systemctl failed"),
        )
        assert "Error" in _text(result)
        assert "systemctl failed" in _text(result)

    def test_metadata_passed_through(self):
        captured = {}

        async def fake_create(reminder_type, fire_time_utc, metadata=None):
            captured["metadata"] = metadata
            return _CreateReminderResult("r-1", FUTURE_ISO, "rem-x")

        with patch("inbox_server._rm_create_reminder", fake_create):
            from inbox_server import handle_create_reminder
            _run(handle_create_reminder({
                "reminder_type": "test",
                "fire_time_utc": FUTURE_ISO,
                "metadata": {"context": "ACME interview"},
            }))
        assert captured["metadata"] == {"context": "ACME interview"}

    def test_invalid_metadata_type_returns_error(self):
        result = self._call({"reminder_type": "test", "fire_time_utc": FUTURE_ISO,
                             "metadata": "not-a-dict"})
        assert "Error" in _text(result)
        assert "metadata" in _text(result).lower()

    def test_missing_metadata_defaults_to_empty_dict(self):
        captured = {}

        async def fake_create(reminder_type, fire_time_utc, metadata=None):
            captured["metadata"] = metadata
            return _CreateReminderResult("r-1", FUTURE_ISO, "rem-x")

        with patch("inbox_server._rm_create_reminder", fake_create):
            from inbox_server import handle_create_reminder
            _run(handle_create_reminder({
                "reminder_type": "test",
                "fire_time_utc": FUTURE_ISO,
            }))
        assert captured["metadata"] == {}


# ---------------------------------------------------------------------------
# handle_list_reminders
# ---------------------------------------------------------------------------


class TestHandleListReminders:
    def _call(self, args, *, list_return=None):
        if list_return is None:
            list_return = []
        mock_list = MagicMock(return_value=list_return)
        with patch("inbox_server._rm_list_reminders", mock_list):
            from inbox_server import handle_list_reminders
            return _run(handle_list_reminders(args))

    def test_empty_list_returns_no_reminders_message(self):
        result = self._call({})
        assert "No" in _text(result)
        assert "pending" in _text(result)

    def test_lists_pending_reminders(self):
        reminders = [
            _ReminderInfo("r-1", "interview-prep", FUTURE_ISO),
            _ReminderInfo("r-2", "standup", FUTURE_ISO),
        ]
        result = self._call({}, list_return=reminders)
        text = _text(result)
        assert "r-1" in text
        assert "interview-prep" in text
        assert "standup" in text

    def test_shows_count(self):
        reminders = [_ReminderInfo("r-1", "test", FUTURE_ISO)]
        result = self._call({}, list_return=reminders)
        assert "1 reminder" in _text(result)

    def test_pending_only_true_by_default(self):
        captured = {}

        def fake_list(pending_only=True):
            captured["pending_only"] = pending_only
            return []

        with patch("inbox_server._rm_list_reminders", fake_list):
            from inbox_server import handle_list_reminders
            _run(handle_list_reminders({}))
        assert captured["pending_only"] is True

    def test_pending_only_false_passed_through(self):
        captured = {}

        def fake_list(pending_only=True):
            captured["pending_only"] = pending_only
            return []

        with patch("inbox_server._rm_list_reminders", fake_list):
            from inbox_server import handle_list_reminders
            _run(handle_list_reminders({"pending_only": False}))
        assert captured["pending_only"] is False

    def test_shows_metadata_when_present(self):
        reminders = [
            _ReminderInfo("r-1", "test", FUTURE_ISO, metadata={"context": "interview"})
        ]
        result = self._call({}, list_return=reminders)
        assert "interview" in _text(result)

    def test_exception_returns_error(self):
        def failing_list(pending_only=True):
            raise RuntimeError("registry read failed")

        with patch("inbox_server._rm_list_reminders", failing_list):
            from inbox_server import handle_list_reminders
            result = _run(handle_list_reminders({}))
        assert "Error" in _text(result)


# ---------------------------------------------------------------------------
# handle_cancel_reminder
# ---------------------------------------------------------------------------


class TestHandleCancelReminder:
    def _call(self, args, *, cancel_return=None, cancel_side_effect=None):
        if cancel_return is None and cancel_side_effect is None:
            cancel_return = _CancelReminderResult("r-1", True)
        mock_cancel = AsyncMock(
            return_value=cancel_return,
            side_effect=cancel_side_effect,
        )
        with patch("inbox_server._rm_cancel_reminder", mock_cancel):
            from inbox_server import handle_cancel_reminder
            return _run(handle_cancel_reminder(args))

    def test_success_cancelled_true(self):
        result = self._call({"reminder_id": "r-1"})
        assert "Cancelled" in _text(result)
        assert "r-1" in _text(result)

    def test_not_found_returns_helpful_message(self):
        result = self._call(
            {"reminder_id": "unknown"},
            cancel_return=_CancelReminderResult("unknown", False),
        )
        text = _text(result)
        assert "not found" in text.lower() or "already cancelled" in text.lower()
        # Helpfully suggests list_reminders
        assert "list_reminders" in text

    def test_missing_reminder_id_returns_error(self):
        result = self._call({})
        assert "Error" in _text(result)
        assert "reminder_id" in _text(result).lower()

    def test_systemd_failure_returns_error(self):
        result = self._call(
            {"reminder_id": "r-1"},
            cancel_side_effect=RuntimeError("systemctl failed"),
        )
        assert "Error" in _text(result)
        assert "systemctl failed" in _text(result)

    def test_cancel_reminder_id_passed_correctly(self):
        captured = {}

        async def fake_cancel(reminder_id):
            captured["reminder_id"] = reminder_id
            return _CancelReminderResult(reminder_id, True)

        with patch("inbox_server._rm_cancel_reminder", fake_cancel):
            from inbox_server import handle_cancel_reminder
            _run(handle_cancel_reminder({"reminder_id": "my-specific-reminder-id"}))
        assert captured["reminder_id"] == "my-specific-reminder-id"
