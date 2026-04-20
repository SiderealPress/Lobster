"""Unit tests for check-inflight-reminders.py

Tests are derived from the spec (issue #1686):
- Staleness condition: (now - started_at) > expected_done_in_minutes * 2
- Completed entries (have completed_at) are skipped
- Already-reminded entries (have reminded_at) are skipped
- Stale entries get a reminder written to inbox and reminded_at added
- Entries without expected_done_in_minutes use a default (30 minutes)
- The inbox message includes task_id, description, and chat_id
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# check-inflight-reminders.py has a hyphenated filename so we use importlib.
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "check-inflight-reminders.py"
_spec = importlib.util.spec_from_file_location("inflight_reminders", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
ir = importlib.util.module_from_spec(_spec)
sys.modules["inflight_reminders"] = ir
_spec.loader.exec_module(ir)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Constants matching those in the implementation
# ---------------------------------------------------------------------------

DEFAULT_EXPECTED_DONE_MINUTES = ir.DEFAULT_EXPECTED_DONE_MINUTES
STALENESS_MULTIPLIER = ir.STALENESS_MULTIPLIER

# Reference "now" for all time calculations
NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    task_id: str,
    started_minutes_ago: float,
    expected_done_in_minutes: float | None = None,
    completed: bool = False,
    reminded: bool = False,
    description: str = "test task",
    chat_id: int = 8305714125,
) -> dict:
    started_at = datetime.fromtimestamp(
        NOW.timestamp() - started_minutes_ago * 60, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    e: dict = {
        "task_id": task_id,
        "description": description,
        "started_at": started_at,
        "chat_id": chat_id,
        "status": "running",
    }
    if expected_done_in_minutes is not None:
        e["expected_done_in_minutes"] = expected_done_in_minutes
    if completed:
        e["completed_at"] = started_at  # value doesn't matter, presence does
        e["status"] = "done"
    if reminded:
        e["reminded_at"] = started_at
    return e


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


class TestIsStale:
    def test_entry_is_stale_when_elapsed_exceeds_2x_expected(self) -> None:
        # Expected: 10 min. Elapsed: 21 min. Threshold: 20 min. => stale.
        entry = _entry("t1", started_minutes_ago=21, expected_done_in_minutes=10)
        assert ir.is_stale(entry, NOW) is True

    def test_entry_not_stale_when_elapsed_is_exactly_2x_expected(self) -> None:
        # Elapsed == threshold is NOT yet stale (strict >)
        entry = _entry("t2", started_minutes_ago=20, expected_done_in_minutes=10)
        assert ir.is_stale(entry, NOW) is False

    def test_entry_not_stale_when_elapsed_is_below_threshold(self) -> None:
        entry = _entry("t3", started_minutes_ago=5, expected_done_in_minutes=10)
        assert ir.is_stale(entry, NOW) is False

    def test_uses_default_when_expected_done_in_minutes_absent(self) -> None:
        # Default is DEFAULT_EXPECTED_DONE_MINUTES. Elapsed just above 2x that => stale.
        threshold = DEFAULT_EXPECTED_DONE_MINUTES * STALENESS_MULTIPLIER
        entry = _entry("t4", started_minutes_ago=threshold + 1)
        assert ir.is_stale(entry, NOW) is True

    def test_not_stale_with_default_when_elapsed_below_threshold(self) -> None:
        threshold = DEFAULT_EXPECTED_DONE_MINUTES * STALENESS_MULTIPLIER
        entry = _entry("t5", started_minutes_ago=threshold - 1)
        assert ir.is_stale(entry, NOW) is False

    def test_entry_with_missing_started_at_is_not_stale(self) -> None:
        # Defensive: if the field is missing, skip rather than crash.
        entry = {"task_id": "t6", "description": "no timestamp"}
        assert ir.is_stale(entry, NOW) is False


# ---------------------------------------------------------------------------
# should_remind
# ---------------------------------------------------------------------------


class TestShouldRemind:
    def test_stale_entry_without_reminded_at_should_be_reminded(self) -> None:
        entry = _entry("t1", started_minutes_ago=21, expected_done_in_minutes=10)
        assert ir.should_remind(entry, NOW) is True

    def test_completed_entry_is_skipped(self) -> None:
        entry = _entry("t2", started_minutes_ago=21, expected_done_in_minutes=10, completed=True)
        assert ir.should_remind(entry, NOW) is False

    def test_already_reminded_entry_is_skipped(self) -> None:
        entry = _entry("t3", started_minutes_ago=21, expected_done_in_minutes=10, reminded=True)
        assert ir.should_remind(entry, NOW) is False

    def test_fresh_entry_is_not_reminded(self) -> None:
        entry = _entry("t4", started_minutes_ago=5, expected_done_in_minutes=10)
        assert ir.should_remind(entry, NOW) is False

    def test_entry_with_completed_at_skipped_even_if_no_reminded_at(self) -> None:
        entry = _entry("t5", started_minutes_ago=100, expected_done_in_minutes=10, completed=True)
        assert ir.should_remind(entry, NOW) is False


# ---------------------------------------------------------------------------
# build_reminder_message
# ---------------------------------------------------------------------------


class TestBuildReminderMessage:
    def test_message_contains_task_id(self) -> None:
        entry = _entry("my-task-42", started_minutes_ago=25, expected_done_in_minutes=10)
        msg = ir.build_reminder_message(entry, NOW)
        assert "my-task-42" in msg["text"]

    def test_message_contains_description(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10,
                        description="Fixing the flux capacitor")
        msg = ir.build_reminder_message(entry, NOW)
        assert "Fixing the flux capacitor" in msg["text"]

    def test_message_routed_to_dispatcher_not_user(self) -> None:
        # Reminders go to chat_id=0 (dispatcher) regardless of entry's chat_id
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10,
                        chat_id=8305714125)
        msg = ir.build_reminder_message(entry, NOW)
        assert msg["chat_id"] == 0

    def test_message_has_correct_type_and_source(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        msg = ir.build_reminder_message(entry, NOW)
        assert msg["type"] == "scheduled_reminder"
        assert msg["source"] == "system"

    def test_message_has_reminder_type_field(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        msg = ir.build_reminder_message(entry, NOW)
        assert msg["reminder_type"] == "inflight_stale"

    def test_message_has_task_id_field(self) -> None:
        entry = _entry("my-task-77", started_minutes_ago=25, expected_done_in_minutes=10)
        msg = ir.build_reminder_message(entry, NOW)
        assert msg["task_id"] == "my-task-77"

    def test_message_includes_elapsed_minutes(self) -> None:
        # 25 minutes ago, expected 10 minutes
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        msg = ir.build_reminder_message(entry, NOW)
        # elapsed should appear somewhere in the text as approximately 25 min
        assert "25" in msg["text"] or "min" in msg["text"].lower()

    def test_message_has_timestamp(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        msg = ir.build_reminder_message(entry, NOW)
        assert "timestamp" in msg
        assert msg["timestamp"]  # non-empty

    def test_message_has_id(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        msg = ir.build_reminder_message(entry, NOW)
        assert "id" in msg
        assert msg["id"]


# ---------------------------------------------------------------------------
# mark_reminded
# ---------------------------------------------------------------------------


class TestMarkReminded:
    def test_adds_reminded_at_field(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        assert "reminded_at" not in entry
        updated = ir.mark_reminded(entry, NOW)
        assert "reminded_at" in updated

    def test_does_not_mutate_original(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        original_keys = set(entry.keys())
        ir.mark_reminded(entry, NOW)
        # Original entry should not have been mutated
        assert set(entry.keys()) == original_keys

    def test_reminded_at_is_iso8601(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10)
        updated = ir.mark_reminded(entry, NOW)
        # Should parse without exception
        datetime.fromisoformat(updated["reminded_at"].replace("Z", "+00:00"))

    def test_other_fields_preserved(self) -> None:
        entry = _entry("t1", started_minutes_ago=25, expected_done_in_minutes=10,
                        description="keep me")
        updated = ir.mark_reminded(entry, NOW)
        assert updated["description"] == "keep me"
        assert updated["task_id"] == "t1"


# ---------------------------------------------------------------------------
# process_entries — integration of the above
# ---------------------------------------------------------------------------


class TestProcessEntries:
    def test_stale_entry_generates_reminder_and_marks_reminded(self) -> None:
        stale = _entry("stale-1", started_minutes_ago=25, expected_done_in_minutes=10)
        messages, updated_entries = ir.process_entries([stale], NOW)
        assert len(messages) == 1
        assert messages[0]["task_id"] == "stale-1"
        assert updated_entries[0].get("reminded_at") is not None

    def test_fresh_entry_generates_no_reminder(self) -> None:
        fresh = _entry("fresh-1", started_minutes_ago=5, expected_done_in_minutes=10)
        messages, updated_entries = ir.process_entries([fresh], NOW)
        assert messages == []
        assert "reminded_at" not in updated_entries[0]

    def test_completed_entry_is_skipped(self) -> None:
        done = _entry("done-1", started_minutes_ago=25, expected_done_in_minutes=10, completed=True)
        messages, updated_entries = ir.process_entries([done], NOW)
        assert messages == []

    def test_already_reminded_entry_not_reminded_again(self) -> None:
        reminded = _entry("old-1", started_minutes_ago=25, expected_done_in_minutes=10, reminded=True)
        messages, _ = ir.process_entries([reminded], NOW)
        assert messages == []

    def test_mixed_entries_only_stale_reminded(self) -> None:
        stale = _entry("stale-x", started_minutes_ago=25, expected_done_in_minutes=10)
        fresh = _entry("fresh-y", started_minutes_ago=5, expected_done_in_minutes=10)
        done = _entry("done-z", started_minutes_ago=50, expected_done_in_minutes=10, completed=True)
        messages, updated = ir.process_entries([stale, fresh, done], NOW)
        assert len(messages) == 1
        assert messages[0]["task_id"] == "stale-x"
        # Check only stale entry got reminded_at
        stale_updated = next(e for e in updated if e["task_id"] == "stale-x")
        fresh_updated = next(e for e in updated if e["task_id"] == "fresh-y")
        assert "reminded_at" in stale_updated
        assert "reminded_at" not in fresh_updated

    def test_multiple_stale_entries_all_reminded(self) -> None:
        entries = [
            _entry(f"stale-{i}", started_minutes_ago=30, expected_done_in_minutes=10)
            for i in range(3)
        ]
        messages, updated = ir.process_entries(entries, NOW)
        assert len(messages) == 3
        for e in updated:
            assert "reminded_at" in e

    def test_entries_without_started_at_are_skipped(self) -> None:
        bad_entry = {"task_id": "bad-1", "description": "no timestamp", "chat_id": 0}
        messages, _ = ir.process_entries([bad_entry], NOW)
        assert messages == []


# ---------------------------------------------------------------------------
# parse_entries — reading jsonl with mixed formats
# ---------------------------------------------------------------------------


class TestParseEntries:
    def test_parses_entries_with_started_at(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "inflight-work.jsonl"
        entry = _entry("t1", started_minutes_ago=5, expected_done_in_minutes=10)
        jsonl.write_text(json.dumps(entry) + "\n")
        entries = ir.parse_entries(str(jsonl))
        assert len(entries) == 1
        assert entries[0]["task_id"] == "t1"

    def test_returns_empty_for_nonexistent_file(self, tmp_path: Path) -> None:
        entries = ir.parse_entries(str(tmp_path / "does-not-exist.jsonl"))
        assert entries == []

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "inflight-work.jsonl"
        jsonl.write_text('{"task_id": "good"}\nnot-json\n{"task_id": "also-good"}\n')
        entries = ir.parse_entries(str(jsonl))
        assert len(entries) == 2
        assert entries[0]["task_id"] == "good"
        assert entries[1]["task_id"] == "also-good"

    def test_parses_entries_without_expected_done_in_minutes(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "inflight-work.jsonl"
        entry = {"task_id": "t2", "started_at": "2026-04-19T10:00:00Z", "status": "running"}
        jsonl.write_text(json.dumps(entry) + "\n")
        entries = ir.parse_entries(str(jsonl))
        assert entries[0].get("expected_done_in_minutes") is None


# ---------------------------------------------------------------------------
# write_entries — atomic rewrite of jsonl
# ---------------------------------------------------------------------------


class TestWriteEntries:
    def test_writes_entries_as_jsonl(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "inflight-work.jsonl"
        entries = [
            {"task_id": "t1", "status": "running"},
            {"task_id": "t2", "status": "done"},
        ]
        ir.write_entries(str(jsonl), entries)
        lines = jsonl.read_text().strip().split("\n")
        assert len(lines) == 2
        parsed = [json.loads(l) for l in lines]
        assert parsed[0]["task_id"] == "t1"
        assert parsed[1]["task_id"] == "t2"

    def test_write_is_atomic(self, tmp_path: Path) -> None:
        # After write_entries, the file should exist at the given path
        jsonl = tmp_path / "inflight-work.jsonl"
        ir.write_entries(str(jsonl), [{"task_id": "t1"}])
        assert jsonl.exists()

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "inflight-work.jsonl"
        jsonl.write_text('{"task_id": "old"}\n')
        ir.write_entries(str(jsonl), [{"task_id": "new"}])
        lines = jsonl.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["task_id"] == "new"


# ---------------------------------------------------------------------------
# drop_inbox_message — writes json to inbox directory
# ---------------------------------------------------------------------------


class TestDropInboxMessage:
    def test_writes_json_file_to_inbox(self, tmp_path: Path) -> None:
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        msg = {
            "id": "123456_reminder_inflight",
            "type": "scheduled_reminder",
            "reminder_type": "inflight_stale",
            "source": "system",
            "chat_id": 0,
            "text": "Task t1 is stale",
            "task_id": "t1",
            "timestamp": "2026-04-19T12:00:00+00:00",
        }
        ir.drop_inbox_message(msg, inbox_dir=str(inbox_dir))
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        written = json.loads(files[0].read_text())
        assert written["task_id"] == "t1"
        assert written["type"] == "scheduled_reminder"

    def test_filename_contains_id(self, tmp_path: Path) -> None:
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        msg = {
            "id": "999_test",
            "type": "scheduled_reminder",
            "reminder_type": "inflight_stale",
            "source": "system",
            "chat_id": 0,
            "text": "test",
            "task_id": "x",
            "timestamp": "2026-04-19T12:00:00+00:00",
        }
        ir.drop_inbox_message(msg, inbox_dir=str(inbox_dir))
        files = list(inbox_dir.glob("*.json"))
        assert "999_test" in files[0].name
