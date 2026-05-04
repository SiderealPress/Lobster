"""
gating.py — Pure gating logic for group message access control.

Given a Telegram message from a group chat, determines the correct
action: allow it through, silently drop it, or trigger the registration
flow for an unknown user.

Also provides invocation detection: is_invocation() determines whether
a group message is directly addressed to the bot (@mention, /command,
reply-to-bot), and is_session_followup() checks whether a message is
a follow-up within an active session.

All functions are pure — no I/O, no side effects. The caller handles
the actual message writing, dropping, or DM sending.
"""

from enum import Enum, auto
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from .whitelist import WhitelistStore, is_group_enabled, is_user_allowed

if TYPE_CHECKING:
    from .session import GroupSession


# ---------------------------------------------------------------------------
# Gating result type
# ---------------------------------------------------------------------------

class GatingAction(Enum):
    """The action to take for a group message."""
    ALLOW = auto()               # Write message to inbox (user is whitelisted)
    DROP_SILENT = auto()         # Discard without any reply (group not enabled)
    SEND_REGISTRATION_DM = auto()  # Group enabled, user not whitelisted; send DM


class GatingResult(NamedTuple):
    """Result of gating a message.

    Attributes:
        action: What to do with the message
        chat_id: Group chat ID (negative integer)
        user_id: Sender's user ID
        reason: Human-readable explanation for logging
    """
    action: GatingAction
    chat_id: int
    user_id: int
    reason: str


# ---------------------------------------------------------------------------
# Core gating function
# ---------------------------------------------------------------------------

def gate_message(
    chat_id: int,
    user_id: int,
    store: WhitelistStore,
) -> GatingResult:
    """Determine the correct action for a group message.

    Decision tree:
    1. Is the group enabled in the whitelist?
       - No  → DROP_SILENT (unknown or disabled group)
    2. Is the user in the allowed list for this group?
       - Yes → ALLOW
       - No  → SEND_REGISTRATION_DM (group known, user unknown)

    Args:
        chat_id: Telegram group chat ID (should be negative)
        user_id: Telegram user ID of the message sender
        store: The loaded whitelist store

    Returns:
        GatingResult with the action to take and contextual info
    """
    if not is_group_enabled(chat_id, store):
        return GatingResult(
            action=GatingAction.DROP_SILENT,
            chat_id=chat_id,
            user_id=user_id,
            reason=f"Group {chat_id} is not in the whitelist or is disabled",
        )

    if is_user_allowed(user_id, chat_id, store):
        return GatingResult(
            action=GatingAction.ALLOW,
            chat_id=chat_id,
            user_id=user_id,
            reason=f"User {user_id} is whitelisted for group {chat_id}",
        )

    return GatingResult(
        action=GatingAction.SEND_REGISTRATION_DM,
        chat_id=chat_id,
        user_id=user_id,
        reason=(
            f"User {user_id} is not whitelisted for group {chat_id}; "
            "registration DM required"
        ),
    )


# ---------------------------------------------------------------------------
# Convenience predicates
# ---------------------------------------------------------------------------

def should_allow(result: GatingResult) -> bool:
    """Return True if the message should be written to the inbox."""
    return result.action == GatingAction.ALLOW


def should_drop(result: GatingResult) -> bool:
    """Return True if the message should be silently discarded."""
    return result.action == GatingAction.DROP_SILENT


def should_register(result: GatingResult) -> bool:
    """Return True if the registration DM flow should be triggered."""
    return result.action == GatingAction.SEND_REGISTRATION_DM


# ---------------------------------------------------------------------------
# Batch gating (useful for testing multiple messages at once)
# ---------------------------------------------------------------------------

def gate_messages(
    messages: list[dict],
    store: WhitelistStore,
) -> list[tuple[dict, GatingResult]]:
    """Gate a list of message dicts, each expected to have chat_id and user_id.

    Returns a list of (message, GatingResult) pairs.
    Pure function — no I/O.
    """
    return [
        (msg, gate_message(msg["chat_id"], msg["user_id"], store))
        for msg in messages
    ]


# ---------------------------------------------------------------------------
# Invocation detection (Group Chat UX Policy)
# ---------------------------------------------------------------------------

def is_invocation(
    text: Optional[str],
    bot_username: str,
    bot_user_id: int,
    entities: Optional[list[Any]],
    reply_to_user_id: Optional[int],
) -> bool:
    """Return True if this group message is directly addressed to the bot.

    Invocation conditions (any one is sufficient):
      1. Message text contains @{bot_username} mention (via entities or string search)
      2. Message text starts with / (command)
      3. Message is a reply to a message sent by bot_user_id

    Args:
        text: Message text (may be None for media-only messages)
        bot_username: Bot's @username without the @ prefix (e.g. "your_lobster_bot")
        bot_user_id: Telegram user ID of the bot
        entities: List of Telegram MessageEntity objects or dicts with 'type'/'offset'/'length'
        reply_to_user_id: user_id of the message being replied to, or None

    Returns:
        True if this message is addressed to the bot
    """
    # Condition 3: reply to bot's own message
    if reply_to_user_id is not None and reply_to_user_id == bot_user_id:
        return True

    if not text:
        return False

    # Condition 2: command (starts with /)
    if text.startswith("/"):
        return True

    # Condition 1: @mention via entities
    mention_target = f"@{bot_username}".lower()

    if entities:
        for entity in entities:
            # Support both Telegram python-telegram-bot objects and plain dicts
            if isinstance(entity, dict):
                etype = entity.get("type", "")
                offset = entity.get("offset", 0)
                length = entity.get("length", 0)
            else:
                etype = getattr(entity, "type", "")
                offset = getattr(entity, "offset", 0)
                length = getattr(entity, "length", 0)

            if etype == "mention" and length > 0:
                mentioned = text[offset : offset + length].lower()
                if mentioned == mention_target:
                    return True
    else:
        # Fallback: plain string search (case-insensitive)
        if mention_target in text.lower():
            return True

    return False


def is_session_followup(
    chat_id: int,
    user_id: int,
    active_session: Optional[Any],  # GroupSession or None
) -> bool:
    """Return True if this message is a follow-up within an active session.

    Conditions (all must be true):
      - active_session is not None
      - active_session.is_expired() is False
      - active_session.active is True
      - user_id == active_session.invoker_user_id

    Args:
        chat_id: Telegram group chat ID (used for sanity check)
        user_id: Telegram user ID of the message sender
        active_session: GroupSession or None (from get_active_session())

    Returns:
        True if this is a follow-up in an open session
    """
    if active_session is None:
        return False
    if not active_session.active:
        return False
    if active_session.is_expired():
        return False
    if user_id != active_session.invoker_user_id:
        return False
    return True
