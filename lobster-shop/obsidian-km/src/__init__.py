"""
Obsidian KM — Knowledge Management for Lobster

Automatic link capture and knowledge archival to Obsidian vault.
"""

from .link_capture import (
    capture_link,
    capture_link_sync,
    contains_url,
    extract_urls,
    CaptureResult,
    LinkNote,
)

__all__ = [
    'capture_link',
    'capture_link_sync',
    'contains_url',
    'extract_urls',
    'CaptureResult',
    'LinkNote',
]
