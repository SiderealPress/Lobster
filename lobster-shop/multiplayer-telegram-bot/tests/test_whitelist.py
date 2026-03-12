"""
Tests for whitelist.py — pure function unit tests, no I/O required.
"""

import json
import tempfile
from pathlib import Path

import pytest

from multiplayer_telegram_bot.whitelist import (
    WhitelistStore,
    _empty_store,
    add_allowed_user,
    enable_group,
    is_group_enabled,
    is_user_allowed,
    load_whitelist,
    remove_allowed_user,
    save_whitelist,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_store(groups: dict | None = None) -> WhitelistStore:
    return {"groups": groups or {}}


def make_group(name: str = "Test Group", enabled: bool = True, user_ids: list[int] | None = None) -> dict:
    return {
        "name": name,
        "enabled": enabled,
        "allowed_user_ids": user_ids or [],
    }


# ---------------------------------------------------------------------------
# is_group_enabled
# ---------------------------------------------------------------------------

def test_group_enabled_when_present_and_true():
    store = make_store({"-1001": make_group(enabled=True)})
    assert is_group_enabled("-1001", store) is True


def test_group_not_enabled_when_flag_false():
    store = make_store({"-1001": make_group(enabled=False)})
    assert is_group_enabled("-1001", store) is False


def test_group_not_enabled_when_missing():
    store = make_store({})
    assert is_group_enabled("-1001", store) is False


def test_group_enabled_accepts_int_chat_id():
    store = make_store({"-1001234567890": make_group(enabled=True)})
    assert is_group_enabled(-1001234567890, store) is True


# ---------------------------------------------------------------------------
# is_user_allowed
# ---------------------------------------------------------------------------

def test_user_allowed_when_in_list():
    store = make_store({"-1001": make_group(user_ids=[111, 222])})
    assert is_user_allowed(111, "-1001", store) is True


def test_user_not_allowed_when_not_in_list():
    store = make_store({"-1001": make_group(user_ids=[111])})
    assert is_user_allowed(999, "-1001", store) is False


def test_user_not_allowed_when_group_missing():
    store = make_store({})
    assert is_user_allowed(111, "-1001", store) is False


# ---------------------------------------------------------------------------
# enable_group
# ---------------------------------------------------------------------------

def test_enable_group_creates_entry():
    store = make_store({})
    result = enable_group("-1001", "My Group", store)
    assert result["groups"]["-1001"]["enabled"] is True
    assert result["groups"]["-1001"]["name"] == "My Group"
    assert result["groups"]["-1001"]["allowed_user_ids"] == []


def test_enable_group_preserves_existing_users():
    store = make_store({"-1001": make_group(user_ids=[111], enabled=False)})
    result = enable_group("-1001", "Renamed", store)
    assert result["groups"]["-1001"]["enabled"] is True
    assert result["groups"]["-1001"]["allowed_user_ids"] == [111]


def test_enable_group_does_not_mutate_original():
    store = make_store({})
    _ = enable_group("-1001", "My Group", store)
    assert "-1001" not in store["groups"]


# ---------------------------------------------------------------------------
# add_allowed_user / remove_allowed_user
# ---------------------------------------------------------------------------

def test_add_user_to_existing_group():
    store = make_store({"-1001": make_group(user_ids=[111])})
    result = add_allowed_user(222, "-1001", store)
    assert 222 in result["groups"]["-1001"]["allowed_user_ids"]
    assert 111 in result["groups"]["-1001"]["allowed_user_ids"]


def test_add_user_creates_group_if_missing():
    store = make_store({})
    result = add_allowed_user(111, "-1001", store)
    assert result["groups"]["-1001"]["allowed_user_ids"] == [111]


def test_add_user_idempotent():
    store = make_store({"-1001": make_group(user_ids=[111])})
    result = add_allowed_user(111, "-1001", store)
    assert result["groups"]["-1001"]["allowed_user_ids"].count(111) == 1


def test_remove_user_from_group():
    store = make_store({"-1001": make_group(user_ids=[111, 222])})
    result = remove_allowed_user(111, "-1001", store)
    assert 111 not in result["groups"]["-1001"]["allowed_user_ids"]
    assert 222 in result["groups"]["-1001"]["allowed_user_ids"]


def test_remove_user_from_missing_group_returns_unchanged():
    store = make_store({})
    result = remove_allowed_user(111, "-1001", store)
    assert result == store


# ---------------------------------------------------------------------------
# I/O: load_whitelist / save_whitelist round-trip
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_empty():
    result = load_whitelist(Path("/nonexistent/path/whitelist.json"))
    assert result == {"groups": {}}


def test_round_trip(tmp_path):
    store: WhitelistStore = {
        "groups": {
            "-1001": {
                "name": "Test Group",
                "enabled": True,
                "allowed_user_ids": [111, 222],
            }
        }
    }
    path = tmp_path / "whitelist.json"
    save_whitelist(store, path)
    loaded = load_whitelist(path)
    assert loaded == store


def test_load_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "whitelist.json"
    path.write_text("not json")
    result = load_whitelist(path)
    assert result == {"groups": {}}
