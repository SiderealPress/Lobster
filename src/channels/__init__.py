"""
src/channels — Channel adapter protocol for Lobster (BIS-159 Slice 5).

Exports the ChannelAdapter Protocol and the OutboxFileHandler concrete
implementation so callers can import from a single entry point:

    from channels import ChannelAdapter, OutboxFileHandler
"""

from channels.base import ChannelAdapter
from channels.outbox import OutboxFileHandler

__all__ = ["ChannelAdapter", "OutboxFileHandler"]
