"""
ChannelAdapter Protocol — structural interface for Lobster channel routers.

Every channel router (Telegram, Slack, SMS, WhatsApp, ...) must satisfy this
Protocol so that shared infrastructure can treat them uniformly.

Usage
-----
::

    from src.channels.base import ChannelAdapter

    def uses_adapter(adapter: ChannelAdapter) -> None:
        adapter.start()
        ...

Because the Protocol is ``@runtime_checkable``, you can use
``isinstance(obj, ChannelAdapter)`` for duck-type checks at runtime.

Note: The async Telegram router (``lobster_bot.py``) satisfies this protocol
structurally but is intentionally kept separate because its outbox handler
requires an asyncio event loop.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChannelAdapter(Protocol):
    """Structural interface that all channel routers must satisfy.

    Attributes
    ----------
    source:
        The channel name (e.g. ``"slack"``, ``"sms"``, ``"whatsapp"``).
        Used to match outbox reply files by ``reply["source"]``.
    """

    source: str

    def process_outbox_reply(self, reply: dict) -> bool:
        """Deliver one decoded outbox reply dict.

        Parameters
        ----------
        reply:
            The decoded JSON dict from an outbox file.

        Returns
        -------
        bool
            ``True`` on successful delivery, ``False`` on failure.
        """
        ...

    def start(self) -> None:
        """Start the router (connect to service, begin watching outbox, etc.)."""
        ...

    def stop(self) -> None:
        """Gracefully stop the router and release resources."""
        ...
