"""
src/channels — Shared channel adapter protocol and outbox handler.
"""

from .base import ChannelAdapter
from .outbox import OutboxFileHandler, drain_outbox

__all__ = ["ChannelAdapter", "OutboxFileHandler", "drain_outbox"]
