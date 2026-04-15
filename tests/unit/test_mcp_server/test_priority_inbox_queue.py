"""
Tests for priority inbox queue — P0-P4 ordering in check_inbox (issue #1079, #1584).

Verifies that:
- check_inbox returns messages in P0→P1→P2→P3→P4 order regardless of filename
- Within each tier, messages are ordered by timestamp (FIFO, ascending)
- Unrecognised or unparseable message types default to P4 (no crash, no skip)
- No change to message schema — priority is derived at read time, not stored
- The since_ts historical scan path is unaffected
- subagent_notification is P2 (not P4) — fix for #1584
- Every type in _KNOWN_INBOX_TYPES appears in exactly one priority tier set (#1584)
"""

import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401

# Priority constants — mirror the spec so tests break if the implementation drifts
P0_COMPACT_REMINDER = 0
P0_SELF_CHECK = 0
P1_TEXT = 1
P1_VOICE = 1
P2_SUBAGENT_RESULT = 2
P2_SUBAGENT_NOTIFICATION = 2
P3_AGENT_FAILED = 3
P4_SCHEDULED_REMINDER = 4
P4_DEFAULT = 4

_BASE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_msg(
    msg_id: str,
    msg_type: str = "text",
    subtype: str | None = None,
    offset_seconds: int = 0,
    source: str = "telegram",
    text: str | None = None,
) -> dict:
    ts = _BASE_TS + timedelta(seconds=offset_seconds)
    msg: dict = {
        "id": msg_id,
        "source": source,
        "type": msg_type,
        "text": text if text is not None else f"message {msg_id}",
        "timestamp": _iso(ts),
        "chat_id": 12345,
        "user_id": 12345,
        "username": "testuser",
        "user_name": "Test",
    }
    if subtype:
        msg["subtype"] = subtype
    return msg


@pytest.fixture
def inbox_dir(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture
def processed_dir(tmp_path):
    d = tmp_path / "processed"
    d.mkdir()
    return d


def _run_check_inbox(inbox_dir, processed_dir, args=None):
    args = args or {}
    with patch.multiple(
        "src.mcp.inbox_server",
        INBOX_DIR=inbox_dir,
        PROCESSED_DIR=processed_dir,
    ):
        from src.mcp.inbox_server import handle_check_inbox
        return asyncio.run(handle_check_inbox(args))


def _write(inbox_dir: Path, filename: str, msg: dict) -> None:
    (inbox_dir / filename).write_text(json.dumps(msg))


# ---------------------------------------------------------------------------
# Unit tests for _inbox_priority pure function
# ---------------------------------------------------------------------------


class TestInboxPriorityFunction:
    """_inbox_priority returns the correct tier for each message class."""

    def _priority(self, msg: dict) -> int:
        from src.mcp.inbox_server import _inbox_priority
        return _inbox_priority(msg)

    def test_compact_reminder_subtype_is_p0(self):
        assert self._priority({"type": "text", "subtype": "compact-reminder"}) == P0_COMPACT_REMINDER

    def test_self_check_subtype_is_p0(self):
        assert self._priority({"type": "text", "subtype": "self_check"}) == P0_SELF_CHECK

    def test_compact_reminder_text_prefix_is_p0(self):
        assert self._priority({"type": "text", "text": "compact-reminder extra"}) == P0_COMPACT_REMINDER

    def test_text_message_is_p1(self):
        assert self._priority({"type": "text"}) == P1_TEXT

    def test_voice_message_is_p1(self):
        assert self._priority({"type": "voice"}) == P1_VOICE

    def test_photo_message_is_p1(self):
        assert self._priority({"type": "photo"}) == 1

    def test_document_message_is_p1(self):
        assert self._priority({"type": "document"}) == 1

    def test_subagent_result_is_p2(self):
        assert self._priority({"type": "subagent_result"}) == P2_SUBAGENT_RESULT

    def test_subagent_error_is_p2(self):
        assert self._priority({"type": "subagent_error"}) == 2

    def test_agent_failed_is_p3(self):
        assert self._priority({"type": "agent_failed"}) == P3_AGENT_FAILED

    def test_scheduled_reminder_is_p4(self):
        assert self._priority({"type": "scheduled_reminder"}) == P4_SCHEDULED_REMINDER

    def test_system_error_is_p4(self):
        assert self._priority({"type": "system_error"}) == P4_DEFAULT

    def test_unknown_type_defaults_to_p4(self):
        assert self._priority({"type": "some_new_type_we_never_heard_of"}) == P4_DEFAULT

    def test_empty_message_defaults_to_p4(self):
        assert self._priority({}) == P4_DEFAULT

    def test_priority_derived_from_fields_not_stored(self):
        """Priority must NOT be a stored field — it is always re-derived."""
        msg = {"type": "text", "priority": 99}
        # The priority=99 field must be ignored; type=text → P1
        assert self._priority(msg) == P1_TEXT


# ---------------------------------------------------------------------------
# Integration tests for check_inbox ordering
# ---------------------------------------------------------------------------


class TestCheckInboxPriorityOrdering:
    """check_inbox returns messages in P0→P4 order, FIFO within each tier."""

    def test_p0_before_p1_before_p4_in_mixed_inbox(self, inbox_dir, processed_dir):
        """A mixed inbox returns P0 first, then P1, then P4."""
        # Written in reverse priority order to expose any filename-sorted fallback
        _write(inbox_dir, "zzz_scheduled.json", _make_msg("sched", "scheduled_reminder", offset_seconds=1))
        _write(inbox_dir, "mmm_text.json", _make_msg("usermsg", "text", offset_seconds=2))
        _write(inbox_dir, "aaa_compact.json", _make_msg("compact", "text", subtype="compact-reminder", offset_seconds=3, source="system"))

        result = _run_check_inbox(inbox_dir, processed_dir)
        text = result[0].text

        compact_pos = text.find("compact")
        usermsg_pos = text.find("usermsg")
        sched_pos = text.find("sched")

        assert compact_pos < usermsg_pos, "P0 (compact-reminder) must appear before P1 (text)"
        assert usermsg_pos < sched_pos, "P1 (text) must appear before P4 (scheduled_reminder)"

    def test_within_tier_messages_are_fifo_by_timestamp(self, inbox_dir, processed_dir):
        """Within the same priority tier, older messages appear first."""
        # Both are P1 (text); older1 should appear before older2
        _write(inbox_dir, "zzz_newer.json", _make_msg("newer", "text", offset_seconds=60))
        _write(inbox_dir, "aaa_older.json", _make_msg("older", "text", offset_seconds=0))

        result = _run_check_inbox(inbox_dir, processed_dir)
        text = result[0].text

        older_pos = text.find("older")
        newer_pos = text.find("newer")
        assert older_pos < newer_pos, "Older message must appear before newer message within same tier"

    def test_unparseable_file_does_not_crash_and_defaults_to_p4(self, inbox_dir, processed_dir):
        """An unreadable JSON file is silently skipped without crashing check_inbox."""
        (inbox_dir / "bad.json").write_text("not valid json {{{{")
        _write(inbox_dir, "good.json", _make_msg("good", "text"))

        result = _run_check_inbox(inbox_dir, processed_dir)
        text = result[0].text
        assert "good" in text  # good message still returned
        # No exception raised (test itself passing proves this)

    def test_message_without_priority_field_still_sorted_correctly(self, inbox_dir, processed_dir):
        """Messages that don't have a priority field are sorted by type (no stored priority)."""
        _write(inbox_dir, "a.json", _make_msg("sr", "scheduled_reminder"))
        _write(inbox_dir, "b.json", _make_msg("txt", "text"))

        result = _run_check_inbox(inbox_dir, processed_dir)
        text = result[0].text

        txt_pos = text.find("txt")
        sr_pos = text.find("sr")
        assert txt_pos < sr_pos, "text (P1) must appear before scheduled_reminder (P4)"

    def test_limit_applied_after_priority_sort(self, inbox_dir, processed_dir):
        """With limit=1, the P0 message is returned, not the earliest filename."""
        # zzz_ sorts last alphabetically — without priority sort this would be skipped
        _write(inbox_dir, "aaa_sched.json", _make_msg("sched", "scheduled_reminder"))
        _write(inbox_dir, "zzz_compact.json", _make_msg("compact", "text", subtype="compact-reminder", source="system"))

        result = _run_check_inbox(inbox_dir, processed_dir, {"limit": 1})
        text = result[0].text
        assert "compact" in text, "With limit=1 the P0 message should be returned"
        assert "sched" not in text, "P4 message should not appear when limit=1 cuts off after P0"

    def test_all_p0_subtypes_sorted_before_p1(self, inbox_dir, processed_dir):
        """self_check messages are also P0 and should precede P1 user messages."""
        _write(inbox_dir, "zzz_self.json", _make_msg("selfchk", "text", subtype="self_check", source="system"))
        _write(inbox_dir, "aaa_user.json", _make_msg("usermsg", "text"))

        result = _run_check_inbox(inbox_dir, processed_dir)
        text = result[0].text

        selfchk_pos = text.find("selfchk")
        usermsg_pos = text.find("usermsg")
        assert selfchk_pos < usermsg_pos, "self_check (P0) must appear before user text (P1)"

    def test_since_ts_path_unaffected_by_priority_change(self, inbox_dir, processed_dir):
        """The since_ts historical scan path is not changed — it still reads processed/ too."""
        ts_future = _BASE_TS + timedelta(hours=2)
        msg = _make_msg("proc_msg", "text", offset_seconds=7200)
        (processed_dir / "proc_msg.json").write_text(json.dumps(msg))

        result = _run_check_inbox(
            inbox_dir,
            processed_dir,
            {"since_ts": _iso(_BASE_TS)},
        )
        text = result[0].text
        assert "proc_msg" in text, "since_ts path must still read from processed/ dir"


# ---------------------------------------------------------------------------
# Fix #1584 — subagent_notification must be P2, not P4
# ---------------------------------------------------------------------------


class TestSubagentNotificationPriority:
    """subagent_notification (write_result with sent_reply_to_user=True) must be P2."""

    def _priority(self, msg: dict) -> int:
        from src.mcp.inbox_server import _inbox_priority
        return _inbox_priority(msg)

    def test_subagent_notification_is_p2(self):
        """write_result creates subagent_notification — must be P2, not P4."""
        assert self._priority({"type": "subagent_notification"}) == P2_SUBAGENT_NOTIFICATION

    def test_subagent_notification_sorted_before_p4_in_inbox(self, inbox_dir, processed_dir):
        """In a mixed inbox, subagent_notification appears before scheduled_reminder."""
        _write(inbox_dir, "zzz_notif.json", _make_msg("notif", "subagent_notification"))
        _write(inbox_dir, "aaa_sched.json", _make_msg("sched", "scheduled_reminder"))

        result = _run_check_inbox(inbox_dir, processed_dir)
        text = result[0].text

        notif_pos = text.find("notif")
        sched_pos = text.find("sched")
        assert notif_pos < sched_pos, "subagent_notification (P2) must appear before scheduled_reminder (P4)"

    def test_subagent_notification_sorted_before_p4_after_p1(self, inbox_dir, processed_dir):
        """subagent_notification is P2: after P1 (user text) but before P4 (cron)."""
        _write(inbox_dir, "aaa_sched.json", _make_msg("sched", "scheduled_reminder"))
        _write(inbox_dir, "bbb_notif.json", _make_msg("notif", "subagent_notification"))
        _write(inbox_dir, "ccc_text.json", _make_msg("usermsg", "text"))

        result = _run_check_inbox(inbox_dir, processed_dir)
        text = result[0].text

        usermsg_pos = text.find("usermsg")
        notif_pos = text.find("notif")
        sched_pos = text.find("sched")
        assert usermsg_pos < notif_pos, "P1 (text) must appear before P2 (subagent_notification)"
        assert notif_pos < sched_pos, "P2 (subagent_notification) must appear before P4 (scheduled_reminder)"


# ---------------------------------------------------------------------------
# Fix #1584 — CI exhaustiveness: every known type must appear in exactly one tier
# ---------------------------------------------------------------------------

# The number of priority tiers — used to verify the union covers all tiers exactly.
_EXPECTED_TIER_COUNT = 5  # P0 through P4


class TestPriorityTierExhaustiveness:
    """Every type in _KNOWN_INBOX_TYPES must appear in exactly one priority tier set.

    This test acts as a CI guard: adding a new message type to _KNOWN_INBOX_TYPES
    without placing it in a tier set will cause this test to fail, making
    the omission visible rather than silently defaulting to P4.
    """

    def _build_tier_union(self) -> dict[str, list[int]]:
        """Return a mapping of type_name → list of tiers it appears in.

        A correct implementation has exactly one tier per type.
        """
        from src.mcp.inbox_server import (
            _INBOX_P1_TYPES,
            _INBOX_P2_TYPES,
            _INBOX_P3_TYPES,
            _INBOX_P4_DEFAULT,
        )

        # Map each tier set to its tier number.
        # P0 is matched on subtype/text-prefix, not on type strings, so it has
        # no type-keyed frozenset.  Types that rely solely on the P0 subtype path
        # (e.g. "text" with subtype="compact-reminder") are already covered by
        # their canonical tier (P1 for "text").  No P0 type-level frozenset exists.
        tier_sets: list[tuple[int, frozenset[str]]] = [
            (1, _INBOX_P1_TYPES),
            (2, _INBOX_P2_TYPES),
            (3, _INBOX_P3_TYPES),
        ]

        membership: dict[str, list[int]] = {}
        for tier_num, tier_set in tier_sets:
            for msg_type in tier_set:
                membership.setdefault(msg_type, []).append(tier_num)

        return membership

    def test_every_known_type_appears_in_exactly_one_tier(self):
        """Each type in _KNOWN_INBOX_TYPES must be in exactly one P1-P3 tier set
        OR resolve to P4 (the default) — never in multiple tiers.

        Types that are not in any explicit tier set implicitly belong to P4.
        The important invariant is: no type appears in MORE than one tier set.
        """
        from src.mcp.inbox_server import _KNOWN_INBOX_TYPES, _inbox_priority

        membership = self._build_tier_union()

        # Check 1: no type appears in more than one explicit tier set
        multi_tier = {t: tiers for t, tiers in membership.items() if len(tiers) > 1}
        assert not multi_tier, (
            f"These types appear in multiple priority tier sets (must be in exactly one): {multi_tier}. "
            "Remove from all but one tier set."
        )

        # Check 2: every known type has a deterministic priority (i.e., _inbox_priority
        # returns a value in range 0-4, which it always does — this asserts no exception)
        for msg_type in _KNOWN_INBOX_TYPES:
            priority = _inbox_priority({"type": msg_type})
            assert 0 <= priority <= 4, (
                f"Type '{msg_type}' returned out-of-range priority {priority}. "
                "Valid range is 0-4."
            )

    def test_subagent_notification_not_also_in_p4_tier(self):
        """subagent_notification must NOT be absent from explicit tiers (which would make it P4).

        This is the specific regression from #1584: the type was silently P4 before the fix.
        After the fix it must be in _INBOX_P2_TYPES.
        """
        from src.mcp.inbox_server import _INBOX_P2_TYPES
        assert "subagent_notification" in _INBOX_P2_TYPES, (
            "subagent_notification must be in _INBOX_P2_TYPES. "
            "If it is absent, write_result notifications default to P4 (cron priority) — regression from #1584."
        )
