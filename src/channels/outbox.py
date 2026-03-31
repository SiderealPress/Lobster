"""
src/channels/outbox.py — OutboxFileHandler channel adapter (BIS-159 Slice 5).

Concrete ChannelAdapter implementation that writes reply dicts as JSON files
to a configurable outbox directory.  The file watcher process (bot/lobster_bot.py
or equivalent) picks them up and dispatches them to the correct transport.

This is the primary delivery mechanism for all channels: Telegram, WhatsApp,
SMS, Bisque relay, and Slack all read from a shared outbox directory (or
channel-specific subdirectories) and dispatch based on the source field.

Design:
  - Satisfies ChannelAdapter via structural subtyping (no explicit base class).
  - Uses atomic_write_json (write-to-temp + rename) so the watcher never sees
    a partial file.
  - Immutable after construction: the outbox_dir path is set at init time.
  - Thread-safe: atomic_write_json is re-entrant (each call creates its own
    tempfile in the same directory).

Extended (BIS-166 Slice 5+):
  - OutboxFileHandler also supports watchdog FileSystemEventHandler usage when
    constructed with source/send_fn/log kwargs (used by SMS, WhatsApp, Slack
    routers to dispatch replies from the shared outbox directory).
  - drain_outbox() processes any reply files already present at startup.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from utils.fs import atomic_write_json

try:
    from watchdog.events import FileSystemEventHandler as _FSEHandler
    _HAS_WATCHDOG = True
except ImportError:
    # Watchdog not installed — provide a no-op base so the class still loads.
    class _FSEHandler:  # type: ignore[no-redef]
        def dispatch(self, event: object) -> None:
            pass
    _HAS_WATCHDOG = False


class OutboxFileHandler(_FSEHandler):
    """Write reply dicts atomically to an outbox directory.

    Can be used in two modes:

    **Writer mode** (original BIS-159 interface):
        handler = OutboxFileHandler(outbox_dir=Path("/home/lobster/messages/outbox"))
        handler.write({"id": "12345_telegram", "chat_id": 6645894734, "text": "Hi"})

    **Watchdog mode** (BIS-166 router interface):
        Subclasses watchdog.FileSystemEventHandler so it can be passed directly
        to observer.schedule().  When a new .json file appears whose source
        field matches the configured *source*, the *send_fn* callable is invoked.

        watcher = OutboxFileHandler(source="sms", send_fn=send_sms, log=log)
        observer.schedule(watcher, str(OUTBOX_DIR), recursive=False)

    The file watcher polls the directory and dispatches files whose source
    field matches a registered transport handler.

    Args:
        outbox_dir: Path to the directory where reply JSON files are written
                    (writer mode).  Must exist and be writable; created lazily
                    on first write if it does not exist.
        source: Source tag to match (watchdog mode, e.g. "sms", "whatsapp").
        send_fn: Callable(reply_dict) -> bool called to deliver the reply
                 (watchdog mode).
        log: Logger to use (watchdog mode).
    """

    def __init__(
        self,
        outbox_dir: Path | None = None,
        *,
        source: str | None = None,
        send_fn: Callable[[dict], bool] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        super().__init__()
        self._outbox_dir = outbox_dir
        self._source = source
        self._send_fn = send_fn
        self._log = log or logging.getLogger(__name__)

    @property
    def outbox_dir(self) -> Path | None:
        """The outbox directory this handler writes to (writer mode)."""
        return self._outbox_dir

    def write(self, reply: dict) -> None:
        """Write *reply* as a JSON file in the outbox directory (writer mode).

        The filename is {reply['id']}.json.  If reply has no id
        field, raises KeyError.

        Uses write-to-temp-then-rename so the file watcher never observes a
        partial write.  The parent directory is created if absent.

        Args:
            reply: Reply dict.  Must have an id key.

        Raises:
            KeyError:  If reply does not contain an id field.
            OSError:   If the atomic write or directory creation fails.
        """
        if self._outbox_dir is None:
            raise RuntimeError("OutboxFileHandler.write() requires outbox_dir to be set")
        reply_id = reply["id"]  # Intentional KeyError if missing
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        dest = self._outbox_dir / f"{reply_id}.json"
        atomic_write_json(dest, reply)

    # ------------------------------------------------------------------
    # Watchdog FileSystemEventHandler interface
    # ------------------------------------------------------------------

    def on_created(self, event: object) -> None:
        """Called by watchdog when a new file appears in the watched directory."""
        if self._source is None or self._send_fn is None:
            return
        src_path = getattr(event, "src_path", None)
        if not src_path or not str(src_path).endswith(".json"):
            return
        self._deliver_file(Path(src_path))

    def _deliver_file(self, path: Path) -> None:
        """Read *path*, check source matches, call send_fn, delete on success."""
        try:
            with open(path) as f:
                reply = json.load(f)
        except Exception as exc:
            self._log.error(f"Failed to read outbox file {path}: {exc}")
            return

        if reply.get("source") != self._source:
            return  # Not for this channel

        try:
            ok = self._send_fn(reply)
        except Exception as exc:
            self._log.error(f"send_fn raised for {path}: {exc}")
            return

        if ok:
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:
                self._log.warning(f"Could not delete delivered file {path}: {exc}")
        else:
            self._log.warning(f"send_fn returned False for {path} — leaving in outbox")

    def __repr__(self) -> str:
        if self._outbox_dir is not None:
            return f"OutboxFileHandler(outbox_dir={self._outbox_dir!r})"
        return f"OutboxFileHandler(source={self._source!r})"


def drain_outbox(
    outbox_dir: Path,
    *,
    source: str,
    send_fn: Callable[[dict], bool],
    log: logging.Logger,
) -> None:
    """Process any reply JSON files already present in *outbox_dir* at startup.

    Scans *outbox_dir* for .json files whose source field matches *source*,
    calls *send_fn* for each, and deletes the file on success.

    This handles replies that arrived between the last shutdown and the current
    startup (the watchdog observer would not fire for pre-existing files).

    Args:
        outbox_dir: Directory to scan.
        source: Source tag to match (e.g. "sms", "whatsapp", "slack").
        send_fn: Callable(reply_dict) -> bool to deliver each reply.
        log: Logger instance.
    """
    handler = OutboxFileHandler(source=source, send_fn=send_fn, log=log)
    for path in sorted(outbox_dir.glob("*.json")):
        handler._deliver_file(path)


# ---------------------------------------------------------------------------
# Convenience alias — routers import OutboxWatcher for the watchdog role and
# OutboxFileHandler for the writer role to keep the two responsibilities
# clearly named at the call site.  They are the same class; both interfaces
# are implemented on OutboxFileHandler.
# ---------------------------------------------------------------------------
OutboxWatcher = OutboxFileHandler
