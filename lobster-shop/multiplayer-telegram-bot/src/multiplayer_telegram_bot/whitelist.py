"""
whitelist.py — Pure functions for loading and querying the group whitelist.

State file: ~/messages/config/group-whitelist.json

Schema:
{
  "groups": {
    "<chat_id>": {
      "name": "Group Name",
      "enabled": true,
      "allowed_user_ids": [123456789, 987654321]
    }
  }
}
"""

import json
import os
import tempfile
from pathlib import Path
from typing import TypedDict


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class GroupConfig(TypedDict):
    name: str
    enabled: bool
    allowed_user_ids: list[int]


class WhitelistStore(TypedDict):
    groups: dict[str, GroupConfig]


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

def _default_whitelist_path() -> Path:
    messages_dir = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_dir / "config" / "group-whitelist.json"


# ---------------------------------------------------------------------------
# Pure I/O helpers
# ---------------------------------------------------------------------------

def _empty_store() -> WhitelistStore:
    return {"groups": {}}


def load_whitelist(path: Path | None = None) -> WhitelistStore:
    """Load group-whitelist.json. Returns empty store if missing or malformed."""
    path = path or _default_whitelist_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
            return _empty_store()
        return data  # type: ignore[return-value]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_store()


def save_whitelist(store: WhitelistStore, path: Path | None = None) -> None:
    """Atomically write whitelist to disk."""
    path = path or _default_whitelist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(store, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Pure query functions
# ---------------------------------------------------------------------------

def is_group_enabled(chat_id: str | int, store: WhitelistStore) -> bool:
    """Return True if the group is in the whitelist and enabled."""
    key = str(chat_id)
    group = store["groups"].get(key)
    return group is not None and group.get("enabled", False)


def is_user_allowed(
    user_id: int,
    chat_id: str | int,
    store: WhitelistStore,
) -> bool:
    """Return True if user_id is in the allowed list for chat_id."""
    key = str(chat_id)
    group = store["groups"].get(key)
    if group is None:
        return False
    return user_id in group.get("allowed_user_ids", [])


def get_group_config(
    chat_id: str | int,
    store: WhitelistStore,
) -> GroupConfig | None:
    """Return config for a specific group, or None if not found."""
    return store["groups"].get(str(chat_id))


# ---------------------------------------------------------------------------
# Mutation helpers (return new store, don't mutate in place)
# ---------------------------------------------------------------------------

def enable_group(
    chat_id: str | int,
    name: str,
    store: WhitelistStore,
) -> WhitelistStore:
    """Return a new store with the group enabled."""
    key = str(chat_id)
    groups = dict(store["groups"])
    existing = dict(groups.get(key, {}))
    existing["name"] = name
    existing["enabled"] = True
    if "allowed_user_ids" not in existing:
        existing["allowed_user_ids"] = []
    groups[key] = existing  # type: ignore[assignment]
    return {"groups": groups}


def add_allowed_user(
    user_id: int,
    chat_id: str | int,
    store: WhitelistStore,
) -> WhitelistStore:
    """Return a new store with user_id added to the group's allowed list."""
    key = str(chat_id)
    groups = dict(store["groups"])
    if key not in groups:
        groups[key] = {"name": key, "enabled": True, "allowed_user_ids": []}  # type: ignore[assignment]
    group = dict(groups[key])
    allowed = list(group.get("allowed_user_ids", []))
    if user_id not in allowed:
        allowed.append(user_id)
    group["allowed_user_ids"] = allowed
    groups[key] = group  # type: ignore[assignment]
    return {"groups": groups}


def remove_allowed_user(
    user_id: int,
    chat_id: str | int,
    store: WhitelistStore,
) -> WhitelistStore:
    """Return a new store with user_id removed from the group's allowed list."""
    key = str(chat_id)
    groups = dict(store["groups"])
    if key not in groups:
        return store
    group = dict(groups[key])
    allowed = [uid for uid in group.get("allowed_user_ids", []) if uid != user_id]
    group["allowed_user_ids"] = allowed
    groups[key] = group  # type: ignore[assignment]
    return {"groups": groups}
