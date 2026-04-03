"""
Slack Connector — User Permissions (Allowlist).

Loads users.yaml and provides pure permission checks for Slack users.

Design principles:
- Config parsing isolated to load/reload; everything else is pure lookups.
- Immutable snapshots: reload() atomically replaces internal state.
- All check functions are pure: (snapshot, user_id) -> bool.
- Missing config file falls back to permissive defaults (no crash).
- Wildcard entry {id: "*"} grants permission to all users.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("slack-user-permissions")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
) / "slack-connector" / "config" / "users.yaml"

_BUILTIN_DEFAULTS: dict[str, Any] = {
    "can_address_lobster": False,
    "is_admin": False,
}

_WILDCARD_ID = "*"


# ---------------------------------------------------------------------------
# Pure functions — config parsing and permission checks
# ---------------------------------------------------------------------------


def parse_users_config(
    raw: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], bool]:
    """Parse raw YAML dict into (defaults, users_by_id, has_wildcard).

    Pure function.
    Returns:
        defaults: merged default permissions
        users_by_id: {slack_user_id: user_dict} (excludes wildcard)
        has_wildcard: True if a wildcard entry exists
    """
    file_defaults = raw.get("defaults", {})
    merged_defaults = {**_BUILTIN_DEFAULTS, **file_defaults}

    users_list = raw.get("users", []) or []
    users_by_id: dict[str, dict[str, Any]] = {}
    has_wildcard = False

    for entry in users_list:
        user_id = entry.get("id", "")
        if not user_id:
            continue
        if user_id == _WILDCARD_ID:
            has_wildcard = True
            continue
        # Merge: builtin defaults < file defaults < per-user overrides
        users_by_id[user_id] = {**merged_defaults, **entry}

    return merged_defaults, users_by_id, has_wildcard


def check_can_address(
    *,
    slack_user_id: str,
    defaults: dict[str, Any],
    users_by_id: dict[str, dict[str, Any]],
    has_wildcard: bool,
) -> bool:
    """Check whether a user is permitted to address Lobster.

    Pure function. Wildcard grants universal access.
    """
    if has_wildcard:
        return True

    user_cfg = users_by_id.get(slack_user_id)
    if user_cfg is None:
        # Not listed — use defaults
        return defaults.get("can_address_lobster", False)

    return user_cfg.get("can_address_lobster", False)


def check_is_admin(
    *,
    slack_user_id: str,
    defaults: dict[str, Any],
    users_by_id: dict[str, dict[str, Any]],
) -> bool:
    """Check whether a user is an admin.

    Pure function. Wildcard does NOT grant admin.
    """
    user_cfg = users_by_id.get(slack_user_id)
    if user_cfg is None:
        return defaults.get("is_admin", False)

    return user_cfg.get("is_admin", False)


# ---------------------------------------------------------------------------
# UserPermissions — stateful wrapper with hot-reload
# ---------------------------------------------------------------------------


class UserPermissions:
    """User permission checks with hot-reload from YAML.

    State is limited to the loaded config snapshot. All permission logic
    delegates to pure functions above.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._defaults: dict[str, Any] = {**_BUILTIN_DEFAULTS}
        self._users_by_id: dict[str, dict[str, Any]] = {}
        self._has_wildcard: bool = False
        self.reload()

    def reload(self) -> None:
        """Reload config from disk. Atomic replacement of internal state.

        If the file is missing or unparseable, falls back to builtin defaults
        (no wildcard, can_address_lobster=False).
        """
        if not self._config_path.exists():
            log.info(
                "User permissions config not found at %s, using builtin defaults",
                self._config_path,
            )
            self._defaults = {**_BUILTIN_DEFAULTS}
            self._users_by_id = {}
            self._has_wildcard = False
            return

        try:
            raw = yaml.safe_load(self._config_path.read_text()) or {}
            defaults, users_by_id, has_wildcard = parse_users_config(raw)
            # Atomic swap
            self._defaults = defaults
            self._users_by_id = users_by_id
            self._has_wildcard = has_wildcard
            log.info(
                "Loaded user permissions: %d users, wildcard=%s from %s",
                len(users_by_id),
                has_wildcard,
                self._config_path,
            )
        except Exception:
            log.exception("Failed to parse user permissions at %s", self._config_path)
            # Keep previous config on parse failure

    def can_address_lobster(self, slack_user_id: str) -> bool:
        """Check whether a user is permitted to address Lobster."""
        return check_can_address(
            slack_user_id=slack_user_id,
            defaults=self._defaults,
            users_by_id=self._users_by_id,
            has_wildcard=self._has_wildcard,
        )

    def is_admin(self, slack_user_id: str) -> bool:
        """Check whether a user is an admin."""
        return check_is_admin(
            slack_user_id=slack_user_id,
            defaults=self._defaults,
            users_by_id=self._users_by_id,
        )
