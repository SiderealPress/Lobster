"""
commands.py — Command handlers for /enable-group-bot and /whitelist.

These are pure command-handling functions that:
1. Parse and validate command arguments
2. Load the current whitelist state
3. Apply a whitelist mutation (pure function from whitelist.py)
4. Save the updated whitelist
5. Return a confirmation or error string

Side effects (file I/O) are isolated to the two functions that load/save.
The actual Telegram reply is handled by the caller.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .whitelist import (
    WhitelistStore,
    add_allowed_user,
    enable_group,
    load_whitelist,
    remove_allowed_user,
    save_whitelist,
)


# ---------------------------------------------------------------------------
# Command result type
# ---------------------------------------------------------------------------

class CommandResult(NamedTuple):
    """Result of executing a bot command.

    Attributes:
        success: True if command was executed successfully
        reply: Text to send back to the user
        updated_store: The new whitelist state (None if no change was made)
    """
    success: bool
    reply: str
    updated_store: WhitelistStore | None = None


# ---------------------------------------------------------------------------
# Argument parsing (pure)
# ---------------------------------------------------------------------------

def parse_enable_group_bot(text: str) -> tuple[str | None, str]:
    """Parse the /enable-group-bot command arguments.

    Expected format: /enable-group-bot GROUP_ID [optional name]

    Returns:
        (group_id, error_message) — error_message is empty string on success
    """
    parts = text.strip().split(maxsplit=2)

    # parts[0] is the command itself (/enable-group-bot)
    if len(parts) < 2:
        return None, "Usage: /enable-group-bot GROUP_ID [optional name]"

    group_id = parts[1]

    # Validate: group IDs are negative integers (e.g. -1001234567890)
    try:
        gid_int = int(group_id)
        if gid_int >= 0:
            return None, f"Group ID must be negative (got {group_id}). Group IDs start with - or -100."
    except ValueError:
        return None, f"Invalid group ID '{group_id}' — must be a negative integer (e.g. -1001234567890)."

    return group_id, ""


def parse_whitelist(text: str) -> tuple[tuple[int, str] | None, str]:
    """Parse the /whitelist command arguments.

    Expected format: /whitelist USER_ID GROUP_ID

    Returns:
        ((user_id, group_id), error_message) — error_message is empty on success
    """
    parts = text.strip().split()

    if len(parts) < 3:
        return None, "Usage: /whitelist USER_ID GROUP_ID"

    user_id_str = parts[1]
    group_id_str = parts[2]

    try:
        user_id = int(user_id_str)
        if user_id <= 0:
            return None, f"User ID must be a positive integer (got {user_id_str})."
    except ValueError:
        return None, f"Invalid user ID '{user_id_str}' — must be a positive integer."

    try:
        group_id_int = int(group_id_str)
        if group_id_int >= 0:
            return None, f"Group ID must be negative (got {group_id_str})."
    except ValueError:
        return None, f"Invalid group ID '{group_id_str}' — must be a negative integer."

    return (user_id, group_id_str), ""


def parse_unwhitelist(text: str) -> tuple[tuple[int, str] | None, str]:
    """Parse the /unwhitelist command arguments.

    Expected format: /unwhitelist USER_ID GROUP_ID

    Returns:
        ((user_id, group_id), error_message) — error_message is empty on success
    """
    # Same parsing logic as /whitelist
    result, error = parse_whitelist(text.replace("/unwhitelist", "/whitelist", 1))
    return result, error


# ---------------------------------------------------------------------------
# Command handlers (I/O at boundaries)
# ---------------------------------------------------------------------------

def handle_enable_group_bot(
    text: str,
    group_name: str = "",
    whitelist_path: Path | None = None,
) -> CommandResult:
    """Handle the /enable-group-bot command.

    Enables a Telegram group in the whitelist so its messages are processed.

    Args:
        text: Full command text (e.g. "/enable-group-bot -1001234567890 My Group")
        group_name: Optional display name for the group (overrides text arg)
        whitelist_path: Path to group-whitelist.json (None = default location)

    Returns:
        CommandResult with success status and reply text
    """
    group_id, error = parse_enable_group_bot(text)
    if error:
        return CommandResult(success=False, reply=error)

    # Extract optional name from command text if not provided
    parts = text.strip().split(maxsplit=2)
    name = group_name or (parts[2] if len(parts) >= 3 else group_id)

    store = load_whitelist(whitelist_path)
    updated = enable_group(group_id, name, store)
    save_whitelist(updated, whitelist_path)

    return CommandResult(
        success=True,
        reply=f"Group {name} ({group_id}) is now enabled for Lobster bot access.",
        updated_store=updated,
    )


def handle_whitelist(
    text: str,
    whitelist_path: Path | None = None,
) -> CommandResult:
    """Handle the /whitelist command.

    Adds a user to the allowed list for a specific group.

    Args:
        text: Full command text (e.g. "/whitelist 123456789 -1001234567890")
        whitelist_path: Path to group-whitelist.json (None = default location)

    Returns:
        CommandResult with success status and reply text
    """
    result, error = parse_whitelist(text)
    if error:
        return CommandResult(success=False, reply=error)

    user_id, group_id = result

    store = load_whitelist(whitelist_path)

    # Check if group is enabled; warn but still add user
    group = store["groups"].get(str(group_id))
    group_enabled = group is not None and group.get("enabled", False)

    updated = add_allowed_user(user_id, group_id, store)
    save_whitelist(updated, whitelist_path)

    reply = f"User {user_id} added to whitelist for group {group_id}."
    if not group_enabled:
        reply += f"\n\nNote: Group {group_id} is not yet enabled. Run /enable-group-bot {group_id} to activate it."

    return CommandResult(
        success=True,
        reply=reply,
        updated_store=updated,
    )


def handle_unwhitelist(
    text: str,
    whitelist_path: Path | None = None,
) -> CommandResult:
    """Handle the /unwhitelist command.

    Removes a user from the allowed list for a specific group.

    Args:
        text: Full command text (e.g. "/unwhitelist 123456789 -1001234567890")
        whitelist_path: Path to group-whitelist.json (None = default location)

    Returns:
        CommandResult with success status and reply text
    """
    result, error = parse_unwhitelist(text)
    if error:
        return CommandResult(success=False, reply=error)

    user_id, group_id = result

    store = load_whitelist(whitelist_path)
    updated = remove_allowed_user(user_id, group_id, store)
    save_whitelist(updated, whitelist_path)

    return CommandResult(
        success=True,
        reply=f"User {user_id} removed from whitelist for group {group_id}.",
        updated_store=updated,
    )
