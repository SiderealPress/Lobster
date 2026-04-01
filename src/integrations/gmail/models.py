"""
Immutable domain types for the Gmail integration.

Design principles:
- All types are frozen dataclasses (immutable value objects).
- Datetimes are always timezone-aware UTC.
- No I/O or side effects — this module is pure data definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass(frozen=True)
class GmailMessage:
    """Immutable representation of a single Gmail message.

    Attributes:
        id:          Gmail-assigned message identifier.
        thread_id:   Thread this message belongs to.
        subject:     Message subject header (empty string if absent).
        sender:      ``From`` header value, e.g. ``"Name <email@domain.com>"``.
        recipients:  ``To`` header values as a list of address strings.
        date:        Message date as a timezone-aware UTC datetime.
        snippet:     Gmail's ~200-character plain-text preview.
        body_text:   Full plain-text body.  Empty string when ``format``
                     was ``"metadata"`` or ``"minimal"`` (body not requested).
        label_ids:   List of Gmail label IDs applied to this message (e.g.
                     ``["INBOX", "UNREAD"]``).
        is_unread:   True if ``"UNREAD"`` is in ``label_ids``.
    """

    id: str
    thread_id: str
    subject: str
    sender: str
    recipients: List[str]
    date: datetime
    snippet: str
    body_text: str
    label_ids: List[str]
    is_unread: bool


@dataclass(frozen=True)
class GmailThread:
    """Immutable representation of a Gmail thread (conversation).

    Attributes:
        id:       Gmail-assigned thread identifier.
        messages: All messages in the thread, ordered oldest-first.
        subject:  Subject derived from the first message in the thread.
                  Empty string if the thread has no messages.
    """

    id: str
    messages: List[GmailMessage]
    subject: str
