"""
src/channels/base.py — ChannelAdapter Protocol (BIS-159 Slice 5).

Defines the structural Protocol that all channel adapters must satisfy.
A "channel adapter" is responsible for delivering a reply dict to its
transport (Telegram bot, WhatsApp/Twilio, SMS, Slack, Bisque relay, etc.)
by writing it to the appropriate outbox directory.

Design principles:
  - Protocol (structural subtyping) — no inheritance required.
  - Pure interface: adapters own no state beyond the path they write to.
  - Callers are agnostic to transport details; they hand off a reply dict
    and the adapter handles directory routing.

Usage:
    def send(adapter: ChannelAdapter, reply: dict) -> None:
        adapter.write(reply)

    # Concrete adapter
    adapter = OutboxFileHandler(outbox_dir=Path("~/messages/outbox"))
    send(adapter, {"id": "...", "text": "Hello", "chat_id": 123})
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ChannelAdapter(Protocol):
    """Structural protocol for channel adapters.

    Any object implementing ``write(reply)`` satisfies this protocol.
    No base class or explicit registration is needed.

    Methods:
        write: Deliver *reply* to the channel's transport.  Implementations
               must be idempotent with respect to duplicate calls for the
               same reply id.
    """

    def write(self, reply: dict) -> None:
        """Deliver *reply* to the underlying transport.

        Args:
            reply: A reply dict that must contain at minimum:
                   - ``id`` (str): Unique message identifier.
                   - ``chat_id`` (int|str): Destination chat/user.
                   - ``text`` (str): Reply body.
                   Additional fields are transport-specific and may be
                   ignored by adapters that do not support them.

        Raises:
            OSError: If the underlying write fails (e.g., directory does
                     not exist or is not writable).
        """
        ...  # pragma: no cover
