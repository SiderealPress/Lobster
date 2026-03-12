"""
router.py — Pure functions for detecting and routing Telegram group messages.

The Lobster bot writes incoming messages to ~/messages/inbox/ with a `source`
field. This module provides the logic to:
  - Detect whether a message is from a group/supergroup
  - Assign the correct source tag ("lobster-group" vs default)
  - Build a well-formed inbox message dict

All functions are pure — no I/O, no side effects, fully testable.
"""

from datetime import datetime, timezone
from typing import Any

# Chat types as defined by the Telegram Bot API
CHAT_TYPE_PRIVATE = "private"
CHAT_TYPE_GROUP = "group"
CHAT_TYPE_SUPERGROUP = "supergroup"
CHAT_TYPE_CHANNEL = "channel"

# Source tags
SOURCE_GROUP = "lobster-group"
SOURCE_DEFAULT = "telegram"

# Group chat types (both group and supergroup route to lobster-group)
_GROUP_CHAT_TYPES = frozenset({CHAT_TYPE_GROUP, CHAT_TYPE_SUPERGROUP})


# ---------------------------------------------------------------------------
# Pure detection functions
# ---------------------------------------------------------------------------

def is_group_message(chat_type: str) -> bool:
    """Return True if chat_type indicates a group or supergroup.

    >>> is_group_message("group")
    True
    >>> is_group_message("supergroup")
    True
    >>> is_group_message("private")
    False
    >>> is_group_message("channel")
    False
    """
    return chat_type in _GROUP_CHAT_TYPES


def get_source_for_chat(chat_type: str, default_source: str = SOURCE_DEFAULT) -> str:
    """Return the inbox source tag for this chat type.

    Groups and supergroups get the "lobster-group" tag.
    All other types (private, channel, unknown) get default_source.

    >>> get_source_for_chat("group")
    'lobster-group'
    >>> get_source_for_chat("supergroup")
    'lobster-group'
    >>> get_source_for_chat("private")
    'telegram'
    >>> get_source_for_chat("private", default_source="whatsapp")
    'whatsapp'
    >>> get_source_for_chat("channel")
    'telegram'
    """
    return SOURCE_GROUP if is_group_message(chat_type) else default_source


# ---------------------------------------------------------------------------
# Inbox message builder
# ---------------------------------------------------------------------------

def build_inbox_message(
    text: str,
    chat_id: int,
    user_id: int,
    chat_type: str,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    message_id: int | None = None,
    timestamp: str | None = None,
    default_source: str = SOURCE_DEFAULT,
) -> dict[str, Any]:
    """Build the inbox message dict with the correct source tag.

    Pure function — no I/O. The result can be written to ~/messages/inbox/ by
    the caller.

    Args:
        text: Message text content
        chat_id: Telegram chat ID (negative for groups)
        user_id: Telegram user ID of the sender
        chat_type: Telegram chat type ("private", "group", "supergroup", "channel")
        username: Optional Telegram @username (without @)
        first_name: Optional sender first name
        last_name: Optional sender last name
        message_id: Optional Telegram message ID
        timestamp: Optional ISO 8601 timestamp; defaults to now (UTC)
        default_source: Source tag to use for non-group chats

    Returns:
        dict ready to be JSON-serialized and written to the inbox
    """
    source = get_source_for_chat(chat_type, default_source)
    ts = timestamp or datetime.now(timezone.utc).isoformat()

    msg: dict[str, Any] = {
        "text": text,
        "chat_id": chat_id,
        "user_id": user_id,
        "chat_type": chat_type,
        "source": source,
        "timestamp": ts,
    }

    if username is not None:
        msg["username"] = username
    if first_name is not None:
        msg["first_name"] = first_name
    if last_name is not None:
        msg["last_name"] = last_name
    if message_id is not None:
        msg["message_id"] = message_id

    return msg


# ---------------------------------------------------------------------------
# Routing decision type
# ---------------------------------------------------------------------------

def classify_message(
    chat_id: int,
    user_id: int,
    chat_type: str,
    text: str,
) -> dict[str, Any]:
    """Return a routing classification for a message.

    Returns:
        dict with keys:
          - is_group: bool
          - source: str — the inbox source tag
          - requires_gating: bool — True if group and needs whitelist check
    """
    group = is_group_message(chat_type)
    return {
        "is_group": group,
        "source": get_source_for_chat(chat_type),
        "requires_gating": group,
        "chat_id": chat_id,
        "user_id": user_id,
    }
