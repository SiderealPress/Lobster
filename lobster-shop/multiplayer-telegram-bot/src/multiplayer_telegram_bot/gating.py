"""
gating.py — Pure gating logic for group message access control.

Given a Telegram message from a group chat, determines the correct
action: allow it through, silently drop it, or trigger the registration
flow for an unknown user.

All functions are pure — no I/O, no side effects. The caller handles
the actual message writing, dropping, or DM sending.
"""

from enum import Enum, auto
from typing import NamedTuple

from .whitelist import WhitelistStore, is_group_enabled, is_user_allowed


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
