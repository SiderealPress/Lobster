"""
Shared outbox watchdog handler for synchronous (non-async) Lobster channel routers.

All sync routers (Slack, SMS, WhatsApp/Twilio) share the same file-watching loop:

1. A JSON file appears in ``~/messages/outbox/``.
2. The handler checks whether ``reply["source"]`` matches this channel.
3. If it matches, the handler calls ``send_fn(reply)`` to deliver the message.
4. On success the file is removed; on failure it is left for inspection.

Usage
-----
::

    from src.channels.outbox import OutboxFileHandler

    def send_sms(reply: dict) -> bool:
        to   = reply["chat_id"]
        text = reply["text"]
        return twilio_client.messages.create(from_=SMS_NUMBER, to=to, body=text) is not None

    handler = OutboxFileHandler(source="sms", send_fn=send_sms, log=log)
    observer = Observer()
    observer.schedule(handler, str(OUTBOX_DIR), recursive=False)
    observer.start()

Notes
-----
- ``send_fn`` receives the full decoded reply dict and returns ``True`` on
  success, ``False`` on failure.
- Each file is processed in a daemon thread so the watchdog callback returns
  immediately and the observer is never blocked.
- A short ``sleep(READ_DELAY_SECS)`` before reading guards against a
  partially-written file appearing on ``on_created``.
- ``on_moved`` is also handled so that atomic writes (temp-file -> rename) are
  caught correctly -- this matches the ``atomic_write_json`` pattern used by
  the MCP inbox server.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import Any

from watchdog.events import FileSystemEventHandler

# How long to wait after a file event before attempting to read.
# Guards against partially-written files landing on on_created.
READ_DELAY_SECS: float = 0.1


class OutboxFileHandler(FileSystemEventHandler):
    """Watchdog handler that routes outbox reply files to a send function.

    Parameters
    ----------
    source:
        The channel source string to match (e.g. ``"slack"``, ``"sms"``,
        ``"whatsapp"``).  Only files whose ``reply["source"]`` equals this
        value (case-insensitive) are processed.
    send_fn:
        Pure callable that accepts a decoded reply dict and returns ``True``
        on successful delivery, ``False`` otherwise.  Side effects (API
        calls, network I/O) live here.
    log:
        Logger instance; if ``None``, a module-level logger is used.
    read_delay:
        Seconds to sleep before reading a newly appeared file.  Override
        for tests (set to 0).
    """

    def __init__(
        self,
        source: str,
        send_fn: Callable[[dict[str, Any]], bool],
        log: logging.Logger | None = None,
        read_delay: float = READ_DELAY_SECS,
    ) -> None:
        super().__init__()
        self._source = source.lower()
        self._send_fn = send_fn
        self._log = log or logging.getLogger(__name__)
        self._read_delay = read_delay

    # ------------------------------------------------------------------
    # Watchdog callbacks
    # ------------------------------------------------------------------

    def on_created(self, event) -> None:
        if event.is_directory or not event.src_path.endswith(".json"):
            return
        self._dispatch(event.src_path)

    def on_moved(self, event) -> None:
        """Handle atomic writes: temp file renamed to .json."""
        if event.is_directory or not event.dest_path.endswith(".json"):
            return
        self._dispatch(event.dest_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch(self, filepath: str) -> None:
        """Spawn a daemon thread to process *filepath*."""
        Thread(target=self._process, args=(filepath,), daemon=True).start()

    def _process(self, filepath: str) -> None:
        """Read the outbox file and deliver it if it belongs to this channel.

        This is the single shared implementation that previously lived
        (duplicated) inside each router's ``OutboxHandler._process`` method.
        """
        try:
            if self._read_delay:
                time.sleep(self._read_delay)

            try:
                with open(filepath, "r") as f:
                    reply = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                self._log.error("Failed to read outbox file %s: %s", filepath, exc)
                return

            # Skip files that belong to a different channel.
            if reply.get("source", "").lower() != self._source:
                return

            chat_id = reply.get("chat_id", "")
            text = reply.get("text", "")

            if not chat_id or not text:
                self._log.warning(
                    "Invalid %s reply %s: missing chat_id or text",
                    self._source,
                    filepath,
                )
                _safe_remove(filepath, self._log)
                return

            if self._send_fn(reply):
                _safe_remove(filepath, self._log)
            else:
                self._log.error(
                    "Failed to deliver %s reply from %s -- leaving for retry",
                    self._source,
                    filepath,
                )

        except Exception as exc:
            self._log.error("Error processing outbox file %s: %s", filepath, exc)


# ---------------------------------------------------------------------------
# Startup helper
# ---------------------------------------------------------------------------


def drain_outbox(
    outbox_dir: Path,
    source: str,
    send_fn: Callable[[dict[str, Any]], bool],
    log: logging.Logger | None = None,
) -> None:
    """Process any reply files already present in *outbox_dir* at startup.

    Call this once before starting the watchdog observer to avoid missing
    files that queued up while the router was offline.

    Parameters
    ----------
    outbox_dir:
        Directory to scan (``~/messages/outbox/``).
    source:
        Channel source string to match.
    send_fn:
        Same callable passed to :class:`OutboxFileHandler`.
    log:
        Logger; if ``None``, the module logger is used.
    """
    _log = log or logging.getLogger(__name__)
    handler = OutboxFileHandler(source=source, send_fn=send_fn, log=_log, read_delay=0)
    for filepath in sorted(outbox_dir.glob("*.json")):
        try:
            with open(filepath, "r") as f:
                reply = json.load(f)
            if reply.get("source", "").lower() == source.lower():
                handler._process(str(filepath))
        except Exception as exc:
            _log.error("Error draining outbox file %s: %s", filepath, exc)


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------


def _safe_remove(filepath: str, log: logging.Logger) -> None:
    """Remove *filepath*, logging a warning on failure instead of raising."""
    try:
        os.remove(filepath)
    except OSError as exc:
        log.warning("Could not remove outbox file %s: %s", filepath, exc)
