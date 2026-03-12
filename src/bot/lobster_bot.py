#!/usr/bin/env python3
"""
Lobster Bot v2 - File-based message passing to master Claude session

Instead of spawning Claude processes, this bot:
1. Writes incoming messages to ~/messages/inbox/
2. Watches ~/messages/outbox/ for replies
3. Sends replies back to Telegram

The master Claude session processes inbox messages and writes to outbox.
"""

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import re
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes


def md_to_html(text: str) -> str:
    """Convert Telegram-flavored Markdown to HTML for reliable rendering.

    Handles: [text](url) links, `code`, ```code blocks```, **bold**, *bold*, _italic_
    Escapes &, <, > in non-HTML portions.
    """
    # Split on code blocks first to avoid formatting inside them
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Code span or block
            if part.startswith('```'):
                inner = part[3:]
                if inner.endswith('```'):
                    inner = inner[:-3]
                # Strip optional language tag on first line
                inner = re.sub(r'^\w+\n', '', inner)
                escaped = inner.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                result.append(f'<pre><code>{escaped}</code></pre>')
            else:
                inner = part[1:-1]
                escaped = inner.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                result.append(f'<code>{escaped}</code>')
        else:
            # Regular text — escape HTML entities first, then apply inline formatting
            p = part.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            # Links: [text](url)
            p = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', p)
            # Bold: **text** or __text__
            p = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', p)
            p = re.sub(r'__(.+?)__', r'<b>\1</b>', p)
            # Italic: _text_ (single, not double)
            p = re.sub(r'(?<![_*])_([^_\n]+)_(?![_*])', r'<i>\1</i>', p)
            result.append(p)
    return ''.join(result)

try:
    from onboarding import is_user_onboarded, mark_user_onboarded, get_onboarding_message
except ImportError:
    from src.bot.onboarding import is_user_onboarded, mark_user_onboarded, get_onboarding_message

# Configuration from environment
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS = [int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if x.strip()]

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
if not ALLOWED_USERS:
    raise ValueError("TELEGRAM_ALLOWED_USERS environment variable is required")

_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))

INBOX_DIR = _MESSAGES / "inbox"
OUTBOX_DIR = _MESSAGES / "outbox"
AUDIO_DIR = _MESSAGES / "audio"
IMAGES_DIR = _MESSAGES / "images"
DEAD_LETTER_DIR = _MESSAGES / "dead-letter"

# Hibernation state file - written by Claude when it hibernates
LOBSTER_STATE_FILE = _MESSAGES / "config" / "lobster-state.json"

# Script used to start a fresh Claude session (same as lobster-claude.service)
_REPO_DIR = Path(os.environ.get("LOBSTER_INSTALL_DIR", Path.home() / "lobster"))
CLAUDE_WAKE_SCRIPT = _REPO_DIR / "scripts" / "start-lobster.sh"

# Telegram message length limit.
# TELEGRAM_HARD_LIMIT is the API hard cap; no message may exceed it.
# TELEGRAM_MAX_LENGTH is a softer target used when splitting raw markdown.
# We use 4000 (not 4096) because md_to_html conversion expands text:
#   - HTML entities: & → &amp; (5 chars), < → &lt; (4), > → &gt; (4)
#   - Inline tags:   **bold** → <b>bold</b> (+7), _italic_ → <i>italic</i> (+7)
#   - Code blocks:   ```…``` → <pre><code>…</code></pre> (+24)
# Worst case a heavily-marked-up 4000-char markdown block can expand to ~4096
# HTML chars.  If a converted chunk still exceeds the hard limit, process_reply
# performs a second-pass split at a progressively tighter limit.
TELEGRAM_HARD_LIMIT = 4096
TELEGRAM_MAX_LENGTH = 4000


def _is_inside_code_block(text: str, pos: int) -> bool:
    """Return True if character position `pos` falls inside a triple-backtick block.

    We count the number of triple-backtick openers that precede `pos` in the
    text slice [0:pos]. An odd count means we are inside a code block.

    This is intentionally simple: it does not handle escaped backticks or
    nested backtick spans, which is consistent with how md_to_html works.
    """
    segment = text[:pos]
    # Count non-overlapping occurrences of ```
    count = len(re.findall(r'```', segment))
    return count % 2 == 1


def _find_code_block_end(text: str, start: int) -> int:
    """Return the position just after the closing ``` that closes the block
    opened before `start`, or -1 if not found."""
    close = text.find('```', start)
    if close == -1:
        return -1
    return close + 3  # position after the closing ```


def split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split a message into chunks that each fit within Telegram's character limit.

    Splitting strategy (highest priority first):
    1. Never split inside a triple-backtick code block. If the natural break
       point falls inside a code block, push the split to after the block ends
       (or before the block starts, whichever keeps chunks under the limit).
    2. Paragraph boundary (double newline).
    3. Single-newline boundary.
    4. Sentence boundary (". ", "! ", "? " followed by a capital or digit).
    5. Word boundary (last space before the limit).
    6. Hard split at max_length (last resort).

    Continuation labels: if the message is split, each chunk after the first
    is prefixed with "_(continued)_\\n\\n" so the reader knows it is a
    follow-on message.

    The function operates on raw markdown text. Callers are responsible for
    converting each returned chunk to HTML (via md_to_html) before sending.
    """
    if len(text) <= max_length:
        return [text]

    continuation_prefix = "_(continued)_\n\n"
    # Effective limit for continuation chunks (prefix eats some space)
    cont_max = max_length - len(continuation_prefix)

    chunks: list[str] = []
    remaining = text
    first_chunk = True

    while remaining:
        limit = max_length if first_chunk else cont_max

        if len(remaining) <= limit:
            chunk = remaining if first_chunk else continuation_prefix + remaining
            chunks.append(chunk)
            break

        # Determine candidate split position within [0, limit]
        split_pos = _find_clean_split(remaining, limit)

        raw_chunk = remaining[:split_pos].rstrip()
        chunk = raw_chunk if first_chunk else continuation_prefix + raw_chunk
        chunks.append(chunk)

        remaining = remaining[split_pos:].lstrip('\n')
        first_chunk = False

    return chunks


def _find_clean_split(text: str, limit: int) -> int:
    """Find the best position to split `text` at or before `limit` characters.

    Priority: avoid code blocks > paragraph break > newline > sentence > word > hard.
    Returns the index at which to cut (exclusive end of chunk).
    """
    # If the split point lands inside a code block, we need special handling.
    # Strategy: look for a split point just before the code block opens, or
    # after the code block closes — whichever is closer to `limit`.
    candidate = _best_text_split(text, limit)

    # Check if candidate splits inside a code block
    if _is_inside_code_block(text, candidate):
        # Find where the code block started
        block_start = text.rfind('```', 0, candidate)
        # Option A: split just before the code block (if block_start > 0)
        before_block = block_start if block_start > 0 else None

        # Option B: split after the code block closes
        block_end = _find_code_block_end(text, candidate)
        after_block = block_end if block_end != -1 and block_end <= len(text) else None

        if before_block is not None and before_block > 0:
            # Prefer splitting before the block; it keeps the block together
            return before_block
        elif after_block is not None:
            # Block end may exceed limit — that is acceptable to keep block intact
            return after_block
        # Fallback: hard split (block is pathologically large — just cut)

    return candidate


def _best_text_split(text: str, limit: int) -> int:
    """Find the best plain-text split point at or before `limit`.

    Does not check for code blocks — that is handled by the caller.
    Priority: paragraph > newline > sentence > word > hard.
    """
    # 1. Paragraph boundary
    pos = text.rfind('\n\n', 0, limit)
    if pos > 0:
        return pos + 2  # include the double newline in the consumed part

    # 2. Single newline
    pos = text.rfind('\n', 0, limit)
    if pos > 0:
        return pos + 1

    # 3. Sentence boundary: ". ", "! ", "? " where next char is upper or digit
    sentence_end = re.search(
        r'[.!?][ ]+(?=[A-Z0-9])',
        text[:limit]
    )
    # rfind the last sentence boundary in the window
    for match in re.finditer(r'[.!?][ ]+(?=[A-Z0-9])', text[:limit]):
        sentence_end = match
    if sentence_end:  # type: ignore[possibly-undefined]
        pos = sentence_end.end()
        if pos > 0:
            return pos

    # 4. Word boundary
    pos = text.rfind(' ', 0, limit)
    if pos > 0:
        return pos + 1

    # 5. Hard split
    return limit


def _prepare_send_items(text: str) -> list[tuple[str, str]]:
    """Split *text* into (markdown_chunk, html_chunk) pairs ready to send.

    Primary splitting is done on the raw markdown via split_message() using
    TELEGRAM_MAX_LENGTH (4000).  Because md_to_html() can expand text (HTML
    entities, inline tags, code-block wrappers), we perform a second-pass
    safety check: any HTML chunk that still exceeds TELEGRAM_HARD_LIMIT (4096)
    is re-split by tightening the markdown limit by 10 % and retrying, up to
    a minimum floor of 1000 characters.  This is an unusual edge case
    (requires very dense markup) but the loop guarantees we never send an
    oversized message to the API.
    """
    md_chunks = split_message(text)
    result: list[tuple[str, str]] = []

    for md_chunk in md_chunks:
        html_chunk = md_to_html(md_chunk)
        if len(html_chunk) <= TELEGRAM_HARD_LIMIT:
            result.append((md_chunk, html_chunk))
            continue

        # HTML exceeds the hard limit — re-split this markdown chunk at a
        # progressively tighter limit until the HTML fits.
        import logging as _logging
        _log = _logging.getLogger("lobster")
        tighter_limit = int(TELEGRAM_MAX_LENGTH * 0.9)
        floor = 1000
        sub_chunks: list[str] | None = None
        while tighter_limit >= floor:
            sub_chunks = split_message(md_chunk, max_length=tighter_limit)
            if all(len(md_to_html(s)) <= TELEGRAM_HARD_LIMIT for s in sub_chunks):
                break
            tighter_limit = int(tighter_limit * 0.9)
        else:
            # Floor reached — hard-truncate each sub-chunk as last resort
            sub_chunks = sub_chunks or [md_chunk]

        _log.warning(
            f"md_to_html expanded a {len(md_chunk)}-char markdown chunk to "
            f"{len(html_chunk)} HTML chars (>{TELEGRAM_HARD_LIMIT}); "
            f"re-split into {len(sub_chunks)} sub-chunks at limit={tighter_limit}"
        )
        for sub in sub_chunks:
            sub_html = md_to_html(sub)
            if len(sub_html) > TELEGRAM_HARD_LIMIT:
                # Absolute last resort: hard truncate the HTML
                sub_html = sub_html[:TELEGRAM_HARD_LIMIT - 3] + "..."
            result.append((sub, sub_html))

    return result


# Ensure directories exist
INBOX_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
DEAD_LETTER_DIR.mkdir(parents=True, exist_ok=True)
LOBSTER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Logging
LOG_DIR = _WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("lobster")
log.setLevel(logging.INFO)
_file_handler = RotatingFileHandler(
    LOG_DIR / "telegram-bot.log",
    maxBytes=5 * 1024 * 1024,  # 5MB
    backupCount=3,
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_file_handler)
log.addHandler(logging.StreamHandler())

# Global reference to the bot app and event loop for sending replies
bot_app = None
main_loop = None

# Tracks files currently being processed to prevent duplicate sends
_processing_files: set[str] = set()

# Lock to prevent concurrent wake attempts (race condition: two simultaneous
# incoming messages while hibernating should only trigger one Claude spawn)
_wake_lock = threading.Lock()

# Directory where MCP mark_processing moves messages
_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_PROCESSING_DIR = _MESSAGES_DIR / "processing"

# Media group buffering — Telegram sends each photo in a media group as a
# separate update with the same media_group_id. We buffer them and emit a
# single grouped inbox message after MEDIA_GROUP_FLUSH_DELAY seconds.
MEDIA_GROUP_FLUSH_DELAY = 2.0  # seconds to wait for all photos in a group

@dataclass
class _MediaGroupBuffer:
    """Accumulates photo updates for a single Telegram media group."""
    media_group_id: str
    chat_id: int
    user_id: int
    username: Optional[str]
    user_name: str
    caption: str = ""
    image_paths: list = field(default_factory=list)
    reply_ctx: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    flush_task: Optional[asyncio.Task] = None

# media_group_id -> _MediaGroupBuffer
_media_group_buffers: dict[str, _MediaGroupBuffer] = {}


async def send_typing_indicator(chat_id: int) -> None:
    """Send a Telegram 'typing...' indicator to chat_id.

    The indicator lasts ~5 seconds on the Telegram client side.
    Silently ignores failures (typing is best-effort).
    """
    if not bot_app:
        return
    try:
        await bot_app.bot.send_chat_action(chat_id=chat_id, action="typing")
        log.debug(f"Sent typing indicator to chat_id={chat_id}")
    except Exception as e:
        log.debug(f"Typing indicator failed for chat_id={chat_id}: {e}")


async def typing_refresh_loop() -> None:
    """Background task: refresh typing indicator every 4s for messages in processing/.

    Telegram's typing indicator expires after ~5 seconds, so we refresh at 4s
    to keep it visible while Lobster works on a long task.
    """
    log.info("Typing refresh loop started")
    while True:
        await asyncio.sleep(4)
        try:
            if not bot_app:
                continue
            # Scan all files in the processing directory
            if not _PROCESSING_DIR.exists():
                continue
            for msg_file in _PROCESSING_DIR.glob("*.json"):
                try:
                    data = json.loads(msg_file.read_text())
                    source = data.get("source", "")
                    chat_id = data.get("chat_id")
                    if source == "telegram" and chat_id:
                        await send_typing_indicator(int(chat_id))
                except Exception:
                    pass  # Skip corrupt/unreadable files silently
        except Exception as e:
            log.debug(f"Typing refresh loop error: {e}")


def _read_lobster_state() -> str:
    """Read current Lobster mode from state file.

    Returns 'active' or 'hibernate'. Defaults to 'active' on any error
    (missing file, corrupt JSON, unknown mode).
    """
    try:
        if not LOBSTER_STATE_FILE.exists():
            return "active"
        data = json.loads(LOBSTER_STATE_FILE.read_text())
        mode = data.get("mode", "active")
        return mode if mode in ("active", "hibernate") else "active"
    except Exception:
        return "active"


def _is_claude_running() -> bool:
    """Return True if a Claude process with --dangerously-skip-permissions is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude.*--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _read_lobster_state_data() -> dict:
    """Read full Lobster state data from state file.

    Returns the parsed dict, or an empty dict on any error.
    """
    try:
        if not LOBSTER_STATE_FILE.exists():
            return {}
        return json.loads(LOBSTER_STATE_FILE.read_text())
    except Exception:
        return {}


def _is_hibernate_stale(state_data: dict, max_age_seconds: int = 60) -> bool:
    """Return True if the hibernate state is stale (updated_at older than max_age_seconds).

    A stale hibernate state means Claude wrote "hibernate" but the CLI process
    never actually exited — it's a zombie that pgrep still finds.
    """
    updated_at = state_data.get("updated_at")
    if not updated_at:
        return True  # No timestamp means we can't trust it — treat as stale
    try:
        ts = datetime.fromisoformat(updated_at)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > max_age_seconds
    except Exception:
        return True  # Unparseable timestamp — treat as stale


def _kill_stale_claude() -> None:
    """Kill any stale Claude processes matching --dangerously-skip-permissions."""
    try:
        subprocess.run(
            ["pkill", "-f", "claude.*--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
        )
        log.info("wake_claude: sent pkill to stale Claude process(es)")
        time.sleep(3)  # Wait for process to die
    except Exception as e:
        log.warning(f"wake_claude: pkill failed: {e}")


def wake_claude_if_hibernating() -> None:
    """If Lobster is hibernating and Claude is not running, spawn a fresh session.

    Uses a threading lock so that concurrent calls (e.g. two messages arriving
    at the same time while hibernating) only trigger a single spawn.

    Handles stale hibernate state: if the state file says "hibernate" but the
    updated_at timestamp is older than 60 seconds, the Claude CLI process is
    likely a zombie (it wrote hibernate state but never exited). In this case,
    force-kill the old process before restarting.
    """
    state_data = _read_lobster_state_data()
    mode = state_data.get("mode", "active")
    if mode not in ("active", "hibernate"):
        mode = "active"

    # Fast path: if not hibernating, nothing to do
    if mode != "hibernate":
        return

    # Check if Claude process is running
    if _is_claude_running():
        # Claude process exists — but is it a zombie from stale hibernate?
        if _is_hibernate_stale(state_data):
            log.warning(
                "wake_claude: hibernate state is stale and Claude process still running — "
                "killing zombie process"
            )
            _kill_stale_claude()
        else:
            log.info("wake_claude: Claude already running despite hibernate state")
            return

    # Try to acquire the wake lock without blocking
    if not _wake_lock.acquire(blocking=False):
        log.info("wake_claude: another wake attempt is in progress, skipping")
        return

    try:
        # Re-check inside the lock to handle the TOCTOU window
        if _read_lobster_state() != "hibernate":
            return
        if _is_claude_running():
            log.info("wake_claude: Claude started before we could acquire lock")
            return

        log.info("wake_claude: Lobster is hibernating and Claude is not running — waking")

        # Reset state to "active" BEFORE spawning Claude.
        # This prevents restart storms: even if spawn fails, the state is no longer
        # "hibernate", so the health check won't skip its safety net.
        try:
            state_data = {"mode": "active", "woke_at": datetime.now(timezone.utc).isoformat()}
            tmp = LOBSTER_STATE_FILE.parent / f".lobster-state-wake-{os.getpid()}.tmp"
            tmp.write_text(json.dumps(state_data, indent=2))
            tmp.rename(LOBSTER_STATE_FILE)
            log.info("wake_claude: reset state to 'active'")
        except Exception as e:
            log.error(f"wake_claude: failed to reset state ({e}), proceeding with wake anyway")

        # Preferred: restart via systemd (keeps service state consistent)
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", "lobster-claude"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                log.info("wake_claude: 'systemctl restart lobster-claude' succeeded")
            else:
                log.error(f"wake_claude: systemctl restart exited {result.returncode}: {result.stderr.strip()}")
                raise RuntimeError("systemctl restart failed")
        except Exception as e:
            log.error(f"wake_claude: systemctl restart failed ({e}), trying start script")
            # Fallback: call start-lobster.sh directly
            if CLAUDE_WAKE_SCRIPT.exists():
                subprocess.Popen(
                    ["bash", str(CLAUDE_WAKE_SCRIPT)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                log.info(f"wake_claude: spawned {CLAUDE_WAKE_SCRIPT}")
            else:
                log.error(f"wake_claude: fallback script not found: {CLAUDE_WAKE_SCRIPT}")
    finally:
        _wake_lock.release()


def atomic_write_json(path: Path, data: dict, indent: int = 2) -> None:
    """Atomically write JSON to a file (write-to-temp-then-rename).

    On POSIX systems, rename() within the same filesystem is atomic,
    so readers never see a partial file.
    """
    content = json.dumps(data, indent=indent)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class OutboxHandler(FileSystemEventHandler):
    """Watches outbox for reply files and sends them via Telegram."""

    def _schedule_processing(self, filepath):
        if filepath.endswith('.json') and not filepath.endswith('.tmp'):
            if bot_app and main_loop and main_loop.is_running():
                if filepath not in _processing_files:
                    _processing_files.add(filepath)
                    asyncio.run_coroutine_threadsafe(
                        self.process_reply(filepath),
                        main_loop
                    )

    def on_created(self, event):
        if event.is_directory:
            return
        self._schedule_processing(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._schedule_processing(event.src_path)

    async def process_reply(self, filepath):
        try:
            await asyncio.sleep(0.5)  # Delay to ensure file write is complete
            with open(filepath, 'r') as f:
                reply = json.load(f)

            chat_id = reply.get('chat_id')
            text = reply.get('text', '')
            buttons = reply.get('buttons')

            if chat_id and text and bot_app:
                reply_markup = build_inline_keyboard(buttons) if buttons else None
                send_items = _prepare_send_items(text)
                n = len(send_items)
                for i, (md_chunk, html_chunk) in enumerate(send_items):
                    # Only attach inline keyboard to the final chunk
                    chunk_markup = reply_markup if i == n - 1 else None
                    try:
                        await bot_app.bot.send_message(
                            chat_id=chat_id,
                            text=html_chunk,
                            parse_mode="HTML",
                            reply_markup=chunk_markup
                        )
                    except Exception as exc:
                        # Fallback to plain text if HTML parsing fails.
                        # Plain text is also subject to the hard limit so we
                        # truncate as a last resort rather than crash/drop.
                        plain = md_chunk
                        if len(plain) > TELEGRAM_HARD_LIMIT:
                            plain = plain[:TELEGRAM_HARD_LIMIT - 3] + "..."
                            log.warning(
                                f"HTML send failed for {chat_id}, falling back to "
                                f"truncated plain text ({len(md_chunk)} chars): {exc}"
                            )
                        await bot_app.bot.send_message(
                            chat_id=chat_id,
                            text=plain,
                            reply_markup=chunk_markup
                        )
                if n > 1:
                    log.info(f"Sent reply to {chat_id} in {n} chunks: {text[:50]}...")
                else:
                    log.info(f"Sent reply to {chat_id}: {text[:50]}...")
                os.remove(filepath)
            else:
                log.warning(f"Skipping reply {filepath}: missing chat_id={chat_id}, text={bool(text)}, bot={bool(bot_app)}")
                os.remove(filepath)
        finally:
            _processing_files.discard(filepath)


async def process_existing_outbox():
    """Process any outbox files that exist on startup."""
    handler = OutboxHandler()
    existing_files = list(OUTBOX_DIR.glob("*.json"))
    if existing_files:
        log.info(f"Processing {len(existing_files)} existing outbox file(s)...")
        for filepath in existing_files:
            try:
                await handler.process_reply(str(filepath))
            except Exception as e:
                log.error(f"Error processing existing outbox file {filepath}: {e}")


_outbox_fail_counts: dict[str, int] = {}


async def sweep_outbox():
    """Periodic sweep catches files missed by watchdog or failed on first attempt."""
    handler = OutboxHandler()
    while True:
        await asyncio.sleep(10)
        try:
            for filepath in sorted(OUTBOX_DIR.glob("*.json")):
                # Skip temp files from atomic writes
                if filepath.suffix == '.tmp':
                    continue
                # Only process files older than 2 seconds (ensure write completion)
                try:
                    age = time.time() - filepath.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age < 2:
                    continue

                fname = str(filepath)
                if fname in _processing_files:
                    continue
                _processing_files.add(fname)
                try:
                    await handler.process_reply(fname)
                    _outbox_fail_counts.pop(fname, None)
                except Exception as e:
                    _outbox_fail_counts[fname] = _outbox_fail_counts.get(fname, 0) + 1
                    count = _outbox_fail_counts[fname]
                    log.error(f"Sweep: failed to process {filepath.name} (attempt {count}/5): {e}")
                    if count >= 5:
                        dest = DEAD_LETTER_DIR / filepath.name
                        shutil.move(fname, str(dest))
                        _outbox_fail_counts.pop(fname, None)
                        log.error(f"Moved to dead-letter after 5 failures: {filepath.name}")
        except Exception as e:
            log.error(f"Outbox sweep error: {e}")


def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USERS


def extract_reply_to_context(message) -> dict | None:
    """Extract full reply-to context from a Telegram message, if it is a reply.

    Returns a dict with:
      - reply_to_message_id: int — the Telegram message ID of the replied-to message
      - reply_to_type: str — "text", "photo", "voice", "document", "sticker", etc.
      - reply_to_text: str | None — full text or caption of the replied-to message
      - reply_to_from_user: str | None — username of the sender of the replied-to message

    Returns None if the message is not a reply.
    """
    if not message.reply_to_message:
        return None

    reply = message.reply_to_message

    # Determine the type of the replied-to message
    if reply.text:
        reply_type = "text"
        reply_text = reply.text
    elif reply.caption:
        # Photos, documents, videos, etc. with a caption
        if reply.photo:
            reply_type = "photo"
        elif reply.document:
            reply_type = "document"
        elif reply.video:
            reply_type = "video"
        elif reply.audio:
            reply_type = "audio"
        else:
            reply_type = "media"
        reply_text = reply.caption
    elif reply.voice:
        reply_type = "voice"
        reply_text = None
    elif reply.photo:
        reply_type = "photo"
        reply_text = None
    elif reply.document:
        reply_type = "document"
        reply_text = None
    elif reply.video:
        reply_type = "video"
        reply_text = None
    elif reply.audio:
        reply_type = "audio"
        reply_text = None
    elif reply.sticker:
        reply_type = "sticker"
        reply_text = reply.sticker.emoji if reply.sticker.emoji else None
    else:
        reply_type = "unknown"
        reply_text = None

    from_user = reply.from_user.username if reply.from_user else None

    return {
        "message_id": reply.message_id,
        "reply_to_message_id": reply.message_id,
        "reply_to_type": reply_type,
        "reply_to_text": reply_text,
        "reply_to_from_user": from_user,
        # Keep legacy "text" field for backwards compatibility
        "text": reply_text,
        "from_user": from_user,
    }


def build_inline_keyboard(buttons: list) -> InlineKeyboardMarkup | None:
    """
    Build an InlineKeyboardMarkup from a buttons specification.

    Supported formats:
    1. Simple row format: [["Button 1", "Button 2"], ["Button 3"]]
       - Each string becomes a button with text=callback_data

    2. Object format: [[{"text": "Option A", "callback_data": "opt_a"}], ...]
       - Explicit text and callback_data per button

    3. Mixed format: [["Simple"], [{"text": "Complex", "callback_data": "complex"}]]
    """
    if not buttons or not isinstance(buttons, list):
        return None

    keyboard = []
    for row in buttons:
        if not isinstance(row, list):
            continue
        keyboard_row = []
        for button in row:
            if isinstance(button, str):
                # Simple format: text is also the callback_data
                keyboard_row.append(InlineKeyboardButton(text=button, callback_data=button))
            elif isinstance(button, dict):
                # Object format: explicit text and callback_data
                text = button.get('text', '')
                callback_data = button.get('callback_data', text)
                if text:
                    keyboard_row.append(InlineKeyboardButton(text=text, callback_data=callback_data))
        if keyboard_row:
            keyboard.append(keyboard_row)

    return InlineKeyboardMarkup(keyboard) if keyboard else None


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    user = query.from_user

    if not is_authorized(user.id):
        await query.answer("Unauthorized", show_alert=True)
        return

    # Wake Claude if it hibernated
    wake_claude_if_hibernating()

    # Acknowledge the button press
    await query.answer()

    msg_id = f"{int(time.time() * 1000)}_{query.id}"
    callback_data = query.data

    # Create message file in inbox for the button press
    msg_data = {
        "id": msg_id,
        "source": "telegram",
        "type": "callback",
        "chat_id": query.message.chat_id,
        "user_id": user.id,
        "username": user.username,
        "user_name": user.first_name,
        "text": f"[Button pressed: {callback_data}]",
        "callback_data": callback_data,
        "callback_query_id": query.id,
        "original_message_id": query.message.message_id,
        "original_message_text": query.message.text,
        "timestamp": datetime.utcnow().isoformat(),
    }

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)

    log.info(f"Button press from {user.first_name}: {callback_data}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    # /start triggers onboarding for new users, otherwise shows short greeting
    if not is_user_onboarded(user.id):
        await send_onboarding(update, user)
    else:
        await update.message.reply_text(
            f"👋 Hey {user.first_name}!\n\n"
            "I'm Lobster. Messages you send here go to the master Claude session.\n\n"
            "The session will process them and reply back here."
        )


async def onboarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /onboarding command — always shows the full onboarding message."""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    await send_onboarding(update, user)


async def send_onboarding(update: Update, user) -> None:
    """Send the onboarding message and mark the user as onboarded."""
    message_text = get_onboarding_message(user.first_name)
    try:
        await update.message.reply_text(md_to_html(message_text), parse_mode="HTML")
    except Exception:
        await update.message.reply_text(message_text)
    mark_user_onboarded(user.id)
    log.info(f"Sent onboarding to user {user.id} ({user.first_name})")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not is_authorized(user.id):
        log.warning(f"Unauthorized: {user.id}")
        return

    # Wake Claude if it hibernated — do this before writing to inbox so Claude
    # is starting up while the message file is being written
    wake_claude_if_hibernating()

    # Send typing indicator immediately so the user sees Lobster is working
    await send_typing_indicator(message.chat_id)

    # First-message detection: send onboarding to new users
    if not is_user_onboarded(user.id):
        await send_onboarding(update, user)

    msg_id = f"{int(time.time() * 1000)}_{message.message_id}"

    # Handle voice messages
    if message.voice:
        await handle_voice_message(update, context, msg_id)
        return

    # Handle photo messages
    if message.photo:
        await handle_photo_message(update, context, msg_id)
        return

    # Handle document/file messages (including images sent as files)
    if message.document:
        await handle_document_message(update, context, msg_id)
        return

    text = message.text
    if not text:
        return

    # Create message file in inbox
    msg_data = {
        "id": msg_id,
        "source": "telegram",
        "chat_id": message.chat_id,
        "user_id": user.id,
        "username": user.username,
        "user_name": user.first_name,
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Capture full reply-to context if this message is a reply
    reply_ctx = extract_reply_to_context(message)
    if reply_ctx:
        msg_data["reply_to"] = reply_ctx

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)

    log.info(f"Wrote message to inbox: {msg_id}")

    # Send acknowledgment
    await message.reply_text("📨 Message received. Processing...")


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle voice messages: download audio and save to inbox with metadata."""
    user = update.effective_user
    message = update.message
    voice = message.voice

    # Send typing indicator immediately (wake already called in handle_message)
    await send_typing_indicator(message.chat_id)

    try:
        # Download voice file from Telegram
        file = await context.bot.get_file(voice.file_id)
        audio_filename = f"{msg_id}.ogg"
        audio_path = AUDIO_DIR / audio_filename

        await file.download_to_drive(audio_path)
        log.info(f"Downloaded voice message to: {audio_path}")

        # Create message file in inbox with voice metadata
        msg_data = {
            "id": msg_id,
            "source": "telegram",
            "type": "voice",
            "chat_id": message.chat_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": "[Voice message - pending transcription]",
            "audio_file": str(audio_path),
            "audio_duration": voice.duration,
            "audio_mime_type": voice.mime_type or "audio/ogg",
            "file_id": voice.file_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Capture full reply-to context if this message is a reply
        reply_ctx = extract_reply_to_context(message)
        if reply_ctx:
            msg_data["reply_to"] = reply_ctx

        inbox_file = INBOX_DIR / f"{msg_id}.json"
        atomic_write_json(inbox_file, msg_data)

        log.info(f"Wrote voice message to inbox: {msg_id}")
        await message.reply_text("🎤 Voice message received. Transcribing...")

    except Exception as e:
        log.error(f"Error handling voice message: {e}")
        await message.reply_text("❌ Failed to process voice message.")


async def _flush_media_group(media_group_id: str, chat_id: int) -> None:
    """Flush a buffered media group to the inbox as a single grouped message.

    Called after MEDIA_GROUP_FLUSH_DELAY seconds, at which point all photos
    in the group should have arrived and been downloaded.
    """
    await asyncio.sleep(MEDIA_GROUP_FLUSH_DELAY)

    buf = _media_group_buffers.pop(media_group_id, None)
    if buf is None:
        return  # Already flushed or never existed

    if not buf.image_paths:
        log.warning(f"Media group {media_group_id} flushed with no images — skipping")
        return

    # Stable ID based on group id (reproducible, not time-dependent)
    group_msg_id = f"grp_{media_group_id}"

    image_count = len(buf.image_paths)
    caption = buf.caption
    text_field = caption if caption else f"[{image_count} photos - see image_files]"

    if image_count == 1:
        # Degenerate group — treat as single photo
        msg_data = {
            "id": group_msg_id,
            "source": "telegram",
            "type": "photo",
            "chat_id": buf.chat_id,
            "user_id": buf.user_id,
            "username": buf.username,
            "user_name": buf.user_name,
            "text": caption if caption else "[Photo - see image_file]",
            "image_file": buf.image_paths[0],
            "media_group_id": media_group_id,
            "timestamp": datetime.utcnow().isoformat(),
        }
    else:
        msg_data = {
            "id": group_msg_id,
            "source": "telegram",
            "type": "photo",
            "chat_id": buf.chat_id,
            "user_id": buf.user_id,
            "username": buf.username,
            "user_name": buf.user_name,
            "text": text_field,
            "image_files": buf.image_paths,
            "image_count": image_count,
            "media_group_id": media_group_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

    if buf.reply_ctx:
        msg_data["reply_to"] = buf.reply_ctx

    inbox_file = INBOX_DIR / f"{group_msg_id}.json"
    atomic_write_json(inbox_file, msg_data)

    log.info(
        f"Flushed media group {media_group_id} to inbox: "
        f"{image_count} photo(s), id={group_msg_id}"
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle photo messages: download image and save to inbox with metadata.

    Single photos are written immediately. Photos that belong to a media group
    (identified by message.media_group_id) are buffered for MEDIA_GROUP_FLUSH_DELAY
    seconds, then emitted as a single inbox message with an image_files array.
    """
    user = update.effective_user
    message = update.message

    # Send typing indicator immediately (wake already called in handle_message)
    await send_typing_indicator(message.chat_id)

    try:
        # Get the largest photo (last in the array — Telegram orders by resolution)
        photo = message.photo[-1]

        # Download photo file from Telegram
        file = await context.bot.get_file(photo.file_id)
        image_filename = f"{msg_id}.jpg"
        image_path = IMAGES_DIR / image_filename

        await file.download_to_drive(image_path)
        log.info(f"Downloaded photo to: {image_path}")

        caption = message.caption or ""
        reply_ctx = extract_reply_to_context(message)
        media_group_id = message.media_group_id  # None for single photos

        if media_group_id:
            # --- Media group path ---
            # Add this photo to (or create) the buffer for this group
            if media_group_id not in _media_group_buffers:
                buf = _MediaGroupBuffer(
                    media_group_id=media_group_id,
                    chat_id=message.chat_id,
                    user_id=user.id,
                    username=user.username,
                    user_name=user.first_name,
                    reply_ctx=reply_ctx,
                )
                _media_group_buffers[media_group_id] = buf

                # Schedule a flush after the delay. We cancel and reschedule
                # on each new photo so the timer resets if photos trickle in.
                buf.flush_task = asyncio.create_task(
                    _flush_media_group(media_group_id, message.chat_id)
                )
                log.info(f"Started media group buffer for group_id={media_group_id}")
                # Send a single acknowledgment for the whole group
                await message.reply_text("📷 Media group received. Processing...")
            else:
                buf = _media_group_buffers[media_group_id]
                # Cancel the existing flush task and reschedule so we wait
                # for any remaining photos before flushing
                if buf.flush_task and not buf.flush_task.done():
                    buf.flush_task.cancel()
                buf.flush_task = asyncio.create_task(
                    _flush_media_group(media_group_id, message.chat_id)
                )

            # Capture caption from whichever photo has it (only one does)
            if caption and not buf.caption:
                buf.caption = caption
            # Capture reply context from first photo
            if reply_ctx and not buf.reply_ctx:
                buf.reply_ctx = reply_ctx

            buf.image_paths.append(str(image_path))
            log.info(
                f"Added photo to media group {media_group_id}: "
                f"{len(buf.image_paths)} photo(s) so far"
            )

        else:
            # --- Single photo path ---
            msg_data = {
                "id": msg_id,
                "source": "telegram",
                "type": "photo",
                "chat_id": message.chat_id,
                "user_id": user.id,
                "username": user.username,
                "user_name": user.first_name,
                "text": caption if caption else "[Photo - see image_file]",
                "image_file": str(image_path),
                "image_width": photo.width,
                "image_height": photo.height,
                "file_id": photo.file_id,
                "timestamp": datetime.utcnow().isoformat(),
            }

            if reply_ctx:
                msg_data["reply_to"] = reply_ctx

            inbox_file = INBOX_DIR / f"{msg_id}.json"
            atomic_write_json(inbox_file, msg_data)

            log.info(f"Wrote photo message to inbox: {msg_id}")
            await message.reply_text("📷 Image received. Processing...")

    except Exception as e:
        log.error(f"Error handling photo message: {e}")
        await message.reply_text("❌ Failed to process image.")


async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle document messages: download file and save to inbox with metadata."""
    user = update.effective_user
    message = update.message
    document = message.document

    # Send typing indicator immediately (wake already called in handle_message)
    await send_typing_indicator(message.chat_id)

    try:
        # Check if it's an image sent as document
        mime_type = document.mime_type or ""
        is_image = mime_type.startswith("image/")
        original_name = document.file_name or "file"
        file_size_mb = (document.file_size or 0) / (1024 * 1024)

        log.info(f"Receiving document: name={original_name}, mime={mime_type}, size={file_size_mb:.1f}MB, file_id={document.file_id}")

        if file_size_mb > 20:
            log.warning(f"File {original_name} is {file_size_mb:.1f}MB — exceeds Telegram Bot API 20MB download limit")
            await message.reply_text(f"⚠️ File too large ({file_size_mb:.1f}MB). Telegram Bot API limit is 20MB. Please send a smaller file or upload it elsewhere and share a link.")
            return

        # Download file from Telegram
        file = await context.bot.get_file(document.file_id)

        # Determine extension and save location
        ext = Path(original_name).suffix or (".jpg" if is_image else "")

        if is_image:
            save_path = IMAGES_DIR / f"{msg_id}{ext}"
        else:
            # For non-images, save to a general files directory
            files_dir = _MESSAGES / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            save_path = files_dir / f"{msg_id}{ext}"

        await file.download_to_drive(save_path)
        log.info(f"Downloaded document to: {save_path}")

        # Get caption if any
        caption = message.caption or ""

        # Create message file in inbox
        msg_data = {
            "id": msg_id,
            "source": "telegram",
            "type": "image" if is_image else "document",
            "chat_id": message.chat_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": caption if caption else f"[Document: {original_name}]",
            "file_path": str(save_path),
            "file_name": original_name,
            "mime_type": mime_type,
            "file_size": document.file_size,
            "file_id": document.file_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if is_image:
            msg_data["image_file"] = str(save_path)

        # Capture full reply-to context if this message is a reply
        reply_ctx = extract_reply_to_context(message)
        if reply_ctx:
            msg_data["reply_to"] = reply_ctx

        inbox_file = INBOX_DIR / f"{msg_id}.json"
        atomic_write_json(inbox_file, msg_data)

        log.info(f"Wrote document message to inbox: {msg_id}")
        emoji = "📷" if is_image else "📎"
        await message.reply_text(f"{emoji} File received. Processing...")

    except Exception as e:
        file_name = document.file_name or "unknown"
        file_size_mb = (document.file_size or 0) / (1024 * 1024)
        log.error(f"Error handling document: name={file_name}, size={file_size_mb:.1f}MB, error={type(e).__name__}: {e}", exc_info=True)
        await message.reply_text(f"❌ Failed to process file: {type(e).__name__}. Check logs for details.")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Error: {context.error}", exc_info=context.error)


async def run_bot():
    global bot_app, main_loop

    log.info("Starting Lobster Bot v2 (file-based)...")
    log.info(f"Inbox: {INBOX_DIR}")
    log.info(f"Outbox: {OUTBOX_DIR}")

    # Store the event loop for the outbox watcher
    main_loop = asyncio.get_running_loop()

    # Set up outbox watcher
    observer = Observer()
    observer.schedule(OutboxHandler(), str(OUTBOX_DIR), recursive=False)
    observer.start()
    log.info("Watching outbox for replies...")

    # Create bot application
    bot_app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("onboarding", onboarding_command))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.add_handler(MessageHandler(filters.VOICE, handle_message))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    bot_app.add_handler(CallbackQueryHandler(handle_callback_query))
    bot_app.add_error_handler(error_handler)

    # Initialize and start
    await bot_app.initialize()
    await bot_app.start()
    log.info("Bot is now polling...")

    # Process any existing outbox files from before startup
    await process_existing_outbox()

    # Start periodic outbox sweep (catches watchdog misses and retries failures)
    asyncio.create_task(sweep_outbox())
    log.info("Outbox sweep task started (every 10s)")

    # Start typing indicator refresh loop (keeps "typing..." visible during long tasks)
    asyncio.create_task(typing_refresh_loop())
    log.info("Typing indicator refresh loop started (every 4s)")

    try:
        await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        # Keep running until interrupted
        while True:
            await asyncio.sleep(1)
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        observer.stop()
        observer.join()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
