"""
registration.py — Registration flow for unknown users in enabled groups.

When a user sends a message to an enabled group but is not yet whitelisted,
we send them a private DM with instructions on how to get access.

Design:
- Pure registration logic (build_registration_dm) is separate from I/O
- The send_dm parameter is an injectable function for testability
- No global state — everything is passed in as arguments
"""

from typing import Callable, NamedTuple, Protocol

# Default message template
DEFAULT_REGISTRATION_MESSAGE = (
    "Hi! To use Lobster in this group, please send /register to this bot directly."
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class RegistrationDM(NamedTuple):
    """A registration DM to be sent to a user.

    Attributes:
        user_id: Recipient's Telegram user ID
        text: Message text to send
        group_chat_id: The group the user tried to message (for context)
    """
    user_id: int
    text: str
    group_chat_id: int


class SendDMResult(NamedTuple):
    """Result of attempting to send a registration DM.

    Attributes:
        success: Whether the DM was delivered
        user_id: Target user ID
        error: Error message if failed, else None
    """
    success: bool
    user_id: int
    error: str | None = None


# A callable type for sending DMs (for dependency injection)
SendDMFn = Callable[[int, str], bool]


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def build_registration_dm(
    user_id: int,
    group_chat_id: int,
    group_name: str | None = None,
    message_template: str = DEFAULT_REGISTRATION_MESSAGE,
) -> RegistrationDM:
    """Build a registration DM without sending it.

    The message template can optionally reference {group_name} which will
    be replaced if group_name is provided.

    Args:
        user_id: Telegram user ID to send the DM to
        group_chat_id: Group chat ID where the user was blocked
        group_name: Optional human-readable group name for the message
        message_template: Message to send; supports {group_name} placeholder

    Returns:
        RegistrationDM NamedTuple ready to be dispatched

    >>> dm = build_registration_dm(12345, -100999)
    >>> dm.user_id
    12345
    >>> dm.group_chat_id
    -100999
    >>> "register" in dm.text.lower()
    True
    """
    if group_name and "{group_name}" in message_template:
        text = message_template.format(group_name=group_name)
    else:
        text = message_template

    return RegistrationDM(
        user_id=user_id,
        text=text,
        group_chat_id=group_chat_id,
    )


def send_registration_dm(
    dm: RegistrationDM,
    send_fn: SendDMFn,
) -> SendDMResult:
    """Send a registration DM using the provided send function.

    The send_fn is an injected dependency so this can be tested without
    a real Telegram connection. In production, pass the actual bot send
    function; in tests, pass a mock or stub.

    Args:
        dm: The RegistrationDM to send (from build_registration_dm)
        send_fn: Callable(user_id, text) -> bool (True = sent, False = failed)

    Returns:
        SendDMResult with success status
    """
    try:
        success = send_fn(dm.user_id, dm.text)
        return SendDMResult(success=success, user_id=dm.user_id)
    except Exception as exc:
        return SendDMResult(success=False, user_id=dm.user_id, error=str(exc))


# ---------------------------------------------------------------------------
# High-level convenience function
# ---------------------------------------------------------------------------

def handle_registration_flow(
    user_id: int,
    group_chat_id: int,
    send_fn: SendDMFn,
    group_name: str | None = None,
    message_template: str = DEFAULT_REGISTRATION_MESSAGE,
) -> SendDMResult:
    """Full registration flow: build DM and send it.

    This is the main entry point for the registration flow. It combines
    build_registration_dm and send_registration_dm.

    Args:
        user_id: Telegram user ID of the unregistered user
        group_chat_id: Group chat ID where they were blocked
        send_fn: Callable(user_id, text) -> bool for sending the DM
        group_name: Optional group name for personalized messages
        message_template: Customizable message template

    Returns:
        SendDMResult indicating success or failure
    """
    dm = build_registration_dm(
        user_id=user_id,
        group_chat_id=group_chat_id,
        group_name=group_name,
        message_template=message_template,
    )
    return send_registration_dm(dm, send_fn)
