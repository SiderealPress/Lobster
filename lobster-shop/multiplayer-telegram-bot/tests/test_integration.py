"""
tests/test_integration.py

End-to-end integration test that exercises the full message flow:

1. Initialize empty whitelist
2. Enable a test group
3. Add a test user to whitelist
4. Simulate group message from allowed user -> written to lobster-group inbox
5. Simulate group message from disallowed user -> dropped, registration DM triggered
6. Simulate message from unknown group -> silently dropped

All I/O is isolated:
- The whitelist uses a temporary file (via tmp_path fixture)
- The "send DM" side effect uses a mock callable
- No real filesystem or Telegram API calls are made
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from multiplayer_telegram_bot.commands import (
    handle_enable_group_bot,
    handle_whitelist,
)
from multiplayer_telegram_bot.gating import (
    GatingAction,
    gate_message,
    should_allow,
    should_drop,
    should_register,
)
from multiplayer_telegram_bot.registration import (
    handle_registration_flow,
)
from multiplayer_telegram_bot.router import (
    build_inbox_message,
    classify_message,
    is_group_message,
)
from multiplayer_telegram_bot.whitelist import (
    load_whitelist,
    save_whitelist,
)


# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

TEST_GROUP_ID = -1009876543210
TEST_GROUP_ID_STR = str(TEST_GROUP_ID)
TEST_GROUP_NAME = "Integration Test Group"

ALLOWED_USER_ID = 111111111
DISALLOWED_USER_ID = 222222222
UNKNOWN_GROUP_ID = -1000000000000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_whitelist_path(tmp_path: Path) -> Path:
    """Return a writable path for a temporary whitelist file."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    return config_dir / "group-whitelist.json"


def _simulate_telegram_message(
    text: str,
    chat_id: int,
    user_id: int,
    chat_type: str = "supergroup",
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    """Return a dict representing an incoming Telegram message update."""
    return {
        "text": text,
        "chat_id": chat_id,
        "user_id": user_id,
        "chat_type": chat_type,
        "username": username,
        "first_name": first_name,
    }


# ---------------------------------------------------------------------------
# Step 1: Initialize empty whitelist
# ---------------------------------------------------------------------------

class TestStep1EmptyWhitelist:
    """An empty whitelist rejects all group messages."""

    def test_empty_whitelist_loaded(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        store = load_whitelist(wl_path)
        assert store == {"groups": {}}, "Empty whitelist should have no groups"

    def test_group_not_enabled_in_empty_whitelist(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        store = load_whitelist(wl_path)
        result = gate_message(TEST_GROUP_ID, ALLOWED_USER_ID, store)
        assert result.action == GatingAction.DROP_SILENT
        assert should_drop(result)

    def test_unknown_user_dropped_from_empty_whitelist(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        store = load_whitelist(wl_path)
        result = gate_message(TEST_GROUP_ID, DISALLOWED_USER_ID, store)
        assert result.action == GatingAction.DROP_SILENT


# ---------------------------------------------------------------------------
# Step 2: Enable a test group
# ---------------------------------------------------------------------------

class TestStep2EnableGroup:
    """The /enable-group-bot command should add the group to the whitelist."""

    def test_enable_group_bot_command_succeeds(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        cmd_text = f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}"
        result = handle_enable_group_bot(cmd_text, whitelist_path=wl_path)
        assert result.success, f"Expected success, got: {result.reply}"
        assert result.updated_store is not None

    def test_group_enabled_in_whitelist_after_command(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        cmd_text = f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}"
        handle_enable_group_bot(cmd_text, whitelist_path=wl_path)

        store = load_whitelist(wl_path)
        assert TEST_GROUP_ID_STR in store["groups"], "Group should be in whitelist"
        group = store["groups"][TEST_GROUP_ID_STR]
        assert group["enabled"] is True, "Group should be enabled"

    def test_enabled_group_with_no_users_triggers_registration(self, tmp_path: Path) -> None:
        """Group enabled but no users — any sender triggers registration DM."""
        wl_path = _make_whitelist_path(tmp_path)
        cmd_text = f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}"
        handle_enable_group_bot(cmd_text, whitelist_path=wl_path)

        store = load_whitelist(wl_path)
        result = gate_message(TEST_GROUP_ID, ALLOWED_USER_ID, store)
        assert result.action == GatingAction.SEND_REGISTRATION_DM
        assert should_register(result)

    def test_reply_message_contains_group_name(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        cmd_text = f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}"
        result = handle_enable_group_bot(cmd_text, whitelist_path=wl_path)
        assert TEST_GROUP_NAME in result.reply


# ---------------------------------------------------------------------------
# Step 3: Add a test user to whitelist
# ---------------------------------------------------------------------------

class TestStep3AddUserToWhitelist:
    """The /whitelist command should add a user to the group's allowed list."""

    def _setup_enabled_group(self, tmp_path: Path) -> Path:
        wl_path = _make_whitelist_path(tmp_path)
        handle_enable_group_bot(
            f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}",
            whitelist_path=wl_path,
        )
        return wl_path

    def test_whitelist_command_succeeds(self, tmp_path: Path) -> None:
        wl_path = self._setup_enabled_group(tmp_path)
        cmd_text = f"/whitelist {ALLOWED_USER_ID} {TEST_GROUP_ID}"
        result = handle_whitelist(cmd_text, whitelist_path=wl_path)
        assert result.success, f"Expected success, got: {result.reply}"

    def test_user_appears_in_allowed_list(self, tmp_path: Path) -> None:
        wl_path = self._setup_enabled_group(tmp_path)
        handle_whitelist(f"/whitelist {ALLOWED_USER_ID} {TEST_GROUP_ID}", whitelist_path=wl_path)

        store = load_whitelist(wl_path)
        group = store["groups"][TEST_GROUP_ID_STR]
        assert ALLOWED_USER_ID in group["allowed_user_ids"]

    def test_disallowed_user_not_in_list(self, tmp_path: Path) -> None:
        wl_path = self._setup_enabled_group(tmp_path)
        handle_whitelist(f"/whitelist {ALLOWED_USER_ID} {TEST_GROUP_ID}", whitelist_path=wl_path)

        store = load_whitelist(wl_path)
        group = store["groups"][TEST_GROUP_ID_STR]
        assert DISALLOWED_USER_ID not in group["allowed_user_ids"]

    def test_whitelist_file_persisted_to_disk(self, tmp_path: Path) -> None:
        """Whitelist changes survive a reload from disk."""
        wl_path = self._setup_enabled_group(tmp_path)
        handle_whitelist(f"/whitelist {ALLOWED_USER_ID} {TEST_GROUP_ID}", whitelist_path=wl_path)

        # Re-load from disk and verify
        reloaded = load_whitelist(wl_path)
        group = reloaded["groups"][TEST_GROUP_ID_STR]
        assert ALLOWED_USER_ID in group["allowed_user_ids"]


# ---------------------------------------------------------------------------
# Step 4: Allowed user message -> written to lobster-group inbox
# ---------------------------------------------------------------------------

class TestStep4AllowedUserMessage:
    """Group message from a whitelisted user should be tagged as lobster-group."""

    def _setup_full_whitelist(self, tmp_path: Path) -> Path:
        wl_path = _make_whitelist_path(tmp_path)
        handle_enable_group_bot(
            f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}",
            whitelist_path=wl_path,
        )
        handle_whitelist(
            f"/whitelist {ALLOWED_USER_ID} {TEST_GROUP_ID}",
            whitelist_path=wl_path,
        )
        return wl_path

    def test_allowed_user_gating_allows(self, tmp_path: Path) -> None:
        wl_path = self._setup_full_whitelist(tmp_path)
        store = load_whitelist(wl_path)
        result = gate_message(TEST_GROUP_ID, ALLOWED_USER_ID, store)
        assert result.action == GatingAction.ALLOW
        assert should_allow(result)

    def test_inbox_message_has_correct_source(self, tmp_path: Path) -> None:
        """Inbox message dict should have source='lobster-group'."""
        msg = _simulate_telegram_message(
            text="Hello from the group!",
            chat_id=TEST_GROUP_ID,
            user_id=ALLOWED_USER_ID,
            chat_type="supergroup",
            username="testuser",
            first_name="Test",
        )
        inbox_msg = build_inbox_message(
            text=msg["text"],
            chat_id=msg["chat_id"],
            user_id=msg["user_id"],
            chat_type=msg["chat_type"],
            username=msg["username"],
            first_name=msg["first_name"],
        )
        assert inbox_msg["source"] == "lobster-group"
        assert inbox_msg["chat_id"] == TEST_GROUP_ID
        assert inbox_msg["user_id"] == ALLOWED_USER_ID
        assert inbox_msg["text"] == "Hello from the group!"

    def test_classify_message_marks_group_as_requiring_gating(self) -> None:
        classification = classify_message(
            chat_id=TEST_GROUP_ID,
            user_id=ALLOWED_USER_ID,
            chat_type="supergroup",
            text="Test message",
        )
        assert classification["is_group"] is True
        assert classification["requires_gating"] is True
        assert classification["source"] == "lobster-group"

    def test_full_pipeline_allowed_user(self, tmp_path: Path) -> None:
        """Full pipeline: detect group -> gate -> build inbox message."""
        wl_path = self._setup_full_whitelist(tmp_path)
        store = load_whitelist(wl_path)

        msg = _simulate_telegram_message(
            text="Meeting recap: we decided to proceed with Q3 plan.",
            chat_id=TEST_GROUP_ID,
            user_id=ALLOWED_USER_ID,
            chat_type="supergroup",
        )

        # Step 1: Is this a group message?
        assert is_group_message(msg["chat_type"]) is True

        # Step 2: Gate it
        gate_result = gate_message(msg["chat_id"], msg["user_id"], store)
        assert gate_result.action == GatingAction.ALLOW

        # Step 3: Build inbox message
        inbox_msg = build_inbox_message(
            text=msg["text"],
            chat_id=msg["chat_id"],
            user_id=msg["user_id"],
            chat_type=msg["chat_type"],
        )
        assert inbox_msg["source"] == "lobster-group"
        assert inbox_msg["text"] == msg["text"]


# ---------------------------------------------------------------------------
# Step 5: Disallowed user -> dropped, registration DM triggered
# ---------------------------------------------------------------------------

class TestStep5DisallowedUserRegistration:
    """Group-enabled but non-whitelisted user triggers a registration DM."""

    def _setup_group_no_user(self, tmp_path: Path) -> Path:
        """Enable group but add only ALLOWED_USER_ID, not DISALLOWED_USER_ID."""
        wl_path = _make_whitelist_path(tmp_path)
        handle_enable_group_bot(
            f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}",
            whitelist_path=wl_path,
        )
        handle_whitelist(
            f"/whitelist {ALLOWED_USER_ID} {TEST_GROUP_ID}",
            whitelist_path=wl_path,
        )
        return wl_path

    def test_disallowed_user_triggers_registration(self, tmp_path: Path) -> None:
        wl_path = self._setup_group_no_user(tmp_path)
        store = load_whitelist(wl_path)
        result = gate_message(TEST_GROUP_ID, DISALLOWED_USER_ID, store)
        assert result.action == GatingAction.SEND_REGISTRATION_DM
        assert should_register(result)

    def test_disallowed_user_message_not_allowed(self, tmp_path: Path) -> None:
        wl_path = self._setup_group_no_user(tmp_path)
        store = load_whitelist(wl_path)
        result = gate_message(TEST_GROUP_ID, DISALLOWED_USER_ID, store)
        assert not should_allow(result)

    def test_registration_dm_sent_to_correct_user(self, tmp_path: Path) -> None:
        """handle_registration_flow calls send_fn with the right user ID."""
        wl_path = self._setup_group_no_user(tmp_path)

        sent_to: list[int] = []

        def mock_send_fn(user_id: int, text: str) -> bool:
            sent_to.append(user_id)
            return True

        result = handle_registration_flow(
            user_id=DISALLOWED_USER_ID,
            group_chat_id=TEST_GROUP_ID,
            send_fn=mock_send_fn,
        )
        assert result.success is True
        assert DISALLOWED_USER_ID in sent_to

    def test_registration_dm_text_contains_register_instruction(self, tmp_path: Path) -> None:
        received_texts: list[str] = []

        def mock_send_fn(user_id: int, text: str) -> bool:
            received_texts.append(text)
            return True

        handle_registration_flow(
            user_id=DISALLOWED_USER_ID,
            group_chat_id=TEST_GROUP_ID,
            send_fn=mock_send_fn,
        )
        assert received_texts, "send_fn should have been called"
        assert "register" in received_texts[0].lower()

    def test_failed_dm_send_captured_gracefully(self) -> None:
        """If send_fn raises, the error is captured — not re-raised."""
        def failing_send_fn(user_id: int, text: str) -> bool:
            raise ConnectionError("Telegram API unavailable")

        result = handle_registration_flow(
            user_id=DISALLOWED_USER_ID,
            group_chat_id=TEST_GROUP_ID,
            send_fn=failing_send_fn,
        )
        assert result.success is False
        assert result.error is not None
        assert "Telegram API unavailable" in result.error

    def test_full_pipeline_disallowed_user(self, tmp_path: Path) -> None:
        """Full pipeline: detect group -> gate -> registration DM (not inbox)."""
        wl_path = self._setup_group_no_user(tmp_path)
        store = load_whitelist(wl_path)

        msg = _simulate_telegram_message(
            text="Hey can I get access?",
            chat_id=TEST_GROUP_ID,
            user_id=DISALLOWED_USER_ID,
            chat_type="group",
        )

        # Step 1: Is this a group message?
        assert is_group_message(msg["chat_type"]) is True

        # Step 2: Gate it
        gate_result = gate_message(msg["chat_id"], msg["user_id"], store)
        assert gate_result.action == GatingAction.SEND_REGISTRATION_DM

        # Step 3: Registration DM (mock send)
        dm_calls: list[tuple[int, str]] = []

        def capture_send(user_id: int, text: str) -> bool:
            dm_calls.append((user_id, text))
            return True

        reg_result = handle_registration_flow(
            user_id=gate_result.user_id,
            group_chat_id=gate_result.chat_id,
            send_fn=capture_send,
        )
        assert reg_result.success is True
        assert dm_calls[0][0] == DISALLOWED_USER_ID


# ---------------------------------------------------------------------------
# Step 6: Unknown group -> silently dropped
# ---------------------------------------------------------------------------

class TestStep6UnknownGroupDropped:
    """Messages from groups not in the whitelist are silently discarded."""

    def test_unknown_group_silently_dropped(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        # Only enable TEST_GROUP_ID
        handle_enable_group_bot(
            f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}",
            whitelist_path=wl_path,
        )
        store = load_whitelist(wl_path)

        # Message from a completely different group
        result = gate_message(UNKNOWN_GROUP_ID, ALLOWED_USER_ID, store)
        assert result.action == GatingAction.DROP_SILENT
        assert should_drop(result)

    def test_disabled_group_silently_dropped(self, tmp_path: Path) -> None:
        """A group that was enabled then disabled is also dropped."""
        from multiplayer_telegram_bot.whitelist import WhitelistStore

        # Build a store with an explicitly disabled group
        disabled_store: WhitelistStore = {
            "groups": {
                TEST_GROUP_ID_STR: {
                    "name": TEST_GROUP_NAME,
                    "enabled": False,  # explicitly disabled
                    "allowed_user_ids": [ALLOWED_USER_ID],
                }
            }
        }

        result = gate_message(TEST_GROUP_ID, ALLOWED_USER_ID, disabled_store)
        assert result.action == GatingAction.DROP_SILENT

    def test_channel_messages_not_treated_as_group(self) -> None:
        """Telegram channels should not route to lobster-group."""
        assert is_group_message("channel") is False

    def test_private_messages_not_treated_as_group(self) -> None:
        assert is_group_message("private") is False

    def test_full_pipeline_unknown_group_no_side_effects(self, tmp_path: Path) -> None:
        """Unknown group: detect group -> gate -> no DM, no inbox write."""
        wl_path = _make_whitelist_path(tmp_path)
        store = load_whitelist(wl_path)  # empty

        msg = _simulate_telegram_message(
            text="Unsolicited message from random group",
            chat_id=UNKNOWN_GROUP_ID,
            user_id=ALLOWED_USER_ID,
            chat_type="supergroup",
        )

        # Step 1: Is this a group message? (yes — but that's fine)
        assert is_group_message(msg["chat_type"]) is True

        # Step 2: Gate it — should drop
        gate_result = gate_message(msg["chat_id"], msg["user_id"], store)
        assert gate_result.action == GatingAction.DROP_SILENT

        # Step 3: No DM sent, no inbox message built
        # (caller checks gate_result.action and does nothing for DROP_SILENT)
        assert should_drop(gate_result)
        assert not should_allow(gate_result)
        assert not should_register(gate_result)


# ---------------------------------------------------------------------------
# Full end-to-end walkthrough (smoke test)
# ---------------------------------------------------------------------------

class TestFullEndToEndFlow:
    """A single test that walks through all 6 steps in sequence."""

    def test_full_e2e_flow(self, tmp_path: Path) -> None:
        wl_path = _make_whitelist_path(tmp_path)
        dm_log: list[tuple[int, str]] = []

        def mock_send_dm(user_id: int, text: str) -> bool:
            dm_log.append((user_id, text))
            return True

        # Step 1: Empty whitelist
        store = load_whitelist(wl_path)
        assert store == {"groups": {}}

        # Step 2: Enable the group
        enable_result = handle_enable_group_bot(
            f"/enable-group-bot {TEST_GROUP_ID} {TEST_GROUP_NAME}",
            whitelist_path=wl_path,
        )
        assert enable_result.success

        # Step 3: Add allowed user
        whitelist_result = handle_whitelist(
            f"/whitelist {ALLOWED_USER_ID} {TEST_GROUP_ID}",
            whitelist_path=wl_path,
        )
        assert whitelist_result.success

        # Reload store from disk
        store = load_whitelist(wl_path)

        # Step 4: Allowed user message -> ALLOW
        gate_ok = gate_message(TEST_GROUP_ID, ALLOWED_USER_ID, store)
        assert gate_ok.action == GatingAction.ALLOW
        inbox_msg = build_inbox_message(
            text="Hello from allowed user",
            chat_id=TEST_GROUP_ID,
            user_id=ALLOWED_USER_ID,
            chat_type="supergroup",
        )
        assert inbox_msg["source"] == "lobster-group"

        # Step 5: Disallowed user -> SEND_REGISTRATION_DM
        gate_bad = gate_message(TEST_GROUP_ID, DISALLOWED_USER_ID, store)
        assert gate_bad.action == GatingAction.SEND_REGISTRATION_DM
        reg = handle_registration_flow(
            user_id=DISALLOWED_USER_ID,
            group_chat_id=TEST_GROUP_ID,
            send_fn=mock_send_dm,
        )
        assert reg.success
        assert any(uid == DISALLOWED_USER_ID for uid, _ in dm_log)

        # Step 6: Unknown group -> DROP_SILENT
        gate_unknown = gate_message(UNKNOWN_GROUP_ID, ALLOWED_USER_ID, store)
        assert gate_unknown.action == GatingAction.DROP_SILENT


# ---------------------------------------------------------------------------
# Phase 5: Bot join → whitelist → message gate → lobster-group route
# ---------------------------------------------------------------------------


# Simulate the group that is already configured on the live system.
LIVE_GROUP_ID = -5033634362
LIVE_GROUP_ID_STR = str(LIVE_GROUP_ID)
LIVE_USER_ID_ADMIN = 6645894734   # Primary admin user
LIVE_USER_ID_MEMBER = 5717728951  # Secondary group member
LIVE_GROUP_NAME = "Group"


class TestPhase5BotJoinToRoute:
    """End-to-end flow: bot join auto-whitelist → message gate → lobster-group source.

    This test exercises the full pipeline introduced across Phases 1–5:
      1. Bot added to group by an ALLOWED_USER → auto-whitelist fires
      2. Whitelisted user sends a message → gate returns ALLOW
      3. inbox message dict has source="lobster-group"
      4. Non-whitelisted user's message → SEND_REGISTRATION_DM (not ALLOW)
      5. Unknown group → DROP_SILENT
    """

    def _auto_whitelist(
        self,
        tmp_path: Path,
        chat_id: int,
        chat_name: str,
        allowed_user_ids: list[int],
    ) -> Path:
        """Simulate the auto-whitelist logic from handle_my_chat_member.

        When the bot is added to a group by an ALLOWED_USER, the bot calls
        enable_group(...) then add_allowed_user(...) for every user in
        ALLOWED_USERS, then saves. This helper replicates that sequence.
        """
        from multiplayer_telegram_bot.whitelist import enable_group, add_allowed_user, save_whitelist

        wl_path = _make_whitelist_path(tmp_path)
        store = load_whitelist(wl_path)  # starts empty

        # Replicate handle_my_chat_member auto-whitelist
        store = enable_group(chat_id, chat_name, store)
        for uid in allowed_user_ids:
            store = add_allowed_user(uid, chat_id, store)
        save_whitelist(store, wl_path)
        return wl_path

    def test_bot_join_creates_enabled_group(self, tmp_path: Path) -> None:
        """After bot-join auto-whitelist, group is enabled in the store."""
        wl_path = self._auto_whitelist(
            tmp_path, LIVE_GROUP_ID, LIVE_GROUP_NAME,
            [LIVE_USER_ID_ADMIN, LIVE_USER_ID_MEMBER],
        )
        store = load_whitelist(wl_path)
        assert LIVE_GROUP_ID_STR in store["groups"]
        group = store["groups"][LIVE_GROUP_ID_STR]
        assert group["enabled"] is True

    def test_bot_join_seeds_both_allowed_users(self, tmp_path: Path) -> None:
        """Both ALLOWED_USERS are present in allowed_user_ids after auto-whitelist."""
        wl_path = self._auto_whitelist(
            tmp_path, LIVE_GROUP_ID, LIVE_GROUP_NAME,
            [LIVE_USER_ID_ADMIN, LIVE_USER_ID_MEMBER],
        )
        store = load_whitelist(wl_path)
        group = store["groups"][LIVE_GROUP_ID_STR]
        assert LIVE_USER_ID_ADMIN in group["allowed_user_ids"]
        assert LIVE_USER_ID_MEMBER in group["allowed_user_ids"]

    def test_whitelisted_user_message_gates_allow(self, tmp_path: Path) -> None:
        """After auto-whitelist, whitelisted user gets ALLOW from gate_message."""
        wl_path = self._auto_whitelist(
            tmp_path, LIVE_GROUP_ID, LIVE_GROUP_NAME,
            [LIVE_USER_ID_ADMIN, LIVE_USER_ID_MEMBER],
        )
        store = load_whitelist(wl_path)
        result = gate_message(LIVE_GROUP_ID, LIVE_USER_ID_ADMIN, store)
        assert result.action == GatingAction.ALLOW
        assert should_allow(result)

    def test_allowed_user_message_has_lobster_group_source(self, tmp_path: Path) -> None:
        """build_inbox_message returns source=lobster-group for a group message."""
        inbox_msg = build_inbox_message(
            text="Hey team, any updates?",
            chat_id=LIVE_GROUP_ID,
            user_id=LIVE_USER_ID_ADMIN,
            chat_type="supergroup",
            username="admin_user",
            first_name="Admin",
        )
        assert inbox_msg["source"] == "lobster-group"
        assert inbox_msg["chat_id"] == LIVE_GROUP_ID

    def test_non_whitelisted_user_triggers_registration(self, tmp_path: Path) -> None:
        """A user not in allowed_user_ids triggers SEND_REGISTRATION_DM, not ALLOW."""
        wl_path = self._auto_whitelist(
            tmp_path, LIVE_GROUP_ID, LIVE_GROUP_NAME,
            [LIVE_USER_ID_ADMIN],  # LIVE_USER_ID_MEMBER not included
        )
        store = load_whitelist(wl_path)
        result = gate_message(LIVE_GROUP_ID, LIVE_USER_ID_MEMBER, store)
        assert result.action == GatingAction.SEND_REGISTRATION_DM
        assert should_register(result)

    def test_unknown_group_silently_dropped(self, tmp_path: Path) -> None:
        """A group not in the whitelist yields DROP_SILENT for any user."""
        wl_path = self._auto_whitelist(
            tmp_path, LIVE_GROUP_ID, LIVE_GROUP_NAME,
            [LIVE_USER_ID_ADMIN, LIVE_USER_ID_MEMBER],
        )
        store = load_whitelist(wl_path)
        other_group = -9999999999
        result = gate_message(other_group, LIVE_USER_ID_ADMIN, store)
        assert result.action == GatingAction.DROP_SILENT
        assert should_drop(result)

    def test_full_bot_join_to_lobster_group_pipeline(self, tmp_path: Path) -> None:
        """Complete Phase 5 smoke test: join → gate → inbox source tag.

        Simulates the full sequence that would happen in production:
        1. Bot is added to LIVE_GROUP_ID by the admin user (ALLOWED_USER)
        2. Auto-whitelist seeds the store with admin + member
        3. Admin sends a message → ALLOW → inbox msg has source=lobster-group
        4. Unknown user sends a message → registration DM path
        5. Message from an unknown group → silent drop
        """
        dm_log: list[tuple[int, str]] = []

        def mock_send_dm(user_id: int, text: str) -> bool:
            dm_log.append((user_id, text))
            return True

        # Step 1: Bot join auto-whitelist (admin user added the bot)
        wl_path = self._auto_whitelist(
            tmp_path, LIVE_GROUP_ID, LIVE_GROUP_NAME,
            [LIVE_USER_ID_ADMIN, LIVE_USER_ID_MEMBER],
        )

        # Step 2: Load the store from disk (as the bot would on each message)
        store = load_whitelist(wl_path)

        # Step 3: Admin sends a text message in the group
        gate_admin = gate_message(LIVE_GROUP_ID, LIVE_USER_ID_ADMIN, store)
        assert gate_admin.action == GatingAction.ALLOW

        inbox_msg = build_inbox_message(
            text="What are we doing this weekend?",
            chat_id=LIVE_GROUP_ID,
            user_id=LIVE_USER_ID_ADMIN,
            chat_type="supergroup",
            username="admin_user",
            first_name="Admin",
        )
        assert inbox_msg["source"] == "lobster-group"
        assert inbox_msg["text"] == "What are we doing this weekend?"

        # Step 4: Stranger sends a message (should trigger registration DM)
        stranger_id = 777777777
        gate_stranger = gate_message(LIVE_GROUP_ID, stranger_id, store)
        assert gate_stranger.action == GatingAction.SEND_REGISTRATION_DM
        reg = handle_registration_flow(
            user_id=stranger_id,
            group_chat_id=LIVE_GROUP_ID,
            send_fn=mock_send_dm,
        )
        assert reg.success
        assert any(uid == stranger_id for uid, _ in dm_log)

        # Step 5: Message from a different group → silent drop
        other_group = -1001234567890
        gate_other = gate_message(other_group, LIVE_USER_ID_ADMIN, store)
        assert gate_other.action == GatingAction.DROP_SILENT
