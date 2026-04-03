"""Tests for user_permissions module — pure functions and UserPermissions class."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.user_permissions import (
    UserPermissions,
    check_can_address,
    check_is_admin,
    parse_users_config,
    _BUILTIN_DEFAULTS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config() -> dict:
    return {
        "defaults": {
            "can_address_lobster": False,
            "is_admin": False,
        },
        "users": [
            {
                "id": "U001",
                "name": "alice",
                "can_address_lobster": True,
                "is_admin": True,
            },
            {
                "id": "U002",
                "name": "bob",
                "can_address_lobster": True,
                "is_admin": False,
            },
            {
                "id": "U003",
                "name": "eve",
                "can_address_lobster": False,
            },
        ],
    }


@pytest.fixture
def wildcard_config() -> dict:
    return {
        "defaults": {"can_address_lobster": False},
        "users": [
            {"id": "*"},
            {"id": "U001", "name": "alice", "is_admin": True},
        ],
    }


@pytest.fixture
def config_file(tmp_path: Path, sample_config: dict) -> Path:
    path = tmp_path / "users.yaml"
    path.write_text(yaml.dump(sample_config))
    return path


@pytest.fixture
def wildcard_file(tmp_path: Path, wildcard_config: dict) -> Path:
    path = tmp_path / "users.yaml"
    path.write_text(yaml.dump(wildcard_config))
    return path


# ---------------------------------------------------------------------------
# Pure function tests: parse_users_config
# ---------------------------------------------------------------------------


class TestParseUsersConfig:
    def test_empty_config(self):
        defaults, users, wildcard = parse_users_config({})
        assert defaults == _BUILTIN_DEFAULTS
        assert users == {}
        assert wildcard is False

    def test_users_parsed(self, sample_config):
        defaults, users, wildcard = parse_users_config(sample_config)
        assert len(users) == 3
        assert users["U001"]["can_address_lobster"] is True
        assert wildcard is False

    def test_wildcard_detected(self, wildcard_config):
        defaults, users, wildcard = parse_users_config(wildcard_config)
        assert wildcard is True
        # Wildcard entry should NOT be in users_by_id
        assert "*" not in users
        # Named user still present
        assert "U001" in users

    def test_user_inherits_defaults(self, sample_config):
        defaults, users, _ = parse_users_config(sample_config)
        # U003 has can_address_lobster=False explicitly, is_admin from defaults
        assert users["U003"]["is_admin"] is False

    def test_none_users_list(self):
        defaults, users, wildcard = parse_users_config({"users": None})
        assert users == {}
        assert wildcard is False

    def test_user_without_id_skipped(self):
        raw = {"users": [{"name": "no-id"}]}
        _, users, _ = parse_users_config(raw)
        assert users == {}


# ---------------------------------------------------------------------------
# Pure function tests: check_can_address
# ---------------------------------------------------------------------------


class TestCheckCanAddress:
    def test_listed_user_permitted(self, sample_config):
        defaults, users, wildcard = parse_users_config(sample_config)
        assert check_can_address(
            slack_user_id="U001", defaults=defaults,
            users_by_id=users, has_wildcard=wildcard,
        ) is True

    def test_listed_user_denied(self, sample_config):
        defaults, users, wildcard = parse_users_config(sample_config)
        assert check_can_address(
            slack_user_id="U003", defaults=defaults,
            users_by_id=users, has_wildcard=wildcard,
        ) is False

    def test_unlisted_user_uses_defaults(self, sample_config):
        defaults, users, wildcard = parse_users_config(sample_config)
        # defaults have can_address_lobster=False
        assert check_can_address(
            slack_user_id="UUNKNOWN", defaults=defaults,
            users_by_id=users, has_wildcard=wildcard,
        ) is False

    def test_wildcard_permits_all(self, wildcard_config):
        defaults, users, wildcard = parse_users_config(wildcard_config)
        assert check_can_address(
            slack_user_id="UANYONE", defaults=defaults,
            users_by_id=users, has_wildcard=wildcard,
        ) is True

    def test_wildcard_permits_unlisted(self, wildcard_config):
        defaults, users, wildcard = parse_users_config(wildcard_config)
        assert check_can_address(
            slack_user_id="UNOBODY", defaults=defaults,
            users_by_id=users, has_wildcard=wildcard,
        ) is True


# ---------------------------------------------------------------------------
# Pure function tests: check_is_admin
# ---------------------------------------------------------------------------


class TestCheckIsAdmin:
    def test_admin_user(self, sample_config):
        defaults, users, _ = parse_users_config(sample_config)
        assert check_is_admin(
            slack_user_id="U001", defaults=defaults, users_by_id=users,
        ) is True

    def test_non_admin_user(self, sample_config):
        defaults, users, _ = parse_users_config(sample_config)
        assert check_is_admin(
            slack_user_id="U002", defaults=defaults, users_by_id=users,
        ) is False

    def test_unlisted_user_not_admin(self, sample_config):
        defaults, users, _ = parse_users_config(sample_config)
        assert check_is_admin(
            slack_user_id="UUNKNOWN", defaults=defaults, users_by_id=users,
        ) is False

    def test_wildcard_does_not_grant_admin(self, wildcard_config):
        defaults, users, _ = parse_users_config(wildcard_config)
        # UANYONE is not listed — wildcard doesn't grant admin
        assert check_is_admin(
            slack_user_id="UANYONE", defaults=defaults, users_by_id=users,
        ) is False


# ---------------------------------------------------------------------------
# UserPermissions class tests (stateful wrapper)
# ---------------------------------------------------------------------------


class TestUserPermissions:
    def test_loads_from_file(self, config_file):
        up = UserPermissions(config_path=str(config_file))
        assert up.can_address_lobster("U001") is True
        assert up.can_address_lobster("U003") is False

    def test_missing_file_uses_defaults(self, tmp_path):
        up = UserPermissions(config_path=str(tmp_path / "nonexistent.yaml"))
        # Default: can_address_lobster=False, no wildcard
        assert up.can_address_lobster("UANYONE") is False

    def test_wildcard_from_file(self, wildcard_file):
        up = UserPermissions(config_path=str(wildcard_file))
        assert up.can_address_lobster("UANYONE") is True
        # Wildcard doesn't grant admin
        assert up.is_admin("UANYONE") is False
        # But named admin user still is admin
        assert up.is_admin("U001") is True

    def test_reload_picks_up_changes(self, config_file):
        up = UserPermissions(config_path=str(config_file))
        assert up.can_address_lobster("U003") is False

        # Grant U003 permission
        new_config = {
            "users": [{"id": "U003", "can_address_lobster": True}],
        }
        config_file.write_text(yaml.dump(new_config))
        up.reload()
        assert up.can_address_lobster("U003") is True

    def test_corrupt_file_keeps_previous(self, config_file):
        up = UserPermissions(config_path=str(config_file))
        assert up.can_address_lobster("U001") is True

        config_file.write_text("!!invalid: yaml: [[[")
        up.reload()
        # Should keep previous config
        assert up.can_address_lobster("U001") is True

    def test_is_admin(self, config_file):
        up = UserPermissions(config_path=str(config_file))
        assert up.is_admin("U001") is True
        assert up.is_admin("U002") is False
