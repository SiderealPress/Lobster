#!/usr/bin/env python3
"""
Lobster WhatsApp Bridge Adapter — BIS-47

Bridges the whatsapp-bridge Node.js process to Lobster's inbox queue.

Architecture:
  1. Spawns `node index.js` in the bridge directory as a subprocess.
  2. Reads newline-delimited JSON events from the bridge's stdout.
  3. Normalizes each event to Lobster's inbox format and atomically writes
     it to ~/messages/inbox/{id}.json.
  4. Watches ~/messages/outbox/ for files with source="whatsapp" and
     delivers reply commands to ~/messages/wa-commands/{timestamp}_{id}.json.
  5. Monitors the bridge process and restarts it on exit (after 5s backoff).

Environment variables:
  WHATSAPP_BRIDGE_DIR   Path to the whatsapp-bridge project directory.
                        Default: ~/lobster-workspace/projects/whatsapp-bridge
  LOBSTER_MESSAGES      Path to the messages directory.
                        Default: ~/messages
  LOBSTER_WORKSPACE     Path to the lobster-workspace directory.
                        Default: ~/lobster-workspace

Usage:
  python3 whatsapp_bridge_adapter.py   # run directly
  from whatsapp_bridge_adapter import start  # import and call start()
"""

import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Thread
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))

BRIDGE_DIR = Path(
    os.environ.get(
        "WHATSAPP_BRIDGE_DIR",
        _WORKSPACE / "projects" / "whatsapp-bridge",
    )
)

INBOX_DIR = _MESSAGES / "inbox"
OUTBOX_DIR = _MESSAGES / "outbox"
WA_COMMANDS_DIR = _MESSAGES / "wa-commands"

RESTART_DELAY_SECONDS = 5

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------

for _d in [INBOX_DIR, OUTBOX_DIR, WA_COMMANDS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = _WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("lobster-whatsapp-bridge")
log.setLevel(logging.INFO)

_fh = RotatingFileHandler(
    LOG_DIR / "whatsapp-bridge-adapter.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
log.addHandler(logging.StreamHandler())

# ---------------------------------------------------------------------------
# Pure helpers — no side effects, fully testable in isolation
# ---------------------------------------------------------------------------


def make_msg_id(bridge_event_id: str) -> str:
    """Derive a stable inbox message ID from the bridge event ID."""
    ts_ms = int(time.time() * 1000)
    # Sanitize bridge ID: replace chars that are unsafe in filenames
    safe = bridge_event_id.replace("/", "_").replace(":", "_")[:64]
    return f"{ts_ms}_wa_{safe}"


def extract_lobster_id(bridge_event: dict) -> Optional[str]:
    """Return the 'user id' / author phone from a bridge event.

    For group messages the sender is in `author`; for 1:1 it is in `from`.
    """
    author = bridge_event.get("author")
    if author:
        return author
    return bridge_event.get("from")


def normalize_event(bridge_event: dict) -> Optional[dict]:
    """Convert a bridge stdout event dict to the Lobster inbox schema.

    Returns None if the event should be skipped (e.g. fromMe=True).

    Bridge event fields (from index.js):
      id, body, from, fromMe, isGroup, author, timestamp, mentionedIds,
      hasMedia, type

    Lobster inbox schema (required):
      id, source, chat_id, user_id, user_name, text, is_group,
      group_name, timestamp, mentions_lobster
    """
    # Skip messages sent by this session — they are our own outgoing replies
    if bridge_event.get("fromMe"):
        return None

    raw_id = str(bridge_event.get("id", ""))
    if not raw_id:
        log.warning("Received bridge event with empty id — skipping")
        return None

    body = bridge_event.get("body", "") or ""
    chat_id = bridge_event.get("from", "")
    is_group = bool(bridge_event.get("isGroup", False))
    author_id = extract_lobster_id(bridge_event)

    # Timestamp: bridge sends Unix seconds (integer); convert to ISO-8601
    raw_ts = bridge_event.get("timestamp")
    if raw_ts:
        try:
            dt = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
            iso_ts = dt.isoformat()
        except (ValueError, OSError):
            iso_ts = datetime.now(tz=timezone.utc).isoformat()
    else:
        iso_ts = datetime.now(tz=timezone.utc).isoformat()

    # Group name: we don't have it from the bridge event directly.
    # The chat_id for groups ends with @g.us (e.g. "120363xxxxxx@g.us").
    # We store the raw group JID as the group_name placeholder; a future
    # enhancement can resolve display names via the bridge's contact API.
    group_name = chat_id if is_group else ""

    # Determine if Lobster is mentioned (any mentionedIds present)
    mentioned_ids = bridge_event.get("mentionedIds", [])
    mentions_lobster = len(mentioned_ids) > 0

    msg_id = make_msg_id(raw_id)

    return {
        "id": msg_id,
        "source": "whatsapp",
        "chat_id": chat_id,
        "user_id": author_id or chat_id,
        "user_name": author_id or chat_id,
        "text": body,
        "is_group": is_group,
        "group_name": group_name,
        "timestamp": iso_ts,
        "mentions_lobster": mentions_lobster,
        # Extra fields that aid debugging (not part of core schema)
        "_bridge_id": raw_id,
        "_has_media": bridge_event.get("hasMedia", False),
        "_type": bridge_event.get("type", ""),
    }


def build_wa_command(chat_id: str, text: str) -> dict:
    """Build the wa-commands JSON payload for the bridge."""
    return {"action": "send", "to": chat_id, "text": text}


def build_command_filename(msg_id: str) -> str:
    """Build a timestamped filename for a wa-commands file."""
    ts = int(time.time() * 1000)
    return f"{ts}_{msg_id}.json"


# ---------------------------------------------------------------------------
# Atomic file I/O
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, data: dict, indent: int = 2) -> None:
    """Write JSON to *path* atomically via a sibling temp file + rename."""
    content = json.dumps(data, indent=indent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Inbox writer
# ---------------------------------------------------------------------------


def write_to_inbox(msg: dict) -> None:
    """Atomically write a normalized message to the inbox directory."""
    msg_id = msg["id"]
    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg)
    log.info(f"Wrote WhatsApp message to inbox: {msg_id} from={msg.get('chat_id')}")


# ---------------------------------------------------------------------------
# Outbox watcher — reads outbox files with source="whatsapp" and writes
# wa-commands for the bridge to deliver.
# ---------------------------------------------------------------------------


class WhatsAppOutboxHandler(FileSystemEventHandler):
    """Watches ~/messages/outbox/ and converts whatsapp replies to wa-commands."""

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith(".json"):
            return
        Thread(
            target=self._process,
            args=(event.src_path,),
            daemon=True,
        ).start()

    def on_moved(self, event):
        """Handle atomic writes that rename a .tmp to .json."""
        if event.is_directory or not event.dest_path.endswith(".json"):
            return
        Thread(
            target=self._process,
            args=(event.dest_path,),
            daemon=True,
        ).start()

    def _process(self, filepath: str) -> None:
        try:
            time.sleep(0.1)  # Brief pause to ensure the write is flushed
            with open(filepath, "r") as f:
                reply = json.load(f)
        except Exception as exc:
            log.error(f"Could not read outbox file {filepath}: {exc}")
            return

        if reply.get("source", "").lower() != "whatsapp":
            return

        chat_id = reply.get("chat_id", "")
        text = reply.get("text", "")

        if not chat_id or not text:
            log.warning(f"Invalid WhatsApp reply in {filepath}: missing chat_id or text")
            try:
                os.remove(filepath)
            except OSError:
                pass
            return

        # Derive a reply ID for the command filename
        reply_id = reply.get("id", Path(filepath).stem)
        cmd_filename = build_command_filename(reply_id)
        cmd_path = WA_COMMANDS_DIR / cmd_filename

        command = build_wa_command(chat_id, text)
        atomic_write_json(cmd_path, command)
        log.info(f"Wrote wa-command: {cmd_filename} → to={chat_id}")

        try:
            os.remove(filepath)
        except OSError as exc:
            log.warning(f"Could not remove outbox file {filepath}: {exc}")


def process_existing_outbox() -> None:
    """Deliver any whatsapp outbox files that accumulated before startup."""
    handler = WhatsAppOutboxHandler()
    for filepath in sorted(OUTBOX_DIR.glob("*.json")):
        try:
            with open(filepath, "r") as f:
                reply = json.load(f)
            if reply.get("source", "").lower() == "whatsapp":
                handler._process(str(filepath))
        except Exception as exc:
            log.error(f"Error draining outbox file {filepath}: {exc}")


# ---------------------------------------------------------------------------
# Bridge process manager
# ---------------------------------------------------------------------------


class BridgeRunner:
    """Manages the whatsapp-bridge subprocess lifecycle.

    Reads newline-delimited JSON from the bridge's stdout in a background
    thread.  Restarts the bridge process if it exits unexpectedly.
    """

    def __init__(self, bridge_dir: Path = BRIDGE_DIR) -> None:
        self._bridge_dir = bridge_dir
        self._process: Optional[subprocess.Popen] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the bridge and the stdout-reader thread."""
        self._running = True
        self._spawn_and_watch()

    def stop(self) -> None:
        """Signal the runner to stop and terminate the bridge process."""
        self._running = False
        if self._process and self._process.poll() is None:
            log.info("Terminating bridge process…")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _spawn_and_watch(self) -> None:
        """Spawn bridge; block reading stdout; restart if it dies."""
        while self._running:
            log.info(f"Starting whatsapp-bridge in {self._bridge_dir}")

            if not self._bridge_dir.exists():
                log.error(
                    f"Bridge directory not found: {self._bridge_dir}. "
                    "Set WHATSAPP_BRIDGE_DIR to the correct path."
                )
                if self._running:
                    time.sleep(RESTART_DELAY_SECONDS)
                continue

            try:
                self._process = subprocess.Popen(
                    ["node", "index.js"],
                    cwd=str(self._bridge_dir),
                    stdout=subprocess.PIPE,
                    stderr=None,  # Inherit stderr so QR codes / diagnostics show in our logs
                    text=True,
                    bufsize=1,  # Line-buffered
                )
                log.info(f"Bridge process started (pid={self._process.pid})")
                self._read_stdout(self._process)

            except FileNotFoundError:
                log.error(
                    "Could not start bridge: `node` not found in PATH. "
                    "Install Node.js >= 18."
                )
            except Exception as exc:
                log.error(f"Unexpected error starting bridge: {exc}")

            if self._running:
                log.warning(
                    f"Bridge exited. Restarting in {RESTART_DELAY_SECONDS}s…"
                )
                time.sleep(RESTART_DELAY_SECONDS)

        log.info("BridgeRunner stopped.")

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        """Read bridge stdout line-by-line, parse JSON, write to inbox."""
        for raw_line in proc.stdout:
            if not self._running:
                break

            line = raw_line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning(f"Non-JSON line from bridge (ignored): {line[:120]}")
                continue

            try:
                msg = normalize_event(event)
                if msg is not None:
                    write_to_inbox(msg)
            except Exception as exc:
                log.error(f"Error normalizing/writing bridge event: {exc} | raw={line[:200]}")

        # Reap the process once stdout closes
        proc.wait()
        exit_code = proc.returncode
        if exit_code != 0:
            log.error(f"Bridge exited with code {exit_code}")
        else:
            log.info("Bridge process exited cleanly.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def start() -> None:
    """Start the adapter: outbox watcher + bridge subprocess.

    This function blocks until KeyboardInterrupt or SIGTERM.
    It is both the module-level entry point and importable by Lobster's
    startup orchestrator.
    """
    log.info("Starting Lobster WhatsApp Bridge Adapter")
    log.info(f"Bridge dir : {BRIDGE_DIR}")
    log.info(f"Inbox      : {INBOX_DIR}")
    log.info(f"Outbox     : {OUTBOX_DIR}")
    log.info(f"WA commands: {WA_COMMANDS_DIR}")

    # 1. Drain any whatsapp replies already sitting in the outbox
    process_existing_outbox()

    # 2. Start outbox watcher (background thread)
    observer = Observer()
    observer.schedule(WhatsAppOutboxHandler(), str(OUTBOX_DIR), recursive=False)
    observer.daemon = True
    observer.start()
    log.info("Outbox watcher started.")

    # 3. Start bridge runner (blocks in its own loop; runs bridge as subprocess)
    runner = BridgeRunner()
    try:
        runner.start()  # blocks here
    except KeyboardInterrupt:
        log.info("Received KeyboardInterrupt — shutting down.")
    finally:
        runner.stop()
        observer.stop()
        observer.join()
        log.info("Adapter stopped.")


if __name__ == "__main__":
    start()
