"""Unit tests for the SSH watcher self-echo filter (issue #1791).

The SSH watcher now skips inbox writes for messages where sender == MY_LOBSTER_NAME.
These are outbound messages re-delivered by the bot-talk relay — not inbound messages
from other Lobster instances.

Without the filter, every outbound POST by this Lobster is echoed back to the sender's
own message feed, which the SSH watcher picks up and routes to the inbox — causing a
tight dispatch loop (confirmed: 1757 messages flooded the inbox on Apr 24 2026).

The filter mirrors the BOT_TALK_SELF_USER check applied on the HTTP path in issue #1345.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import time

import pytest

# ---------------------------------------------------------------------------
# Load the SSH watcher module by path (it has no package structure).
# We patch subprocess and os.environ so no real SSH calls are made.
# ---------------------------------------------------------------------------

WATCHER_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "lobstertalk-ssh-watcher-job.py"
)

# Module name for importlib
_MODULE_NAME = "lobstertalk_ssh_watcher"


def _load_watcher_module(env_overrides: dict[str, str] | None = None):
    """Import the watcher module with env var overrides in effect.

    Because the module computes MY_LOBSTER_NAME at import time from env vars,
    we must reload it within the patched env to pick up different values.
    """
    env = {
        "BOT_TALK_SSH_HOST": "testhost",
        **(env_overrides or {}),
    }
    with patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, WATCHER_PATH)
        assert spec is not None, f"Could not find module at {WATCHER_PATH}"
        mod = importlib.util.module_from_spec(spec)
        # Fresh load — don't reuse cached version
        if _MODULE_NAME in sys.modules:
            del sys.modules[_MODULE_NAME]
        spec.loader.exec_module(mod)
        return mod


# ---------------------------------------------------------------------------
# Pure helper: extract the write_to_inbox logic as a testable function.
# These tests drive the core invariant: self-echoes never reach the inbox.
# ---------------------------------------------------------------------------

def _make_msg(sender: str, content: str = "hello") -> dict[str, Any]:
    """Build a minimal bot-talk message dict."""
    return {
        "sender": sender,
        "content": content,
        "timestamp": "2026-04-24T18:00:00Z",
    }


class TestSelfEchoFilterBehavior:
    """Self-echo messages must never be written to the inbox."""

    LOCAL_IDENTITY = "SaharLobster"

    def test_self_echo_is_not_written_to_inbox(self, tmp_path):
        """A message where sender == MY_LOBSTER_NAME must not appear in the inbox."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox([_make_msg(self.LOCAL_IDENTITY, "my own echo")])

        assert routed == 0, "Self-echo must not be routed to inbox"
        inbox_files = list(inbox.glob("*.json"))
        assert inbox_files == [], "No inbox files must be created for self-echo"

    def test_other_sender_is_written_to_inbox(self, tmp_path):
        """A message from another Lobster must be routed to inbox."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox([_make_msg("AlbertLobster", "hello from albert")])

        assert routed == 1, "Message from another sender must be routed"
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) == 1

    def test_mixed_batch_routes_only_non_self(self, tmp_path):
        """In a batch of mixed messages, only non-self messages are routed."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        messages = [
            _make_msg(self.LOCAL_IDENTITY, "[OUTBOUND] PR review sent"),
            _make_msg("AlbertLobster", "got your review, thanks"),
            _make_msg(self.LOCAL_IDENTITY, "[OUTBOUND] health update"),
            _make_msg("CarolLobster", "checking in"),
        ]

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox(messages)

        assert routed == 2, "Only AlbertLobster and CarolLobster messages should be routed"
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) == 2
        senders = {json.loads(f.read_text())["from"] for f in inbox_files}
        assert senders == {"AlbertLobster", "CarolLobster"}

    def test_all_self_echoes_produces_zero_inbox_writes(self, tmp_path):
        """If every message in the batch is a self-echo, no inbox files are created."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        messages = [
            _make_msg(self.LOCAL_IDENTITY, "echo 1"),
            _make_msg(self.LOCAL_IDENTITY, "echo 2"),
            _make_msg(self.LOCAL_IDENTITY, "echo 3"),
        ]

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox(messages)

        assert routed == 0
        assert list(inbox.glob("*.json")) == []

    def test_empty_message_list_routes_nothing(self, tmp_path):
        """Empty input produces no inbox writes and no errors."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox([])

        assert routed == 0


class TestSelfEchoSkipLogging:
    """Skipped self-echo messages must be logged so we can audit the filter."""

    LOCAL_IDENTITY = "SaharLobster"

    def test_skipped_echoes_logged_to_jsonl(self, tmp_path):
        """When self-echoes are skipped, a log entry noting the count is written."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        log_file = tmp_path / "lobstertalk.jsonl"

        messages = [
            _make_msg(self.LOCAL_IDENTITY, "echo 1"),
            _make_msg(self.LOCAL_IDENTITY, "echo 2"),
        ]

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", log_file), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            mod.write_to_inbox(messages)

        assert log_file.exists(), "Log file must be created when echoes are skipped"
        lines = [l.strip() for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        last_entry = json.loads(lines[-1])
        assert "Skipped" in last_entry.get("content", ""), (
            "Log entry must mention skipped count"
        )
        assert "2" in last_entry["content"], "Log must mention the count (2 echoes)"

    def test_no_log_entry_when_no_echoes(self, tmp_path):
        """When no self-echoes are found, no extra log entry for skips is written."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        log_file = tmp_path / "lobstertalk.jsonl"

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", log_file), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            mod.write_to_inbox([_make_msg("AlbertLobster", "hi")])

        # If log file exists, no "Skipped" entry should be present
        if log_file.exists():
            for line in log_file.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    assert "Skipped" not in entry.get("content", ""), (
                        "No skip log entry when there are no self-echoes"
                    )


class TestSenderFieldFallback:
    """write_to_inbox handles messages that use 'from' instead of 'sender'."""

    LOCAL_IDENTITY = "SaharLobster"

    def test_from_field_used_when_sender_absent(self, tmp_path):
        """If 'sender' key is missing, 'from' is used to identify self-echoes."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        # Use 'from' field instead of 'sender'
        msg = {"from": self.LOCAL_IDENTITY, "content": "echo via from field",
               "timestamp": "2026-04-24T18:00:00Z"}

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox([msg])

        assert routed == 0, "Self-echo via 'from' field must also be filtered"

    def test_other_sender_via_from_field_passes_through(self, tmp_path):
        """'from' field with a different sender should be routed normally."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        msg = {"from": "AlbertLobster", "content": "hi via from field",
               "timestamp": "2026-04-24T18:00:00Z"}

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox([msg])

        assert routed == 1


class TestMyLobsterNameEnvFallback:
    """MY_LOBSTER_NAME reads from env vars in priority order."""

    def test_bot_talk_self_user_takes_precedence(self):
        """BOT_TALK_SELF_USER overrides other env vars when set."""
        mod = _load_watcher_module({
            "BOT_TALK_SELF_USER": "PrimaryLobster",
            "BOT_TALK_SENDER": "FallbackLobster",
            "LOBSTER_NAME": "TertiaryLobster",
        })
        assert mod.MY_LOBSTER_NAME == "PrimaryLobster"

    def test_bot_talk_sender_is_second_fallback(self):
        """BOT_TALK_SENDER is used when BOT_TALK_SELF_USER is not set."""
        env = {
            "BOT_TALK_SENDER": "SecondaryLobster",
            "LOBSTER_NAME": "TertiaryLobster",
        }
        # Ensure BOT_TALK_SELF_USER is absent
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("BOT_TALK_SELF_USER", None)
            mod = _load_watcher_module(env)
        assert mod.MY_LOBSTER_NAME == "SecondaryLobster"

    def test_lobster_name_is_third_fallback(self):
        """LOBSTER_NAME is used when both BOT_TALK_SELF_USER and BOT_TALK_SENDER are absent."""
        env = {"LOBSTER_NAME": "TertiaryLobster"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("BOT_TALK_SELF_USER", None)
            os.environ.pop("BOT_TALK_SENDER", None)
            mod = _load_watcher_module(env)
        assert mod.MY_LOBSTER_NAME == "TertiaryLobster"

    def test_saharlobster_is_default_when_no_env_set(self):
        """Falls back to 'SaharLobster' when no env vars are set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_TALK_SELF_USER", None)
            os.environ.pop("BOT_TALK_SENDER", None)
            os.environ.pop("LOBSTER_NAME", None)
            mod = _load_watcher_module({})
        assert mod.MY_LOBSTER_NAME == "SaharLobster"


class TestInboxMessageContent:
    """Routed messages contain the correct fields."""

    LOCAL_IDENTITY = "SaharLobster"

    def test_routed_message_has_correct_fields(self, tmp_path):
        """Inbox messages from other senders have correct direction and source fields."""
        mod = _load_watcher_module({"BOT_TALK_SELF_USER": self.LOCAL_IDENTITY})
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        with patch.object(mod, "INBOX_DIR", inbox), \
             patch.object(mod, "LOG_FILE", tmp_path / "lobstertalk.jsonl"), \
             patch.object(mod, "MY_LOBSTER_NAME", self.LOCAL_IDENTITY):
            routed = mod.write_to_inbox([_make_msg("AlbertLobster", "hello")])

        assert routed == 1
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) == 1
        msg = json.loads(inbox_files[0].read_text())
        assert msg["direction"] == "INBOUND"
        assert msg["source"] == "bot-talk"
        assert msg["from"] == "AlbertLobster"
        assert msg["text"] == "hello"
