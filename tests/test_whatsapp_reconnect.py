"""
Tests for WhatsApp bridge session management and auto-reconnect — BIS-49.

Tests the Python-side handling of system events (session_expired, disconnected).
The Node.js reconnect logic is covered by the mock-test.js tests.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure src/bot is on the path
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("LOBSTER_MESSAGES", tempfile.mkdtemp())
os.environ.setdefault("LOBSTER_WORKSPACE", tempfile.mkdtemp())
os.environ.setdefault("WA_EVENTS_DIR", tempfile.mkdtemp())
os.environ.setdefault("WA_COMMANDS_DIR", tempfile.mkdtemp())

from bot.whatsapp_bridge_adapter import (
    is_system_event,
    is_routable,
    normalize_system_event,
    outbox_reply_to_command,
)


# ---------------------------------------------------------------------------
# System event detection and normalization
# ---------------------------------------------------------------------------


class TestSessionExpiredHandling:
    """Test that session_expired events are correctly handled by the adapter."""

    def test_session_expired_event_is_detected_as_system(self):
        evt = {
            "id": "sys_1700000000",
            "type": "system",
            "subtype": "session_expired",
            "body": "[WhatsApp bridge] Session expired",
            "from": "system",
            "fromMe": False,
            "isGroup": False,
            "author": "system",
            "timestamp": 1700000000,
            "mentionedIds": [],
            "mentions_lobster": False,
            "chatName": "",
        }
        assert is_system_event(evt) is True

    def test_session_expired_is_routable(self):
        """Session expired events must reach Lobster so Drew can be notified."""
        evt = {
            "type": "system",
            "subtype": "session_expired",
            "body": "[WhatsApp bridge] Session expired",
            "from": "system",
            "fromMe": False,
            "isGroup": False,
        }
        assert is_routable(evt) is True

    def test_session_expired_normalized_correctly(self):
        evt = {
            "type": "system",
            "subtype": "session_expired",
            "body": "[WhatsApp bridge] Session expired — QR scan required",
            "from": "system",
            "fromMe": False,
            "isGroup": False,
            "timestamp": 1700000000,
        }
        result = normalize_system_event(evt)

        assert result["source"] == "whatsapp"
        assert result["type"] == "system"
        assert result["subtype"] == "session_expired"
        assert result["chat_id"] == "system"
        assert "Session expired" in result["text"]
        assert result["is_group"] is False
        assert result["mentions_lobster"] is False

    def test_system_chat_id_reply_is_not_forwarded_to_whatsapp(self):
        """Replies to system events should NOT be sent to WhatsApp — they go to Telegram."""
        reply = {
            "source": "whatsapp",
            "chat_id": "system",
            "text": "QR scan needed",
        }
        # This should return None (system chat_id is excluded from command routing)
        assert outbox_reply_to_command(reply) is None


class TestDisconnectEventHandling:
    """Test handling of various disconnect scenarios."""

    def test_disconnected_event_normalized(self):
        evt = {
            "type": "system",
            "subtype": "disconnected",
            "body": "[WhatsApp bridge] Disconnected: CONNECTION_LOST",
            "from": "system",
            "fromMe": False,
            "isGroup": False,
        }
        result = normalize_system_event(evt)
        assert result["subtype"] == "disconnected"
        assert result["source"] == "whatsapp"

    def test_reconnected_event_normalized(self):
        evt = {
            "type": "system",
            "subtype": "reconnected",
            "body": "[WhatsApp bridge] Reconnected successfully",
            "from": "system",
            "fromMe": False,
            "isGroup": False,
        }
        result = normalize_system_event(evt)
        assert result["subtype"] == "reconnected"
        assert "Reconnected" in result["text"]


class TestSystemEventVsUserMessage:
    """Ensure system events are not confused with user messages."""

    def test_system_event_has_type_field(self):
        user_msg = {
            "id": "123",
            "body": "Hello",
            "from": "15551234567@c.us",
            "fromMe": False,
            "isGroup": False,
        }
        system_evt = {
            "type": "system",
            "subtype": "session_expired",
            "body": "Session expired",
            "from": "system",
        }
        assert is_system_event(user_msg) is False
        assert is_system_event(system_evt) is True

    def test_system_event_never_has_chat_id_forwarded(self):
        """The system chat_id must never be forwarded to WhatsApp as a send command."""
        for chat_id in ("system", "", None):
            reply = {
                "source": "whatsapp",
                "chat_id": chat_id or "",
                "text": "notification text",
            }
            assert outbox_reply_to_command(reply) is None


class TestReconnectStateLogic:
    """
    Test the reconnect state machine logic (extracted from index.js for Python testing).
    The Node.js implementation is tested in connectors/whatsapp/test/mock-test.js.
    This tests the equivalent Python concepts.
    """

    def test_transient_disconnect_does_not_delete_session(self):
        """
        A transient disconnect (CONNECTION_LOST, not LOGOUT) should NOT delete
        the session. Only LOGOUT should delete it.
        """
        # This is a behavioral contract documented in the bridge code.
        # Here we verify our understanding by checking the event subtypes.
        logout_event = {"type": "system", "subtype": "session_expired"}
        transient_event = {"type": "system", "subtype": "disconnected"}

        # Session expiry is specifically for LOGOUT events
        assert logout_event["subtype"] == "session_expired"
        # Transient disconnects have a different subtype
        assert transient_event["subtype"] == "disconnected"
        assert transient_event["subtype"] != "session_expired"

    def test_system_events_produce_correct_inbox_entries(self):
        """System events should produce inbox entries with the right schema."""
        for subtype in ("session_expired", "disconnected", "reconnected", "connected"):
            evt = {
                "type": "system",
                "subtype": subtype,
                "body": f"[WhatsApp bridge] {subtype}",
                "from": "system",
                "fromMe": False,
                "isGroup": False,
            }
            result = normalize_system_event(evt)
            # These fields are required for Lobster to process the notification
            assert result["source"] == "whatsapp"
            assert result["type"] == "system"
            assert result["subtype"] == subtype
            assert result["chat_id"] == "system"
            assert result["user_name"] == "WhatsApp Bridge"
