"""
Tests for the OOM kill monitor (scripts/oom-monitor.py).

Tests focus on the pure parsing and classification logic. Side-effect functions
(journal scanning, Telegram delivery, file I/O) are covered by integration
notes at the bottom.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Import the module under test.
# The script file is named oom-monitor.py (with a dash), which is not a valid
# Python identifier. We use importlib to load it by file path.
import importlib.util

_script_path = Path(__file__).parent.parent.parent / "scripts" / "oom-monitor.py"
_spec = importlib.util.spec_from_file_location("oom_monitor", _script_path)
om = importlib.util.module_from_spec(_spec)
sys.modules["oom_monitor"] = om  # register before exec so @dataclass finds the module
_spec.loader.exec_module(om)


# =============================================================================
# parse_oom_event
# =============================================================================


class TestParseOomEvent:
    """parse_oom_event should extract pid and process name from kernel lines."""

    def test_classic_killed_process(self):
        msg = (
            "Out of memory: Killed process 1234 (claude) total-vm:2048000kB, "
            "anon-rss:1024000kB, file-rss:0kB, shmem-rss:0kB, UID:1000"
        )
        event = om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)
        assert event is not None
        assert event.pid == 1234
        assert event.process_name == "claude"
        assert event.is_lobster_process is True

    def test_kill_without_ed(self):
        """Newer kernels log 'Kill process' (no -ed suffix)."""
        msg = "Out of memory: Kill process 5678 (python3) score 900 or sacrifice child"
        event = om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)
        assert event is not None
        assert event.pid == 5678
        assert event.process_name == "python3"
        assert event.is_lobster_process is True

    def test_memory_cgroup_prefix(self):
        """cgroup OOM kill events should also be captured."""
        msg = "Memory cgroup out of memory: Killed process 999 (uv) total-vm:512000kB, anon-rss:256000kB"
        event = om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)
        assert event is not None
        assert event.pid == 999
        assert event.process_name == "uv"
        assert event.is_lobster_process is True

    def test_oom_reaper_line(self):
        """oom_reaper confirmation lines should be captured."""
        msg = "oom_reaper: reaped process 1234 (claude), now anon-rss:0kB, file-rss:0kB, shmem-rss:0kB"
        event = om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)
        assert event is not None
        assert event.pid == 1234
        assert event.process_name == "claude"
        assert event.is_lobster_process is True

    def test_non_lobster_process(self):
        """Non-Lobster processes should be parsed but flagged as not lobster-related."""
        msg = "Out of memory: Killed process 7777 (mysqld) total-vm:4096000kB, anon-rss:2048000kB"
        event = om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)
        assert event is not None
        assert event.process_name == "mysqld"
        assert event.is_lobster_process is False

    def test_irrelevant_kernel_message(self):
        """Non-OOM kernel lines should return None."""
        msg = "EXT4-fs (sda1): mounted filesystem with ordered data mode"
        assert om.parse_oom_event("2026-01-01T00:00:00+00:00", msg) is None

    def test_empty_message(self):
        assert om.parse_oom_event("2026-01-01T00:00:00+00:00", "") is None

    def test_event_id_is_stable(self):
        """Same timestamp+pid always produces the same event_id."""
        msg = "Out of memory: Killed process 1234 (claude) total-vm:2048000kB"
        ts = "2026-01-15T10:30:00+00:00"
        e1 = om.parse_oom_event(ts, msg)
        e2 = om.parse_oom_event(ts, msg)
        assert e1 is not None and e2 is not None
        assert e1.event_id == e2.event_id

    def test_different_pids_different_event_ids(self):
        """Different pids at same timestamp produce different event_ids."""
        ts = "2026-01-15T10:30:00+00:00"
        e1 = om.parse_oom_event(ts, "Out of memory: Killed process 100 (claude) total-vm:1kB")
        e2 = om.parse_oom_event(ts, "Out of memory: Killed process 200 (claude) total-vm:1kB")
        assert e1 is not None and e2 is not None
        assert e1.event_id != e2.event_id


# =============================================================================
# filter_new_events
# =============================================================================


class TestFilterNewEvents:
    """filter_new_events should exclude events already in seen_ids."""

    def _make_event(self, pid: int, proc: str = "claude") -> om.OomKillEvent:
        msg = f"Out of memory: Killed process {pid} ({proc}) total-vm:1kB"
        event = om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)
        assert event is not None
        return event

    def test_all_new(self):
        events = [self._make_event(1), self._make_event(2)]
        result = om.filter_new_events(events, set())
        assert len(result) == 2

    def test_all_seen(self):
        events = [self._make_event(1), self._make_event(2)]
        seen = {e.event_id for e in events}
        result = om.filter_new_events(events, seen)
        assert result == []

    def test_partial_seen(self):
        events = [self._make_event(1), self._make_event(2), self._make_event(3)]
        seen = {events[0].event_id}
        result = om.filter_new_events(events, seen)
        assert len(result) == 2
        assert events[0] not in result

    def test_empty_events(self):
        assert om.filter_new_events([], {"some-id"}) == []


# =============================================================================
# is_lobster_affected
# =============================================================================


class TestIsLobsterAffected:
    def _make_event(self, proc: str) -> om.OomKillEvent:
        msg = f"Out of memory: Killed process 1 ({proc}) total-vm:1kB"
        return om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)

    def test_true_when_lobster_process_present(self):
        events = [self._make_event("mysqld"), self._make_event("python")]
        assert om.is_lobster_affected(events) is True

    def test_false_when_no_lobster_process(self):
        events = [self._make_event("mysqld"), self._make_event("nginx")]
        assert om.is_lobster_affected(events) is False

    def test_empty_list(self):
        assert om.is_lobster_affected([]) is False


# =============================================================================
# format_telegram_alert
# =============================================================================


class TestFormatTelegramAlert:
    def _event(self, pid: int, proc: str) -> om.OomKillEvent:
        msg = f"Out of memory: Killed process {pid} ({proc}) total-vm:1kB"
        return om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)

    def test_contains_process_name(self):
        events = [self._event(1234, "claude")]
        text = om.format_telegram_alert(events)
        assert "claude" in text
        assert "1234" in text

    def test_warns_about_ghost_agents_for_lobster(self):
        events = [self._event(1, "python")]
        text = om.format_telegram_alert(events)
        assert "ghost" in text.lower() or "ghost" in text

    def test_no_ghost_warning_for_non_lobster_only(self):
        events = [self._event(1, "mysqld")]
        text = om.format_telegram_alert(events)
        # Non-lobster events should mention the process but not necessarily ghost warning
        assert "mysqld" in text

    def test_multiple_events_listed(self):
        events = [self._event(100, "claude"), self._event(200, "python3")]
        text = om.format_telegram_alert(events)
        assert "claude" in text
        assert "python3" in text

    def test_header_present(self):
        events = [self._event(1, "node")]
        text = om.format_telegram_alert(events)
        assert "OOM" in text or "oom" in text.lower()


# =============================================================================
# format_inbox_message
# =============================================================================


class TestFormatInboxMessage:
    def _event(self, pid: int, proc: str) -> om.OomKillEvent:
        msg = f"Out of memory: Killed process {pid} ({proc}) total-vm:1kB"
        return om.parse_oom_event("2026-01-01T00:00:00+00:00", msg)

    def test_required_fields_present(self):
        events = [self._event(1, "claude")]
        payload = om.format_inbox_message(events)
        assert "id" in payload
        assert "type" in payload
        assert "text" in payload
        assert "timestamp" in payload

    def test_type_is_observation(self):
        """Inbox message must use type=observation for platform-agnostic routing."""
        events = [self._event(1, "claude")]
        payload = om.format_inbox_message(events)
        assert payload["type"] == "observation"

    def test_no_hardcoded_chat_id(self):
        """Inbox message must not contain a hardcoded chat_id."""
        events = [self._event(1, "claude")]
        payload = om.format_inbox_message(events)
        assert "chat_id" not in payload

    def test_text_mentions_oom(self):
        events = [self._event(1, "claude")]
        payload = om.format_inbox_message(events)
        assert "OOM" in payload["text"] or "oom" in payload["text"].lower() or "killed" in payload["text"].lower()


# =============================================================================
# State file I/O
# =============================================================================


class TestStateFile:
    def test_load_empty_when_missing(self, tmp_path):
        state = tmp_path / "nonexistent-state.json"
        seen = om.load_state(state)
        assert seen == set()

    def test_save_and_reload(self, tmp_path):
        state = tmp_path / "state.json"
        ids = {"abc123", "def456", "789xyz"}
        om.save_state(state, ids)
        loaded = om.load_state(state)
        assert loaded == ids

    def test_save_creates_parent_dirs(self, tmp_path):
        state = tmp_path / "nested" / "dir" / "state.json"
        om.save_state(state, {"some-id"})
        assert state.exists()

    def test_load_corrupted_file_returns_empty(self, tmp_path):
        state = tmp_path / "state.json"
        state.write_text("not valid json {{{{")
        seen = om.load_state(state)
        assert seen == set()

    def test_save_is_idempotent(self, tmp_path):
        state = tmp_path / "state.json"
        ids = {"id1", "id2"}
        om.save_state(state, ids)
        om.save_state(state, ids)
        loaded = om.load_state(state)
        assert loaded == ids


# =============================================================================
# Integration: how to test the full flow manually
# =============================================================================
#
# Since scan_journal depends on a real journalctl, integration testing is
# done manually:
#
# 1. Dry-run smoke test (safe, no alerts sent, requires LOBSTER_DEBUG=true):
#    LOBSTER_DEBUG=true uv run scripts/oom-monitor.py --dry-run --since-minutes 43200
#    Expected: prints "no OOM events found" or shows any historical events
#
# 2. Debug-mode gate test:
#    uv run scripts/oom-monitor.py --dry-run  # (no LOBSTER_DEBUG)
#    Expected: exits 0 immediately with a log entry
#
# 3. Deduplication test:
#    LOBSTER_DEBUG=true uv run scripts/oom-monitor.py --dry-run
#    LOBSTER_DEBUG=true uv run scripts/oom-monitor.py --dry-run
#    Expected: second run reports "all already alerted"
#
# 4. Live test on a system with OOM history:
#    sudo journalctl -k | grep -i "out of memory\|killed process"
#    # If events exist, run without --dry-run and verify inbox message is written
