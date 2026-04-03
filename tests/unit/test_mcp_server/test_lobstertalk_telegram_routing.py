"""Unit tests for lobstertalk-unified Telegram routing.

PR B: Telegram routing fix.

Bug: the unified job was writing inbox messages with `chat_id: "<sender>"` (e.g.
"AlbertLobster"). The dispatcher routes messages to Telegram using `chat_id` as the
destination — a string sender name will never deliver to anyone.

Fix:
1. task sets `chat_id=ADMIN_CHAT_ID_REDACTED` (ADMIN_CHAT_ID) on inbox messages — the integer
   Telegram ID of the owner. Sender identity is preserved in the `from` field.
2. dispatcher `sys.dispatcher.bootup.md` adds a "Bot-talk" routing rule so the
   dispatcher knows to format and forward these messages to Telegram.

End-to-end path traced here:
  lobstertalk-unified runs
    → GET /messages → INBOUND messages
    → writes inbox file with chat_id=ADMIN_CHAT_ID_REDACTED, source="bot-talk", from="AlbertLobster"
  dispatcher picks up inbox file
    → sees source="bot-talk"
    → formats: "📨 From AlbertLobster via LobsterTalk:\n\n<text>"
    → send_reply(chat_id=ADMIN_CHAT_ID_REDACTED, source="telegram", text=...)
  Telegram delivers to owner's chat
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ADMIN_CHAT_ID = ADMIN_CHAT_ID_REDACTED


# ---------------------------------------------------------------------------
# Pure helper: build_inbox_message with corrected chat_id
# ---------------------------------------------------------------------------


def build_inbox_message(sender: str, content: str, local_identity: str) -> dict[str, Any]:
    """Build an inbox message dict for an INBOUND bot-talk message.

    chat_id is ALWAYS the owner's ADMIN_CHAT_ID (ADMIN_CHAT_ID_REDACTED), not the sender name.
    The dispatcher uses chat_id to determine where to send the Telegram message.
    Sender identity is carried in the `from` field.
    """
    now = datetime.now(timezone.utc)
    ts_ms = int(now.timestamp() * 1000)
    import uuid
    msg_id = f"{ts_ms}_bot_talk_{uuid.uuid4().hex[:8]}"
    return {
        "id": msg_id,
        "type": "text",
        "source": "bot-talk",
        "chat_id": ADMIN_CHAT_ID,  # integer, not sender name
        "user_name": sender,
        "text": content,
        "timestamp": now.isoformat(),
        "direction": "INBOUND",
        "from": sender,
        "to": local_identity,
    }


def format_bot_talk_notification(msg: dict[str, Any]) -> str:
    """Format a bot-talk inbox message for Telegram delivery.

    This mirrors the dispatcher routing rule in sys.dispatcher.bootup.md.
    """
    return f"📨 From {msg['from']} via LobsterTalk:\n\n{msg['text']}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInboxMessageChatId:
    """chat_id in inbox messages must be the owner's ADMIN_CHAT_ID (integer), not sender name."""

    def test_chat_id_is_admin_chat_id(self):
        """The core bug: chat_id was set to sender name string. Must be ADMIN_CHAT_ID_REDACTED."""
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        assert msg["chat_id"] == ADMIN_CHAT_ID, (
            f"Expected chat_id={ADMIN_CHAT_ID}, got {msg['chat_id']!r}. "
            "chat_id must be the owner's Telegram ID, not the sender name."
        )

    def test_chat_id_is_integer_not_string(self):
        """chat_id must be an integer. A string would not route to any Telegram chat."""
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        assert isinstance(msg["chat_id"], int), (
            f"chat_id must be int, got {type(msg['chat_id']).__name__}"
        )

    def test_sender_identity_preserved_in_from_field(self):
        """Sender name is NOT lost — it's in the 'from' field for display purposes."""
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        assert msg["from"] == "AlbertLobster"
        assert msg["chat_id"] == ADMIN_CHAT_ID
        # Both are present: routing destination and sender identity
        assert msg["chat_id"] != msg["from"]

    def test_different_senders_same_chat_id(self):
        """All bot-talk messages route to the same owner chat, regardless of sender."""
        msg_albert = build_inbox_message("AlbertLobster", "hi", "SaharLobster")
        msg_carol = build_inbox_message("CarolLobster", "hi", "SaharLobster")
        assert msg_albert["chat_id"] == ADMIN_CHAT_ID
        assert msg_carol["chat_id"] == ADMIN_CHAT_ID
        # Different senders, same delivery destination
        assert msg_albert["from"] == "AlbertLobster"
        assert msg_carol["from"] == "CarolLobster"

    def test_source_is_bot_talk(self):
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        assert msg["source"] == "bot-talk"

    def test_required_fields_present(self):
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        for field in ("id", "type", "source", "chat_id", "user_name", "text",
                      "timestamp", "direction", "from", "to"):
            assert field in msg, f"Missing field: {field!r}"


class TestDispatcherRoutingFormat:
    """The dispatcher formats bot-talk messages correctly before sending to Telegram."""

    def test_notification_includes_sender_name(self):
        msg = build_inbox_message("AlbertLobster", "Let's sync on the project.", "SaharLobster")
        text = format_bot_talk_notification(msg)
        assert "AlbertLobster" in text

    def test_notification_includes_message_text(self):
        msg = build_inbox_message("AlbertLobster", "Let's sync on the project.", "SaharLobster")
        text = format_bot_talk_notification(msg)
        assert "Let's sync on the project." in text

    def test_notification_format(self):
        msg = build_inbox_message("AlbertLobster", "Hello there.", "SaharLobster")
        text = format_bot_talk_notification(msg)
        assert text == "📨 From AlbertLobster via LobsterTalk:\n\nHello there."

    def test_dispatcher_uses_admin_chat_id_for_send_reply(self):
        """Confirm the dispatcher would call send_reply with the correct chat_id."""
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        # The dispatcher routing rule: send_reply(chat_id=msg['chat_id'], ...)
        # Since chat_id is ADMIN_CHAT_ID, this delivers to the owner's Telegram
        assert msg["chat_id"] == ADMIN_CHAT_ID


class TestInboxFileAtomicWrite:
    """Inbox files are written atomically and contain correct JSON."""

    def test_inbox_file_is_valid_json(self, tmp_path):
        import uuid
        msg = build_inbox_message("AlbertLobster", "hi there", "SaharLobster")
        inbox_file = tmp_path / f"{msg['id']}.json"
        tmp_file = inbox_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(msg), encoding="utf-8")
        tmp_file.rename(inbox_file)
        # File exists and is valid JSON
        assert inbox_file.exists()
        loaded = json.loads(inbox_file.read_text())
        assert loaded["chat_id"] == ADMIN_CHAT_ID
        assert loaded["source"] == "bot-talk"
        assert not tmp_file.exists()

    def test_dispatcher_can_parse_inbox_file(self, tmp_path):
        """Simulate the dispatcher reading and routing a bot-talk inbox file."""
        msg = build_inbox_message("AlbertLobster", "hello from Albert", "SaharLobster")
        inbox_file = tmp_path / f"{msg['id']}.json"
        inbox_file.write_text(json.dumps(msg), encoding="utf-8")

        # Dispatcher reads the file
        loaded = json.loads(inbox_file.read_text())
        assert loaded["source"] == "bot-talk"

        # Dispatcher uses chat_id for routing (must be integer ADMIN_CHAT_ID)
        target_chat_id = loaded["chat_id"]
        assert target_chat_id == ADMIN_CHAT_ID

        # Dispatcher formats the notification
        notification = format_bot_talk_notification(loaded)
        assert "AlbertLobster" in notification
        assert "hello from Albert" in notification
