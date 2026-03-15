#!/usr/bin/env python3
"""
Unit tests for whatsapp_bridge_adapter.py

Tests cover:
  1. normalize_event: bridge JSON event → Lobster inbox dict
  2. Outbox watcher: new outbox file with source="whatsapp" → wa-commands file

Run with:
  python3 test_whatsapp_bridge_adapter.py
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Bootstrap: the adapter imports watchdog; mock it if unavailable so tests
# run in minimal environments that may not have all deps installed.
# ---------------------------------------------------------------------------

# Patch directory-creation side effects before importing the module
_orig_mkdir = Path.mkdir


def _noop_mkdir(self, *args, **kwargs):
    pass


# We will temporarily redirect the module's directory globals to temp dirs
# by patching os.environ before the import.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_bridge_event(**overrides) -> dict:
    """Return a minimal valid bridge event (as emitted by index.js)."""
    base = {
        "id": "false_12345678901@c.us_AABBCCDDEEFF",
        "body": "Hello Lobster!",
        "from": "12345678901@c.us",
        "fromMe": False,
        "isGroup": False,
        "author": None,
        "timestamp": 1700000000,
        "mentionedIds": [],
        "hasMedia": False,
        "type": "chat",
    }
    base.update(overrides)
    return base


def _sample_group_event(**overrides) -> dict:
    """Return a minimal valid group bridge event."""
    base = {
        "id": "false_120363000000000001@g.us_AABBCCDDEEFF",
        "body": "Hey group!",
        "from": "120363000000000001@g.us",
        "fromMe": False,
        "isGroup": True,
        "author": "9876543210@c.us",
        "timestamp": 1700000001,
        "mentionedIds": ["55512345678@c.us"],
        "hasMedia": False,
        "type": "chat",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNormalizeEvent(unittest.TestCase):
    """Tests for the pure normalize_event() function."""

    def setUp(self):
        # Import here so we can patch env vars first if needed
        import whatsapp_bridge_adapter as adapter
        self.adapter = adapter

    def test_basic_dm_event_fields(self):
        """normalize_event maps a 1:1 DM event to the correct inbox schema."""
        event = _sample_bridge_event()
        msg = self.adapter.normalize_event(event)

        self.assertIsNotNone(msg)
        self.assertEqual(msg["source"], "whatsapp")
        self.assertEqual(msg["chat_id"], "12345678901@c.us")
        self.assertEqual(msg["text"], "Hello Lobster!")
        self.assertFalse(msg["is_group"])
        self.assertEqual(msg["group_name"], "")
        self.assertFalse(msg["mentions_lobster"])
        self.assertIn("T", msg["timestamp"])  # ISO-8601 contains 'T'
        self.assertTrue(msg["id"].startswith(""), "id should be a non-empty string")
        self.assertIn("_wa_", msg["id"])

    def test_fromMe_returns_none(self):
        """Events with fromMe=True must be filtered out (they are our own sends)."""
        event = _sample_bridge_event(fromMe=True)
        result = self.adapter.normalize_event(event)
        self.assertIsNone(result)

    def test_group_event_fields(self):
        """Group events set is_group=True, group_name, and author as user_id."""
        event = _sample_group_event()
        msg = self.adapter.normalize_event(event)

        self.assertIsNotNone(msg)
        self.assertTrue(msg["is_group"])
        self.assertEqual(msg["group_name"], "120363000000000001@g.us")
        self.assertEqual(msg["user_id"], "9876543210@c.us")
        self.assertTrue(msg["mentions_lobster"])

    def test_timestamp_conversion(self):
        """Unix timestamp from bridge is converted to ISO-8601 UTC string."""
        event = _sample_bridge_event(timestamp=1700000000)
        msg = self.adapter.normalize_event(event)

        # Should be ISO format ending in +00:00 or Z
        ts = msg["timestamp"]
        self.assertIn("2023", ts)  # 1700000000 is in Nov 2023

    def test_missing_id_returns_none(self):
        """An event with an empty id is silently dropped."""
        event = _sample_bridge_event(id="")
        result = self.adapter.normalize_event(event)
        self.assertIsNone(result)

    def test_no_author_falls_back_to_from(self):
        """In a 1:1 chat the author field is None; user_id falls back to from."""
        event = _sample_bridge_event(author=None)
        msg = self.adapter.normalize_event(event)
        self.assertEqual(msg["user_id"], "12345678901@c.us")

    def test_build_wa_command(self):
        """build_wa_command produces the expected payload for the bridge."""
        cmd = self.adapter.build_wa_command("12345@c.us", "Hi there")
        self.assertEqual(cmd["action"], "send")
        self.assertEqual(cmd["to"], "12345@c.us")
        self.assertEqual(cmd["text"], "Hi there")


class TestOutboxWatcher(unittest.TestCase):
    """Integration-style tests for the WhatsAppOutboxHandler."""

    def setUp(self):
        import whatsapp_bridge_adapter as adapter
        self.adapter = adapter

        # Create isolated temp dirs for this test
        self._tmp = tempfile.mkdtemp()
        self._outbox = Path(self._tmp) / "outbox"
        self._wa_commands = Path(self._tmp) / "wa-commands"
        self._outbox.mkdir()
        self._wa_commands.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_outbox_file(self, payload: dict) -> Path:
        """Write a JSON file to the temp outbox and return its path."""
        path = self._outbox / f"reply_{int(time.time()*1000)}.json"
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def _run_handler(self, filepath: Path) -> None:
        """Invoke the handler's _process method with patched WA_COMMANDS_DIR."""
        # Patch WA_COMMANDS_DIR so commands land in our temp dir
        with patch.object(self.adapter, "WA_COMMANDS_DIR", self._wa_commands):
            handler = self.adapter.WhatsAppOutboxHandler()
            handler._process(str(filepath))

    def test_whatsapp_reply_generates_wa_command(self):
        """A whatsapp outbox file should produce exactly one wa-commands file."""
        payload = {
            "id": "test_reply_001",
            "source": "whatsapp",
            "chat_id": "12345678901@c.us",
            "text": "Hello back!",
        }
        filepath = self._write_outbox_file(payload)
        self._run_handler(filepath)

        command_files = list(self._wa_commands.glob("*.json"))
        self.assertEqual(len(command_files), 1, "Expected exactly one wa-commands file")

        with open(command_files[0]) as f:
            cmd = json.load(f)

        self.assertEqual(cmd["action"], "send")
        self.assertEqual(cmd["to"], "12345678901@c.us")
        self.assertEqual(cmd["text"], "Hello back!")

    def test_non_whatsapp_reply_is_ignored(self):
        """Outbox files for other sources (e.g. telegram) must be ignored."""
        payload = {
            "id": "tg_001",
            "source": "telegram",
            "chat_id": "999999",
            "text": "Not WhatsApp",
        }
        filepath = self._write_outbox_file(payload)
        self._run_handler(filepath)

        command_files = list(self._wa_commands.glob("*.json"))
        self.assertEqual(len(command_files), 0, "Should produce no wa-commands for non-whatsapp source")

    def test_outbox_file_removed_after_processing(self):
        """Processed outbox files should be deleted."""
        payload = {
            "id": "test_reply_002",
            "source": "whatsapp",
            "chat_id": "12345678901@c.us",
            "text": "Cleaning up",
        }
        filepath = self._write_outbox_file(payload)
        self.assertTrue(filepath.exists())

        self._run_handler(filepath)

        self.assertFalse(filepath.exists(), "Outbox file should be removed after processing")

    def test_invalid_reply_missing_chat_id_is_dropped(self):
        """A reply with no chat_id should not produce a wa-command."""
        payload = {
            "id": "bad_reply",
            "source": "whatsapp",
            "chat_id": "",
            "text": "No destination",
        }
        filepath = self._write_outbox_file(payload)
        self._run_handler(filepath)

        command_files = list(self._wa_commands.glob("*.json"))
        self.assertEqual(len(command_files), 0)

    def test_wa_command_filename_contains_timestamp(self):
        """wa-commands filenames should start with a millisecond timestamp."""
        payload = {
            "id": "ts_test",
            "source": "whatsapp",
            "chat_id": "12345678901@c.us",
            "text": "Timestamp check",
        }
        filepath = self._write_outbox_file(payload)
        self._run_handler(filepath)

        command_files = list(self._wa_commands.glob("*.json"))
        self.assertEqual(len(command_files), 1)

        fname = command_files[0].name
        parts = fname.split("_", 1)
        self.assertTrue(parts[0].isdigit(), f"Filename should start with timestamp digits, got: {fname}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # Ensure the adapter module is importable
    _bot_dir = Path(__file__).resolve().parent
    if str(_bot_dir) not in sys.path:
        sys.path.insert(0, str(_bot_dir))

    print("Running whatsapp_bridge_adapter unit tests...\n")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestNormalizeEvent))
    suite.addTests(loader.loadTestsFromTestCase(TestOutboxWatcher))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.wasSuccessful():
        print("\nAll tests PASSED.")
        sys.exit(0)
    else:
        print("\nSome tests FAILED.")
        sys.exit(1)
