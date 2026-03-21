"""
src/channels/outbox.py — OutboxFileHandler channel adapter (BIS-159 Slice 5).

Concrete ChannelAdapter implementation that writes reply dicts as JSON files
to a configurable outbox directory.  The file watcher process (bot/lobster_bot.py
or equivalent) picks them up and dispatches them to the correct transport.

This is the primary delivery mechanism for all channels: Telegram, WhatsApp,
SMS, Bisque relay, and Slack all read from a shared outbox directory (or
channel-specific subdirectories) and dispatch based on the ``source`` field.

Design:
  - Satisfies ChannelAdapter via structural subtyping (no explicit base class).
  - Uses atomic_write_json (write-to-temp + rename) so the watcher never sees
    a partial file.
  - Immutable after construction: the outbox_dir path is set at init time.
  - Thread-safe: atomic_write_json is re-entrant (each call creates its own
    tempfile in the same directory).
"""

from __future__ import annotations

from pathlib import Path

from utils.fs import atomic_write_json


class OutboxFileHandler:
    """Write reply dicts atomically to an outbox directory.

    The file watcher polls the directory and dispatches files whose ``source``
    field matches a registered transport handler.

    Args:
        outbox_dir: Path to the directory where reply JSON files are written.
                    Must exist and be writable; created lazily on first write
                    if it does not exist.

    Example:
        handler = OutboxFileHandler(outbox_dir=Path("/home/admin/messages/outbox"))
        handler.write({"id": "12345_telegram", "chat_id": ADMIN_CHAT_ID_REDACTED, "text": "Hi"})
        # Writes /home/admin/messages/outbox/12345_telegram.json atomically.
    """

    def __init__(self, outbox_dir: Path) -> None:
        self._outbox_dir = outbox_dir

    @property
    def outbox_dir(self) -> Path:
        """The outbox directory this handler writes to."""
        return self._outbox_dir

    def write(self, reply: dict) -> None:
        """Write *reply* as a JSON file in the outbox directory.

        The filename is ``{reply['id']}.json``.  If ``reply`` has no ``id``
        field, raises ``KeyError``.

        Uses write-to-temp-then-rename so the file watcher never observes a
        partial write.  The parent directory is created if absent.

        Args:
            reply: Reply dict.  Must have an ``id`` key.

        Raises:
            KeyError:  If ``reply`` does not contain an ``id`` field.
            OSError:   If the atomic write or directory creation fails.
        """
        reply_id = reply["id"]  # Intentional KeyError if missing
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        dest = self._outbox_dir / f"{reply_id}.json"
        atomic_write_json(dest, reply)

    def __repr__(self) -> str:
        return f"OutboxFileHandler(outbox_dir={self._outbox_dir!r})"
