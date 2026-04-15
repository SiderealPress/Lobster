"""
MessageStore: encapsulates all message read/write/move operations.

This module extracts the filesystem-level message state machine from
inbox_server.py into a single cohesive class.  The dispatcher and MCP
tool handlers interact with messages exclusively through this class,
keeping all path manipulation and JSON I/O in one place.

Design principles:
- Pure methods: each method has a single, clearly-named side effect
- No global mutable state: all paths are injected via the constructor
- Failures are explicit: callers receive None or False on not-found,
  OSError/json.JSONDecodeError on genuine I/O errors
- Idempotent moves: FileNotFoundError on rename is treated as success
  (another worker already moved the file)
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from utils.fs import atomic_write_json  # noqa: E402

log = logging.getLogger("lobster-mcp")


class MessageStore:
    """Encapsulates all message filesystem operations.

    All state transitions (inbox -> processing -> processed/failed) are
    performed through this class.  The MCP tool handlers call these methods
    rather than manipulating paths directly.

    Args:
        inbox_dir: Directory for incoming messages waiting to be processed.
        processing_dir: Directory for messages currently being processed.
        processed_dir: Directory for successfully handled messages.
        failed_dir: Directory for messages that failed processing.
        outbox_dir: Directory for outgoing replies.
        sent_dir: Directory for a copy of every sent reply (conversation history).
    """

    def __init__(
        self,
        inbox_dir: Path,
        processing_dir: Path,
        processed_dir: Path,
        failed_dir: Path,
        outbox_dir: Path,
        sent_dir: Path,
    ) -> None:
        self.inbox_dir = inbox_dir
        self.processing_dir = processing_dir
        self.processed_dir = processed_dir
        self.failed_dir = failed_dir
        self.outbox_dir = outbox_dir
        self.sent_dir = sent_dir

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def find_message_file(self, directory: Path, message_id: str) -> Path | None:
        """Find a message file in *directory* by message ID.

        Matches on filename substring first (fast path), then falls back
        to reading each JSON file and comparing the ``"id"`` field.

        Returns the matching Path, or None if not found.
        """
        for f in directory.glob("*.json"):
            if message_id in f.name:
                return f
            try:
                with open(f) as fp:
                    msg = json.load(fp)
                if msg.get("id") == message_id:
                    return f
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def read_message(self, path: Path) -> dict[str, Any]:
        """Read and parse a message JSON file.

        Raises:
            OSError: If the file cannot be read.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        return json.loads(path.read_text())

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def move_to_processing(self, msg_path: Path) -> Path:
        """Move a message from its current directory to processing/.

        Returns the new path in processing_dir.

        Raises:
            FileNotFoundError: If the source file is already gone (idempotent —
                another worker claimed it first).
            OSError: On any other rename failure.
        """
        dest = self.processing_dir / msg_path.name
        msg_path.rename(dest)
        return dest

    def move_to_processed(self, msg_path: Path) -> Path:
        """Move a message from its current directory to processed/.

        Returns the new path in processed_dir.

        Raises:
            FileNotFoundError: If the source file is already gone.
            OSError: On any other rename failure.
        """
        dest = self.processed_dir / msg_path.name
        msg_path.rename(dest)
        return dest

    def move_to_failed(self, msg_path: Path, msg_data: dict[str, Any]) -> Path:
        """Atomically write updated *msg_data* to failed/ and remove the source.

        Uses write-then-unlink ordering: if the process crashes after writing
        but before unlinking, a duplicate exists in failed/ which is harmless.
        The reverse (unlink then write) would lose the message.

        Returns the new path in failed_dir.

        Raises:
            OSError / TypeError: If the atomic write fails.
        """
        dest = self.failed_dir / msg_path.name
        atomic_write_json(dest, msg_data)
        msg_path.unlink(missing_ok=True)
        return dest

    def move_to_inbox(self, msg_path: Path) -> Path:
        """Move a message back to inbox/ (used by stale and retry recovery).

        Returns the new path in inbox_dir.

        Raises:
            FileNotFoundError: If the source file is already gone.
            OSError: On any other rename failure.
        """
        dest = self.inbox_dir / msg_path.name
        msg_path.rename(dest)
        return dest

    def stamp_processing_start(self, msg_path: Path, msg_data: dict[str, Any]) -> None:
        """Write _processing_started_at timestamp into the message file.

        Non-fatal: any failure is logged but does not propagate.  Stale
        detection falls back to file mtime when this field is absent.
        """
        try:
            msg_data["_processing_started_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(msg_path, msg_data)
        except (OSError, TypeError) as exc:
            log.warning(f"stamp_processing_start: failed to write timestamp: {exc}")

    # ------------------------------------------------------------------
    # Stale and retry recovery
    # ------------------------------------------------------------------

    @staticmethod
    def stale_timeout_for_message(msg: dict[str, Any]) -> int:
        """Return the stale processing timeout in seconds based on message type.

        Text messages are expected to complete quickly; media types (voice,
        photo, document) may take longer due to transcription or download.
        """
        slow_types = {"voice", "photo", "document"}
        msg_type = msg.get("type", "text")
        return 300 if msg_type in slow_types else 90

    def recover_stale_processing(self) -> None:
        """Move stale messages from processing/ back to inbox/.

        Uses a type-aware timeout: 90 s for text, 300 s for media.
        Skips files that cannot be read or renamed (already moved by another
        worker).
        """
        now = time.time()
        for f in self.processing_dir.glob("*.json"):
            try:
                age = now - f.stat().st_mtime
                msg = json.loads(f.read_text())
                max_age = self.stale_timeout_for_message(msg)
                if age > max_age:
                    dest = self.inbox_dir / f.name
                    f.rename(dest)
                    log.warning(
                        f"Recovered stale message from processing: {f.name} "
                        f"(type: {msg.get('type', 'text')}, age: {int(age)}s, "
                        f"timeout: {max_age}s)"
                    )
            except (OSError, json.JSONDecodeError):
                continue

    def recover_retryable_messages(self) -> None:
        """Move retry-eligible messages from failed/ back to inbox/.

        Skips permanently-failed messages and those whose retry window has
        not yet elapsed.
        """
        now = time.time()
        for f in self.failed_dir.glob("*.json"):
            try:
                msg = json.loads(f.read_text())
                if msg.get("_permanently_failed"):
                    continue
                retry_at = msg.get("_retry_at", 0)
                if now >= retry_at:
                    dest = self.inbox_dir / f.name
                    f.rename(dest)
                    log.info(
                        f"Re-queued retryable message: {f.name} "
                        f"(retry_count: {msg.get('_retry_count', '?')})"
                    )
            except (OSError, json.JSONDecodeError):
                continue

    # ------------------------------------------------------------------
    # Outbox helpers
    # ------------------------------------------------------------------

    def write_reply(self, reply_data: dict[str, Any], outbox_dir: Path | None = None) -> Path:
        """Atomically write a reply to the outbox directory.

        Args:
            reply_data: The reply payload.  Must contain ``"id"`` field.
            outbox_dir: Override outbox directory (e.g. bisque-outbox).
                        Defaults to self.outbox_dir.

        Returns the path the reply was written to.
        """
        dest_dir = outbox_dir if outbox_dir is not None else self.outbox_dir
        reply_id = reply_data["id"]
        outbox_file = dest_dir / f"{reply_id}.json"
        atomic_write_json(outbox_file, reply_data)
        return outbox_file

    def archive_sent_reply(self, reply_data: dict[str, Any]) -> Path:
        """Write a copy of a sent reply to sent/ for conversation history.

        Returns the path in sent_dir.
        """
        reply_id = reply_data["id"]
        sent_file = self.sent_dir / f"{reply_id}.json"
        atomic_write_json(sent_file, reply_data)
        return sent_file
