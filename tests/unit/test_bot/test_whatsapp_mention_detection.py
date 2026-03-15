"""
Unit tests for BIS-48: @lobster mention detection and group reply routing.

Tests the is_routable() and normalize_event() functions in
whatsapp_bridge_adapter.py to verify:

  1. Group message without @mention → NOT routed (filtered by is_routable)
  2. Group message with @mention (mentions_lobster=True) → routed, field preserved
  3. DM (non-group) → always routed regardless of mentions_lobster
  4. mentions_lobster field passes through correctly from bridge event
  5. group_name field passes through from 'group_name' key (whatsapp-bridge/index.js)
  6. group_name field passes through from 'chatName' key (connectors/whatsapp/index.js)
  7. fromMe events are never routed
"""

import sys
import os
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Add src/ to path so we can import the adapter without installing it
# ---------------------------------------------------------------------------

_repo_root = Path(__file__).resolve().parents[3]
_src_bot = _repo_root / "src" / "bot"
if str(_src_bot) not in sys.path:
    sys.path.insert(0, str(_src_bot))


def _import_adapter():
    """Import the adapter, mocking filesystem side-effects."""
    # Redirect dirs to /tmp so mkdir() calls don't fail or create real dirs
    import tempfile
    _tmp = tempfile.mkdtemp()
    os.environ.setdefault("LOBSTER_MESSAGES", _tmp)
    os.environ.setdefault("LOBSTER_WORKSPACE", _tmp)

    # Fresh import
    if "whatsapp_bridge_adapter" in sys.modules:
        del sys.modules["whatsapp_bridge_adapter"]

    import whatsapp_bridge_adapter as adapter
    return adapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LOBSTER_JID = "15551234567@c.us"
OTHER_JID = "19999999999@c.us"
GROUP_JID = "120363000000000001@g.us"


def _make_dm_event(**overrides) -> dict:
    """A direct-message bridge event (non-group)."""
    base = {
        "id": "false_19999999999@c.us_AABBCCDD",
        "body": "Hey Lobster, what's up?",
        "from": OTHER_JID,
        "fromMe": False,
        "isGroup": False,
        "author": None,
        "timestamp": 1700000000,
        "mentionedIds": [],
        "mentions_lobster": False,
        "chatName": "",
        "hasMedia": False,
        "type": "chat",
    }
    base.update(overrides)
    return base


def _make_group_event(**overrides) -> dict:
    """A group bridge event (default: no @mention of Lobster)."""
    base = {
        "id": "false_120363000000000001@g.us_AABBCCDD",
        "body": "General group chatter",
        "from": GROUP_JID,
        "fromMe": False,
        "isGroup": True,
        "author": OTHER_JID,
        "timestamp": 1700000001,
        "mentionedIds": [],
        "mentions_lobster": False,
        "chatName": "Dev Chat",
        "hasMedia": False,
        "type": "chat",
    }
    base.update(overrides)
    return base


def _make_group_mention_event(**overrides) -> dict:
    """A group bridge event that mentions Lobster."""
    base = _make_group_event(
        body=f"Hey @Lobster, help me!",
        mentionedIds=[LOBSTER_JID],
        mentions_lobster=True,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests: is_routable
# ---------------------------------------------------------------------------


class TestIsRoutable(unittest.TestCase):
    """Tests for is_routable() — the BIS-48 mention gate."""

    def setUp(self):
        self.adapter = _import_adapter()

    def test_group_message_without_mention_not_routed(self):
        """Group messages that don't mention Lobster must be filtered out."""
        event = _make_group_event(mentions_lobster=False)
        self.assertFalse(
            self.adapter.is_routable(event),
            "Group message without @mention must NOT be routed to inbox",
        )

    def test_group_message_with_mention_is_routed(self):
        """Group messages that mention Lobster must pass through."""
        event = _make_group_mention_event()
        self.assertTrue(
            self.adapter.is_routable(event),
            "Group message with @mention must be routed to inbox",
        )

    def test_dm_always_routed_regardless_of_mention(self):
        """Direct messages must always be routed — no mention required."""
        event = _make_dm_event(mentions_lobster=False, mentionedIds=[])
        self.assertTrue(
            self.adapter.is_routable(event),
            "DM must always be routed to inbox (no @mention needed)",
        )

    def test_dm_with_explicit_no_mention_still_routed(self):
        """DM with mentions_lobster=False still routes (mention field ignored for DMs)."""
        event = _make_dm_event(mentions_lobster=False)
        self.assertTrue(self.adapter.is_routable(event))

    def test_fromMe_never_routed(self):
        """Messages sent by this session must never reach the inbox."""
        event = _make_dm_event(fromMe=True)
        self.assertFalse(
            self.adapter.is_routable(event),
            "fromMe=True events must be filtered out",
        )

    def test_fromMe_group_never_routed(self):
        """fromMe group messages are also filtered, even if they have a mention."""
        event = _make_group_mention_event(fromMe=True)
        self.assertFalse(self.adapter.is_routable(event))

    def test_system_event_always_routed(self):
        """Bridge system events (e.g. session_expired) always route to inbox."""
        event = {
            "id": "sys_1234",
            "type": "system",
            "subtype": "session_expired",
            "body": "Session expired",
            "from": "system",
            "fromMe": False,
            "isGroup": False,
            "mentionedIds": [],
            "mentions_lobster": False,
        }
        self.assertTrue(self.adapter.is_routable(event))


# ---------------------------------------------------------------------------
# Tests: normalize_event — mentions_lobster and group_name passthrough
# ---------------------------------------------------------------------------


class TestNormalizeEventBis48(unittest.TestCase):
    """Tests for normalize_event() focusing on BIS-48 fields."""

    def setUp(self):
        self.adapter = _import_adapter()

    def test_mentions_lobster_true_passes_through(self):
        """mentions_lobster=True from bridge is preserved in the inbox message."""
        event = _make_group_mention_event(mentions_lobster=True)
        msg = self.adapter.normalize_event(event)
        self.assertTrue(
            msg["mentions_lobster"],
            "mentions_lobster=True must be preserved in the inbox message",
        )

    def test_mentions_lobster_false_passes_through(self):
        """mentions_lobster=False is preserved (even if we see it — group gate is upstream)."""
        event = _make_dm_event(mentions_lobster=False)
        msg = self.adapter.normalize_event(event)
        self.assertFalse(msg["mentions_lobster"])

    def test_group_name_from_chatName_field(self):
        """group_name is read from 'chatName' field (connectors/whatsapp/index.js format)."""
        event = _make_group_mention_event(chatName="Lobster Fan Club")
        msg = self.adapter.normalize_event(event)
        self.assertEqual(
            msg["group_name"],
            "Lobster Fan Club",
            "group_name must be read from 'chatName' field",
        )

    def test_group_name_from_group_name_field(self):
        """group_name is read from 'group_name' field (whatsapp-bridge/index.js format)."""
        event = _make_group_mention_event(chatName="", group_name="Backend Team")
        msg = self.adapter.normalize_event(event)
        self.assertEqual(
            msg["group_name"],
            "Backend Team",
            "group_name must also be read from 'group_name' field (BIS-48 bridge format)",
        )

    def test_group_name_chatName_takes_precedence_over_group_name(self):
        """chatName takes precedence if both fields are present."""
        event = _make_group_mention_event(chatName="Primary Name", group_name="Secondary Name")
        msg = self.adapter.normalize_event(event)
        self.assertEqual(msg["group_name"], "Primary Name")

    def test_group_name_empty_for_dm(self):
        """DMs must have an empty group_name."""
        event = _make_dm_event()
        msg = self.adapter.normalize_event(event)
        self.assertEqual(msg["group_name"], "")

    def test_mentions_lobster_in_dm_is_false(self):
        """DM with no mentions has mentions_lobster=False."""
        event = _make_dm_event()
        msg = self.adapter.normalize_event(event)
        self.assertFalse(msg["mentions_lobster"])

    def test_standard_inbox_fields_present(self):
        """Normalized message includes all required Lobster inbox schema fields."""
        event = _make_group_mention_event()
        msg = self.adapter.normalize_event(event)

        required_fields = [
            "id", "source", "chat_id", "user_id", "user_name",
            "text", "is_group", "group_name", "mentions_lobster", "timestamp",
        ]
        for field in required_fields:
            self.assertIn(field, msg, f"Missing required inbox field: {field}")

    def test_source_is_whatsapp(self):
        """source field is always 'whatsapp' for bridge events."""
        event = _make_dm_event()
        msg = self.adapter.normalize_event(event)
        self.assertEqual(msg["source"], "whatsapp")


# ---------------------------------------------------------------------------
# Tests: outbox_reply_to_command
# ---------------------------------------------------------------------------


class TestOutboxReplyToCommand(unittest.TestCase):
    """Tests for outbox_reply_to_command() — reply routing."""

    def setUp(self):
        self.adapter = _import_adapter()

    def test_whatsapp_reply_converts_to_command(self):
        """A whatsapp outbox reply produces a valid bridge send command."""
        reply = {"source": "whatsapp", "chat_id": OTHER_JID, "text": "Hello back!"}
        cmd = self.adapter.outbox_reply_to_command(reply)
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "send")
        self.assertEqual(cmd["to"], OTHER_JID)
        self.assertEqual(cmd["text"], "Hello back!")

    def test_non_whatsapp_reply_returns_none(self):
        """Replies from other sources (telegram, slack) must not produce a command."""
        reply = {"source": "telegram", "chat_id": "12345", "text": "Hi"}
        cmd = self.adapter.outbox_reply_to_command(reply)
        self.assertIsNone(cmd)

    def test_missing_chat_id_returns_none(self):
        """Replies with no chat_id are invalid and must be dropped."""
        reply = {"source": "whatsapp", "chat_id": "", "text": "Hello"}
        cmd = self.adapter.outbox_reply_to_command(reply)
        self.assertIsNone(cmd)

    def test_system_chat_id_returns_none(self):
        """Replies addressed to 'system' (bridge notifications) must not be sent."""
        reply = {"source": "whatsapp", "chat_id": "system", "text": "Ack"}
        cmd = self.adapter.outbox_reply_to_command(reply)
        self.assertIsNone(cmd)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    print("Running BIS-48 mention detection unit tests...\n")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestIsRoutable))
    suite.addTests(loader.loadTestsFromTestCase(TestNormalizeEventBis48))
    suite.addTests(loader.loadTestsFromTestCase(TestOutboxReplyToCommand))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.wasSuccessful():
        print("\nAll BIS-48 tests PASSED.")
        sys.exit(0)
    else:
        print("\nSome tests FAILED.")
        sys.exit(1)
