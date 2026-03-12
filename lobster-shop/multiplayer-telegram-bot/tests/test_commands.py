"""
Tests for commands.py — unit tests for parsing and command handling.
"""

import json
import tempfile
from pathlib import Path

import pytest

from multiplayer_telegram_bot.commands import (
    CommandResult,
    handle_enable_group_bot,
    handle_unwhitelist,
    handle_whitelist,
    parse_enable_group_bot,
    parse_whitelist,
    parse_unwhitelist,
)
from multiplayer_telegram_bot.whitelist import load_whitelist


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROUP_ID = "-1001234567890"
USER_ID = 123456789
GROUP_ID_INT = -1001234567890


# ---------------------------------------------------------------------------
# parse_enable_group_bot (pure, no I/O)
# ---------------------------------------------------------------------------

class TestParseEnableGroupBot:
    def test_valid_group_id_returns_id(self):
        gid, error = parse_enable_group_bot("/enable-group-bot -1001234567890")
        assert gid == "-1001234567890"
        assert error == ""

    def test_valid_supergroup_id(self):
        gid, error = parse_enable_group_bot("/enable-group-bot -100999888777")
        assert gid == "-100999888777"
        assert error == ""

    def test_missing_argument_returns_error(self):
        gid, error = parse_enable_group_bot("/enable-group-bot")
        assert gid is None
        assert "Usage" in error

    def test_non_negative_group_id_returns_error(self):
        gid, error = parse_enable_group_bot("/enable-group-bot 12345")
        assert gid is None
        assert "negative" in error.lower()

    def test_non_numeric_group_id_returns_error(self):
        gid, error = parse_enable_group_bot("/enable-group-bot not-a-number")
        assert gid is None
        assert "invalid" in error.lower()

    def test_extra_args_ignored(self):
        gid, error = parse_enable_group_bot("/enable-group-bot -1001 My Group Name")
        assert gid == "-1001"
        assert error == ""


# ---------------------------------------------------------------------------
# parse_whitelist (pure, no I/O)
# ---------------------------------------------------------------------------

class TestParseWhitelist:
    def test_valid_args_return_user_and_group(self):
        result, error = parse_whitelist("/whitelist 123456789 -1001234567890")
        assert result == (123456789, "-1001234567890")
        assert error == ""

    def test_missing_both_args_returns_error(self):
        result, error = parse_whitelist("/whitelist")
        assert result is None
        assert "Usage" in error

    def test_missing_group_id_returns_error(self):
        result, error = parse_whitelist("/whitelist 123456789")
        assert result is None
        assert "Usage" in error

    def test_non_numeric_user_id_returns_error(self):
        result, error = parse_whitelist("/whitelist notanumber -1001234567890")
        assert result is None
        assert "invalid" in error.lower()

    def test_zero_user_id_returns_error(self):
        result, error = parse_whitelist("/whitelist 0 -1001234567890")
        assert result is None
        assert "positive" in error.lower()

    def test_negative_user_id_returns_error(self):
        result, error = parse_whitelist("/whitelist -100 -1001234567890")
        assert result is None
        assert "positive" in error.lower()

    def test_non_negative_group_id_returns_error(self):
        result, error = parse_whitelist("/whitelist 123 456")
        assert result is None
        assert "negative" in error.lower()


# ---------------------------------------------------------------------------
# handle_enable_group_bot (has I/O via whitelist path)
# ---------------------------------------------------------------------------

class TestHandleEnableGroupBot:
    def test_enables_new_group(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        result = handle_enable_group_bot(
            f"/enable-group-bot {GROUP_ID}",
            whitelist_path=whitelist_path,
        )
        assert result.success is True
        assert GROUP_ID in result.reply

    def test_group_written_to_disk(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(
            f"/enable-group-bot {GROUP_ID}",
            whitelist_path=whitelist_path,
        )
        store = load_whitelist(whitelist_path)
        assert GROUP_ID in store["groups"]
        assert store["groups"][GROUP_ID]["enabled"] is True

    def test_uses_name_from_command_text(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(
            f"/enable-group-bot {GROUP_ID} Test Team",
            whitelist_path=whitelist_path,
        )
        store = load_whitelist(whitelist_path)
        assert store["groups"][GROUP_ID]["name"] == "Test Team"

    def test_uses_explicit_group_name_param(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(
            f"/enable-group-bot {GROUP_ID}",
            group_name="Custom Name",
            whitelist_path=whitelist_path,
        )
        store = load_whitelist(whitelist_path)
        assert store["groups"][GROUP_ID]["name"] == "Custom Name"

    def test_invalid_group_id_returns_failure(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        result = handle_enable_group_bot(
            "/enable-group-bot 12345",
            whitelist_path=whitelist_path,
        )
        assert result.success is False

    def test_updated_store_returned(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        result = handle_enable_group_bot(
            f"/enable-group-bot {GROUP_ID}",
            whitelist_path=whitelist_path,
        )
        assert result.updated_store is not None
        assert GROUP_ID in result.updated_store["groups"]

    def test_preserves_existing_users_when_re_enabling(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        # First enable and add a user
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID}", whitelist_path=whitelist_path)
        handle_whitelist(f"/whitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        # Re-enable the group (e.g. to rename it)
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID} New Name", whitelist_path=whitelist_path)
        store = load_whitelist(whitelist_path)
        assert USER_ID in store["groups"][GROUP_ID]["allowed_user_ids"]


# ---------------------------------------------------------------------------
# handle_whitelist (has I/O)
# ---------------------------------------------------------------------------

class TestHandleWhitelist:
    def test_adds_user_to_existing_enabled_group(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        # First enable the group
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID}", whitelist_path=whitelist_path)
        # Then whitelist a user
        result = handle_whitelist(f"/whitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        assert result.success is True

    def test_user_appears_in_whitelist_after_command(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID}", whitelist_path=whitelist_path)
        handle_whitelist(f"/whitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        store = load_whitelist(whitelist_path)
        assert USER_ID in store["groups"][GROUP_ID]["allowed_user_ids"]

    def test_warns_when_group_not_enabled(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        result = handle_whitelist(f"/whitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        assert result.success is True
        assert "not yet enabled" in result.reply.lower() or "enable" in result.reply.lower()

    def test_invalid_args_return_failure(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        result = handle_whitelist("/whitelist", whitelist_path=whitelist_path)
        assert result.success is False

    def test_reply_contains_user_id(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID}", whitelist_path=whitelist_path)
        result = handle_whitelist(f"/whitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        assert str(USER_ID) in result.reply

    def test_reply_contains_group_id(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID}", whitelist_path=whitelist_path)
        result = handle_whitelist(f"/whitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        assert GROUP_ID in result.reply


# ---------------------------------------------------------------------------
# handle_unwhitelist (has I/O)
# ---------------------------------------------------------------------------

class TestHandleUnwhitelist:
    def test_removes_user_from_whitelist(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID}", whitelist_path=whitelist_path)
        handle_whitelist(f"/whitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        # Remove the user
        result = handle_unwhitelist(f"/unwhitelist {USER_ID} {GROUP_ID}", whitelist_path=whitelist_path)
        assert result.success is True
        store = load_whitelist(whitelist_path)
        assert USER_ID not in store["groups"][GROUP_ID]["allowed_user_ids"]

    def test_removing_nonexistent_user_returns_success(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        handle_enable_group_bot(f"/enable-group-bot {GROUP_ID}", whitelist_path=whitelist_path)
        result = handle_unwhitelist(f"/unwhitelist 999999 {GROUP_ID}", whitelist_path=whitelist_path)
        assert result.success is True

    def test_invalid_args_return_failure(self, tmp_path):
        whitelist_path = tmp_path / "whitelist.json"
        result = handle_unwhitelist("/unwhitelist", whitelist_path=whitelist_path)
        assert result.success is False
