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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in _sys.path:
    _sys.path.insert(0, _SRC_DIR)
from utils.fs import atomic_write_json  # noqa: E402
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# multiplayer-telegram-bot skill — soft import; enables group whitelist management
# and group management commands.  Three levels up from src/bot/lobster_bot.py
# lands at the repo root (~/lobster/), then lobster-shop/ is a subdirectory there.
_SKILL_DIR = str(Path(__file__).resolve().parent.parent.parent /
                 "lobster-shop" / "multiplayer-telegram-bot" / "src")
if _SKILL_DIR not in _sys.path:
    _sys.path.insert(0, _SKILL_DIR)
try:
    from multiplayer_telegram_bot.whitelist import load_whitelist, enable_group, add_allowed_user, save_whitelist  # noqa: E402
    from multiplayer_telegram_bot.gating import gate_message, GatingAction  # noqa: E402
    from multiplayer_telegram_bot.router import get_source_for_chat  # noqa: E402
    from multiplayer_telegram_bot.commands import (  # noqa: E402
        handle_enable_group_bot,
        handle_whitelist,
        handle_unwhitelist,
    )
    from multiplayer_telegram_bot.session import (  # noqa: E402
        get_active_session,
        open_session,
        close_session,
        refresh_session,
        is_closure_signal,
    )
    _GROUP_GATING_ENABLED = True
    _GROUP_COMMANDS_ENABLED = True
    _GROUP_SESSION_ENABLED = True
except ImportError:
    _GROUP_GATING_ENABLED = False
    _GROUP_COMMANDS_ENABLED = False
    _GROUP_SESSION_ENABLED = False
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "multiplayer-telegram-bot skill not available — group gating and management commands disabled"
    )

# ChannelAdapter Protocol — soft import; lobster_bot satisfies it structurally
# but keeps its own async OutboxHandler rather than using OutboxFileHandler.
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from channels.base import ChannelAdapter  # noqa: F401
except ImportError:
    pass  # channels package not yet installed; type hint only

import re
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatMemberHandler, MessageReactionHandler, filters, ContextTypes
from collections import deque


# URLs longer than this are copy-paste targets (e.g. OAuth flows).  Telegram
# hides the raw URL when it is embedded in an <a> tag, so we render them as
# plain text instead — label on the first line, URL on the next — so the user
# can long-press and copy without hunting through a menu.
_LONG_URL_THRESHOLD = 200


def _link_to_html(link_text: str, url: str) -> str:
    """Convert a single [text](url) Markdown link to HTML.

    Short URLs (≤ _LONG_URL_THRESHOLD chars) become a normal <a> tag so
    Telegram renders them as a tappable hyperlink.

    Long URLs (> _LONG_URL_THRESHOLD chars) are expanded to two lines of plain
    text:
        <b>link_text</b>
        <pre>url</pre>

    The <pre> wrapper prevents Telegram from collapsing the URL and makes it
    easy to long-press and copy on mobile.
    """
    if len(url) > _LONG_URL_THRESHOLD:
        # Escape HTML entities in the URL (it may contain & params already escaped)
        escaped_url = url.replace('&amp;', '&').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return f"<b>{link_text}</b>\n<pre>{escaped_url}</pre>"
    return f'<a href="{url}">{link_text}</a>'


def md_to_html(text: str) -> str:
    """Convert Telegram-flavored Markdown to HTML for reliable rendering.

    Handles: [text](url) links, `code`, ```code blocks```, **bold**, *bold*, _italic_,
    ## headings, ### headings, --- horizontal rules.
    Escapes &, <, > in non-HTML portions.

    Long URLs (> _LONG_URL_THRESHOLD chars) are rendered as plain text rather
    than hyperlinks — Telegram hides embedded URLs from users who need to
    copy-paste them (e.g. OAuth flows).
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
            # Horizontal rules: --- on its own line → blank line
            p = re.sub(r'(?m)^---+\s*$', '', p)
            # Headers: ### or ## or # at start of line → <b>text</b>
            p = re.sub(r'(?m)^#{1,6}\s+(.+)$', r'<b>\1</b>', p)
            # Links: [text](url) — long URLs rendered as plain text for copy-paste
            p = re.sub(
                r'\[([^\]]+)\]\(([^)]+)\)',
                lambda m: _link_to_html(m.group(1), m.group(2)),
                p,
            )
            # Bold: **text**
            p = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', p)
            # Italic: _text_ (single, not double)
            # Use \w boundaries so snake_case tokens (write_result, STALE_NO_FILE)
            # are not misread as italic spans.  A leading/trailing underscore
            # only triggers italic when it is NOT adjacent to a word character,
            # e.g. "  _italic_  " works but "write_result" is left untouched.
            p = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'<i>\1</i>', p)
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
# Voice messages are written here first; the transcription worker picks them up,
# runs whisper.cpp, and moves the enriched message to INBOX_DIR automatically.
PENDING_TRANSCRIPTION_DIR = _MESSAGES / "pending-transcription"

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

# Reactions undone within this window (seconds) are treated as cancelled and ignored.
REACTION_UNDO_WINDOW_SECS: float = 5.0

# Pending reactions: tg_msg_id -> asyncio.Task — allows cancellation on undo.
# Keyed by (chat_id, tg_msg_id) to handle multi-user group chats correctly.
_pending_reactions: dict[tuple[int, int], asyncio.Task] = {}

# Rolling ring buffer of sent messages: tg_msg_id -> text snippet.
# Populated in OutboxHandler.process_reply so reactions can include
# the text of the message that was reacted to.
_sent_message_buffer: deque[tuple[int, str]] = deque(maxlen=50)


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
        _log = logging.getLogger("lobster")
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
PENDING_TRANSCRIPTION_DIR.mkdir(parents=True, exist_ok=True)
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

# Lock serialising the _processing_files check-and-add in _schedule_processing.
# Without this, two watchdog events for the same file (e.g. IN_MOVED_TO and
# IN_MODIFY arriving close together) could both pass the `not in` guard before
# either has added the path, causing duplicate Telegram delivery (#922).
_processing_files_lock = threading.Lock()

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

# Group chat engagement state — tracks active bot conversation threads.
# Key: (chat_id, thread_root_message_id | None)
# Value: timestamp of last invocation in this thread
# Entries expire after ENGAGEMENT_WINDOW_SECONDS with no new messages.
_engaged_threads: dict[tuple[int, Optional[int]], float] = {}
ENGAGEMENT_WINDOW_SECONDS = 600  # 10 minutes


def _is_direct_invocation(message, bot_username: str) -> bool:
    """Return True if this group message is directly addressed to the bot.

    A message is a direct invocation if:
    - It contains a @mention entity pointing to the bot's username, OR
    - It is a reply to a message sent by the bot.

    Uses message.entities for mention detection (not raw text search) to avoid
    false positives when users quote the bot's name in ordinary conversation.
    """
    # Reply-to-bot check
    reply_to = getattr(message, "reply_to_message", None)
    if reply_to:
        sender = getattr(reply_to, "from_user", None)
        if sender and getattr(sender, "is_bot", False):
            # Check if that bot is our bot — by username if available
            sender_username = getattr(sender, "username", None)
            if sender_username and bot_username:
                if sender_username.lower() == bot_username.lower():
                    return True
            elif getattr(sender, "is_bot", False):
                # Fallback: any bot reply counts (single-bot context)
                return True

    # Entity-based mention check
    entities = getattr(message, "entities", None) or []
    text = getattr(message, "text", "") or ""
    caption_entities = getattr(message, "caption_entities", None) or []
    caption = getattr(message, "caption", "") or ""

    for entity in list(entities) + list(caption_entities):
        entity_text_source = text if entity in entities else caption
        entity_type = getattr(entity, "type", "")
        if entity_type == "mention":
            offset = getattr(entity, "offset", 0)
            length = getattr(entity, "length", 0)
            mentioned = entity_text_source[offset:offset + length]
            # mentioned is like "@Awp_Sebastian_bot"
            if mentioned.lstrip("@").lower() == bot_username.lower():
                return True

    return False


def _get_thread_root_id(message) -> Optional[int]:
    """Return the Telegram message ID that roots this reply chain, or None.

    If the message is a reply, return the ID of the message it replied to.
    This is used to track engagement by thread rather than by individual message.
    """
    reply_to = getattr(message, "reply_to_message", None)
    if reply_to:
        return getattr(reply_to, "message_id", None)
    return None


def _is_in_engaged_thread(chat_id: int, thread_root_id: Optional[int]) -> bool:
    """Return True if there is an active engagement window for this thread.

    An engagement window is active if the last direct invocation in this thread
    was within ENGAGEMENT_WINDOW_SECONDS.
    """
    key = (chat_id, thread_root_id)
    last_ts = _engaged_threads.get(key)
    if last_ts is None:
        return False
    return (time.time() - last_ts) < ENGAGEMENT_WINDOW_SECONDS


def _mark_thread_engaged(chat_id: int, thread_root_id: Optional[int]) -> None:
    """Record or refresh engagement for a conversation thread."""
    _engaged_threads[(chat_id, thread_root_id)] = time.time()


def _expire_engaged_threads() -> None:
    """Remove stale engagement entries older than ENGAGEMENT_WINDOW_SECONDS.

    Called opportunistically from the typing refresh loop to prevent unbounded
    growth of _engaged_threads.
    """
    cutoff = time.time() - ENGAGEMENT_WINDOW_SECONDS
    stale = [k for k, ts in _engaged_threads.items() if ts < cutoff]
    for k in stale:
        del _engaged_threads[k]


def _get_bot_username() -> str:
    """Return the bot's Telegram username (without @) for mention detection.

    Reads from the running bot_app after initialization. Falls back to the
    BOT_USERNAME environment variable, then to an empty string (which causes
    _is_direct_invocation to skip entity-based checks safely).
    """
    if bot_app and getattr(bot_app, "bot", None):
        username = getattr(bot_app.bot, "username", None)
        if username:
            return username
    env_val = os.environ.get("BOT_USERNAME", "")
    return env_val


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

    For group messages (source="lobster-group"), the typing indicator is sent
    only when direct_invocation=True.  Passive group messages that Lobster
    processes silently should not advertise bot activity to the whole group.
    """
    log.info("Typing refresh loop started")
    _expire_cycle = 0
    while True:
        await asyncio.sleep(4)
        try:
            if not bot_app:
                continue
            # Periodically expire stale engagement windows (every ~60s)
            _expire_cycle += 1
            if _expire_cycle >= 15:
                _expire_engaged_threads()
                _expire_cycle = 0
            # Scan all files in the processing directory
            if not _PROCESSING_DIR.exists():
                continue
            for msg_file in _PROCESSING_DIR.glob("*.json"):
                try:
                    data = json.loads(msg_file.read_text())
                    source = data.get("source", "")
                    chat_id = data.get("chat_id")
                    # For DMs: always send typing indicator.
                    # For group messages: only when directly invoked (not passive).
                    # default True preserves DM behavior for messages without the field.
                    direct_inv = data.get("direct_invocation", True)
                    if source in ("telegram", "lobster-group") and direct_inv and chat_id:
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
            # Read existing state first so we preserve fields like compacted_at,
            # booted_at, and last_restart_at — same pattern as _write_lobster_state()
            # in inbox_server.py. Overwriting with a bare dict was bug #923.
            existing: dict = {}
            try:
                existing = json.loads(LOBSTER_STATE_FILE.read_text())
            except Exception:
                pass
            existing.update({
                "mode": "active",
                "woke_at": datetime.now(timezone.utc).isoformat(),
            })
            tmp = LOBSTER_STATE_FILE.parent / f".lobster-state-wake-{os.getpid()}.tmp"
            tmp.write_text(json.dumps(existing, indent=2))
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
                subprocess.run(
                    ["bash", str(CLAUDE_WAKE_SCRIPT)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
                log.info(f"wake_claude: spawned {CLAUDE_WAKE_SCRIPT}")
            else:
                log.error(f"wake_claude: fallback script not found: {CLAUDE_WAKE_SCRIPT}")
    finally:
        _wake_lock.release()





def extract_reply_to_context(message) -> dict | None:
    """Extract reply-to context from a Telegram message, if it's a reply.

    Returns a dict with the original message's text/caption and sender info,
    or None if this message is not a reply to another message.
    """
    if not message.reply_to_message:
        return None

    orig = message.reply_to_message
    orig_text = orig.text or orig.caption or ""
    orig_user = orig.from_user
    return {
        "text": orig_text,
        "user_id": orig_user.id if orig_user else None,
        "username": orig_user.username if orig_user else None,
        "user_name": orig_user.first_name if orig_user else None,
        "message_id": orig.message_id,
    }


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    if user.id not in ALLOWED_USERS:
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    await update.message.reply_text(
        "Lobster is running! Send me a message and I'll process it."
    )


async def onboarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /onboarding command - show onboarding message."""
    user = update.effective_user
    if user.id not in ALLOWED_USERS:
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    onboarding_msg = get_onboarding_message(user.first_name)
    chunks = split_message(onboarding_msg)
    for chunk in chunks:
        await update.message.reply_text(md_to_html(chunk), parse_mode="HTML")


async def enable_group_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /enable_group_bot <chat_id> [name] — enable a group in the whitelist.

    Only works in private DMs from ALLOWED_USERS. Silently drops the command
    from non-DM chats or non-allowed users.
    """
    user = update.effective_user
    if not user or user.id not in ALLOWED_USERS:
        return
    if update.effective_chat.type != "private":
        return
    if not _GROUP_COMMANDS_ENABLED:
        await update.message.reply_text("Group management commands are not available (skill not installed).")
        return
    result = handle_enable_group_bot(update.message.text)
    await update.message.reply_text(result.reply)


async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /whitelist <user_id> <chat_id> — add a user to a group's whitelist.

    Only works in private DMs from ALLOWED_USERS. Silently drops otherwise.
    """
    user = update.effective_user
    if not user or user.id not in ALLOWED_USERS:
        return
    if update.effective_chat.type != "private":
        return
    if not _GROUP_COMMANDS_ENABLED:
        await update.message.reply_text("Group management commands are not available (skill not installed).")
        return
    result = handle_whitelist(update.message.text)
    await update.message.reply_text(result.reply)


async def unwhitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unwhitelist <user_id> <chat_id> — remove a user from a group's whitelist.

    Only works in private DMs from ALLOWED_USERS. Silently drops otherwise.
    """
    user = update.effective_user
    if not user or user.id not in ALLOWED_USERS:
        return
    if update.effective_chat.type != "private":
        return
    if not _GROUP_COMMANDS_ENABLED:
        await update.message.reply_text("Group management commands are not available (skill not installed).")
        return
    result = handle_unwhitelist(update.message.text)
    await update.message.reply_text(result.reply)


async def list_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list_groups — show all configured groups and their whitelisted users.

    Only works in private DMs from ALLOWED_USERS. Silently drops otherwise.
    """
    user = update.effective_user
    if not user or user.id not in ALLOWED_USERS:
        return
    if update.effective_chat.type != "private":
        return
    if not _GROUP_COMMANDS_ENABLED:
        await update.message.reply_text("Group management commands are not available (skill not installed).")
        return

    store = load_whitelist()
    groups = store.get("groups", {})

    if not groups:
        await update.message.reply_text("No groups configured.")
        return

    lines = ["Configured groups:\n"]
    for group_id, config in groups.items():
        name = config.get("name", group_id)
        enabled = config.get("enabled", False)
        allowed_ids = config.get("allowed_user_ids", [])
        status = "enabled" if enabled else "disabled"
        lines.append(f"• {name} ({group_id}) — {status}")
        if allowed_ids:
            lines.append(f"  Whitelisted users: {', '.join(str(uid) for uid in allowed_ids)}")
        else:
            lines.append("  No whitelisted users")

    await update.message.reply_text("\n".join(lines))


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    user = query.from_user

    if user.id not in ALLOWED_USERS:
        await query.answer("Not authorized")
        return

    # Acknowledge the button press immediately (removes the loading indicator)
    await query.answer()

    # Wake Claude if hibernating
    wake_claude_if_hibernating()

    # Create a message file for the callback
    msg_id = f"{int(time.time() * 1000)}_{query.id}"

    msg_data = {
        "id": msg_id,
        "source": "telegram",
        "type": "callback",
        "chat_id": query.message.chat_id,
        "user_id": user.id,
        "username": user.username,
        "user_name": user.first_name,
        "text": f"[Button pressed: {query.data}]",
        "callback_data": query.data,
        "original_message_text": query.message.text or query.message.caption or "",
        "timestamp": datetime.utcnow().isoformat(),
    }

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)
    log.info(f"Wrote callback message to inbox: {msg_id}")


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle photo messages: download and save to inbox with metadata."""
    user = update.effective_user
    message = update.message

    await send_typing_indicator(message.chat_id)

    # Check if this photo is part of a media group
    if message.media_group_id:
        await _handle_media_group_photo(update, context, msg_id)
        return

    try:
        # Get the largest photo size
        photo = message.photo[-1]

        # Download the photo
        file = await context.bot.get_file(photo.file_id)
        image_path = IMAGES_DIR / f"{msg_id}.jpg"
        await file.download_to_drive(image_path)
        log.info(f"Downloaded photo to: {image_path}")

        caption = message.caption or ""

        chat = message.chat
        _is_group = chat.type in ("group", "supergroup")
        msg_data = {
            "id": msg_id,
            "source": (
                get_source_for_chat(chat.type) if _GROUP_GATING_ENABLED else "telegram"
            ),
            "type": "photo",
            "chat_id": message.chat_id,
            "telegram_message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": caption if caption else "[Photo message]",
            "image_file": str(image_path),
            "timestamp": datetime.utcnow().isoformat(),
        }
        direct_inv = False
        engaged = False
        thread_root_id: Optional[int] = None
        if _is_group:
            bot_username = _get_bot_username()
            thread_root_id = _get_thread_root_id(message)
            direct_inv = _is_direct_invocation(message, bot_username)
            engaged = _is_in_engaged_thread(chat.id, thread_root_id)
            if direct_inv or engaged:
                _mark_thread_engaged(chat.id, thread_root_id)
                _mark_thread_engaged(chat.id, message.message_id)
            msg_data["group_chat_id"] = chat.id
            msg_data["group_title"] = chat.title
            msg_data["direct_invocation"] = direct_inv or engaged
            msg_data["thread_root_message_id"] = thread_root_id

        # Capture full reply-to context if this message is a reply
        reply_ctx = extract_reply_to_context(message)
        if reply_ctx:
            msg_data["reply_to"] = reply_ctx

        inbox_file = INBOX_DIR / f"{msg_id}.json"
        atomic_write_json(inbox_file, msg_data)

        log.info(f"Wrote photo message to inbox: {msg_id}")
        if not _is_group:
            await message.reply_text("📸 Photo received. Looking at it...")
        elif direct_inv or engaged:
            await message.reply_text("📸 Photo received. Looking at it...")

    except Exception as e:
        log.error(f"Error handling photo message: {e}", exc_info=True)
        await message.reply_text("❌ Failed to process photo.")


async def _handle_media_group_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle a single photo that is part of a media group (album).

    Photos in a media group arrive as separate updates with the same
    media_group_id. We buffer them here and emit a single grouped inbox
    message after MEDIA_GROUP_FLUSH_DELAY seconds.
    """
    message = update.message
    user = update.effective_user
    group_id = message.media_group_id

    try:
        photo = message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        # Use msg_id (which is unique per photo update) as the filename
        image_path = IMAGES_DIR / f"{msg_id}.jpg"
        await file.download_to_drive(image_path)
        log.info(f"Downloaded media group photo to: {image_path}")
    except Exception as e:
        log.error(f"Error downloading media group photo: {e}", exc_info=True)
        return

    if group_id not in _media_group_buffers:
        buf = _MediaGroupBuffer(
            media_group_id=group_id,
            chat_id=message.chat_id,
            user_id=user.id,
            username=user.username,
            user_name=user.first_name,
            caption=message.caption or "",
            reply_ctx=extract_reply_to_context(message),
        )
        _media_group_buffers[group_id] = buf
        # Schedule the flush task
        loop = asyncio.get_event_loop()
        buf.flush_task = loop.create_task(_flush_media_group(group_id, message.chat_id))

    buf = _media_group_buffers[group_id]
    buf.image_paths.append(str(image_path))
    # Use the first non-empty caption
    if not buf.caption and message.caption:
        buf.caption = message.caption


async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle document/file messages: save metadata to inbox (no download)."""
    user = update.effective_user
    message = update.message
    document = message.document

    await send_typing_indicator(message.chat_id)

    try:
        caption = message.caption or ""

        chat = message.chat
        _is_group = chat.type in ("group", "supergroup")
        msg_data = {
            "id": msg_id,
            "source": (
                get_source_for_chat(chat.type) if _GROUP_GATING_ENABLED else "telegram"
            ),
            "type": "document",
            "chat_id": message.chat_id,
            "telegram_message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": caption if caption else f"[Document: {document.file_name or 'unnamed'}]",
            "document_file_name": document.file_name,
            "document_mime_type": document.mime_type,
            "document_file_size": document.file_size,
            "file_id": document.file_id,
            "timestamp": datetime.utcnow().isoformat(),
        }
        direct_inv = False
        engaged = False
        thread_root_id: Optional[int] = None
        if _is_group:
            bot_username = _get_bot_username()
            thread_root_id = _get_thread_root_id(message)
            direct_inv = _is_direct_invocation(message, bot_username)
            engaged = _is_in_engaged_thread(chat.id, thread_root_id)
            if direct_inv or engaged:
                _mark_thread_engaged(chat.id, thread_root_id)
                _mark_thread_engaged(chat.id, message.message_id)
            msg_data["group_chat_id"] = chat.id
            msg_data["group_title"] = chat.title
            msg_data["direct_invocation"] = direct_inv or engaged
            msg_data["thread_root_message_id"] = thread_root_id

        # Capture full reply-to context if this message is a reply
        reply_ctx = extract_reply_to_context(message)
        if reply_ctx:
            msg_data["reply_to"] = reply_ctx

        inbox_file = INBOX_DIR / f"{msg_id}.json"
        atomic_write_json(inbox_file, msg_data)

        log.info(f"Wrote document message to inbox: {msg_id}")
        if not _is_group:
            await message.reply_text("📎 Document received.")
        elif direct_inv or engaged:
            await message.reply_text("📎 Document received.")

    except Exception as e:
        log.error(f"Error handling document message: {e}", exc_info=True)
        await message.reply_text("❌ Failed to process document.")


async def _check_group_gating(
    user,
    chat,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Two-tier access check for a message.

    Returns True if the message should be processed, False if it should be
    dropped.  Handles three cases:
      - Group/supergroup with gating enabled: run gate_message() and act on result.
      - Group/supergroup without gating skill: drop silently.
      - DM: allow only if user.id is in ALLOWED_USERS.

    This is a pure decision function — callers are responsible for returning
    early when False is returned.
    """
    if chat.type in ("group", "supergroup"):
        if _GROUP_GATING_ENABLED:
            store = load_whitelist()
            result = gate_message(chat.id, user.id, store)
            if result.action == GatingAction.DROP_SILENT:
                log.debug(f"Group message silently dropped: {result.reason}")
                return False
            elif result.action == GatingAction.SEND_REGISTRATION_DM:
                # Group is whitelisted but user is not — silently drop, no DM
                log.debug(
                    f"Non-whitelisted user {user.id} in whitelisted group {chat.id}: "
                    "silently dropped"
                )
                return False
            # GatingAction.ALLOW — proceed
            return True
        else:
            # Skill not available; drop all group messages silently
            return False
    else:
        # DM path — unchanged behaviour
        return user.id in ALLOWED_USERS


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all incoming messages."""
    message = update.message
    if not message:
        return

    user = update.effective_user
    if not user:
        return

    chat = message.chat
    if not await _check_group_gating(user, chat, context):
        return

    # Wake Claude if hibernating (non-blocking — spawns subprocess if needed)
    wake_claude_if_hibernating()

    # First-message detection: send onboarding to new users
    if not is_user_onboarded(user.id):
        await send_onboarding(update, user)

    msg_id = f"{int(time.time() * 1000)}_{message.message_id}"

    # Handle voice messages and audio file attachments through a unified path.
    # message.voice is an in-app recording (always .ogg); message.audio is an
    # uploaded file attachment (any format).  Both have file_id, duration,
    # file_size, mime_type.  Only Audio has file_name/title/performer.
    audio_obj = message.voice or message.audio
    if audio_obj:
        await handle_audio_message(update, context, msg_id, audio_obj)
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

    # Determine group-chat engagement state before writing to inbox
    _is_group = chat.type in ("group", "supergroup")
    direct_inv = False
    engaged = False
    thread_root_id: Optional[int] = None

    if _is_group:
        bot_username = _get_bot_username()
        thread_root_id = _get_thread_root_id(message)
        direct_inv = _is_direct_invocation(message, bot_username)
        engaged = _is_in_engaged_thread(chat.id, thread_root_id)

        # Per-user session followup check (persistent, survives restarts).
        # In addition to thread-based engagement, check if the sending user
        # has an active session (they invoked the bot recently). This enforces
        # the policy that only the invoker can follow up without @mention.
        _session_followup = False
        _active_session = None
        if _GROUP_SESSION_ENABLED:
            try:
                _active_session = get_active_session(chat.id)
            except Exception as _e:
                log.debug(f"Session lookup failed (non-fatal): {_e}")

        if not direct_inv and not engaged and _active_session is not None:
            if _active_session.invoker_user_id == user.id:
                _session_followup = True
                engaged = True  # treat session followup as engaged

        # Closure signal: close the session only if the sender is the session
        # invoker. A different authorized user saying "thanks" in the same
        # group chat must NOT close a session they did not open.
        if engaged and _active_session is not None:
            try:
                if (
                    is_closure_signal(text)
                    and _active_session.invoker_user_id == user.id
                ):
                    close_session(chat.id)
                    log.debug(
                        f"Session closed for {chat.id}: closure signal from {user.id}"
                    )
                    return
            except Exception as _e:
                log.debug(f"Session closure check failed (non-fatal): {_e}")

        if direct_inv or engaged:
            _mark_thread_engaged(chat.id, thread_root_id)
            # Also register the current message's ID as a future thread root so
            # replies to *this* message are covered by the engagement window.
            _mark_thread_engaged(chat.id, message.message_id)
            log.debug(
                f"Group thread engaged: chat={chat.id} thread_root={thread_root_id} "
                f"msg_id={message.message_id} direct={direct_inv} engaged={engaged}"
            )

            # Open/refresh a per-user session when directly invoked.
            if direct_inv and _GROUP_SESSION_ENABLED:
                try:
                    open_session(chat_id=chat.id, invoker_user_id=user.id)
                except Exception as _e:
                    log.debug(f"Session open failed (non-fatal): {_e}")

    # Create message file in inbox
    msg_data = {
        "id": msg_id,
        "source": (
            get_source_for_chat(chat.type) if _GROUP_GATING_ENABLED else "telegram"
        ),
        "type": "text",
        "chat_id": message.chat_id,
        "telegram_message_id": message.message_id,
        "user_id": user.id,
        "username": user.username,
        "user_name": user.first_name,
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
    }
    if _is_group:
        msg_data["group_chat_id"] = chat.id
        msg_data["group_title"] = chat.title
        msg_data["direct_invocation"] = direct_inv or engaged
        msg_data["thread_root_message_id"] = thread_root_id

    # Capture full reply-to context if this message is a reply
    reply_ctx = extract_reply_to_context(message)
    if reply_ctx:
        msg_data["reply_to"] = reply_ctx

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)

    log.info(f"Wrote message to inbox: {msg_id}")

    # Send acknowledgment.
    # In DMs: always ack.
    # In groups: ack only for direct invocations and engaged thread continuations.
    # Passive group messages are processed silently — no ack, no noise.
    if not _is_group:
        await message.reply_text("📨 Message received. Processing...")
    elif direct_inv or engaged:
        await message.reply_text("📨 Got it. Processing...")


def _find_message_by_telegram_id(tg_message_id: int) -> Path | None:
    """Scan inbox/ and processing/ for a message file with a matching telegram_message_id.

    Returns the Path to the matching file, or None if not found.
    Only text messages are checked — edits arrive as updated_message which carries
    the same message_id as the original, so this check finds messages that haven't
    been processed yet.
    """
    _processing_dir = _MESSAGES / "processing"
    for search_dir in (INBOX_DIR, _processing_dir):
        if not search_dir.exists():
            continue
        for f in search_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("telegram_message_id") == tg_message_id:
                    return f
            except Exception:
                continue
    return None


async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edited_message events from Telegram.

    Design principle: deliver, don't discard.

    If the original message is still in inbox/ or processing/, queue the edit
    as a new message with annotation fields so the dispatcher knows it's an edit.
    If the original has already been processed, also queue it as new (the edit
    may still be actionable).

    The subagent working on the original is NOT cancelled — its result is delivered
    with a note that the user has since edited their message.
    """
    message = update.edited_message
    if not message:
        return

    user = update.effective_user
    if not user:
        return

    chat = message.chat
    if not await _check_group_gating(user, chat, context):
        return

    text = message.text
    if not text:
        return  # Ignore non-text edits (media caption edits are not yet handled)

    original_tg_id = message.message_id
    original_file = _find_message_by_telegram_id(original_tg_id)

    msg_id = f"{int(time.time() * 1000)}_edit_{message.message_id}"

    _is_group = chat.type in ("group", "supergroup")
    msg_data = {
        "id": msg_id,
        "source": (
            get_source_for_chat(chat.type) if _GROUP_GATING_ENABLED else "telegram"
        ),
        "type": "text",
        "chat_id": message.chat_id,
        "telegram_message_id": message.message_id,
        "user_id": user.id,
        "username": user.username,
        "user_name": user.first_name,
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
        "_edit_of_telegram_id": original_tg_id,
    }
    if _is_group:
        msg_data["group_chat_id"] = chat.id
        msg_data["group_title"] = chat.title

    if original_file is not None:
        msg_data["_replaces_inbox_id"] = original_file.stem
        log.info(
            f"Edit of tg_id={original_tg_id} queued as {msg_id} "
            f"(original file: {original_file.name})"
        )
    else:
        msg_data["_edit_note"] = "User edited a previously processed message."
        log.info(
            f"Edit of tg_id={original_tg_id} queued as {msg_id} "
            "(original already processed)"
        )

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)


def _lookup_reacted_to_text(tg_msg_id: int) -> str:
    """Return the buffered text for a sent message, or empty string if not found.

    Pure lookup against _sent_message_buffer — no I/O.
    """
    for buffered_id, buffered_text in _sent_message_buffer:
        if buffered_id == tg_msg_id:
            return buffered_text
    return ""


async def _emit_reaction_signal(
    chat_id: int,
    tg_msg_id: int,
    emoji: str,
) -> None:
    """Write a reaction inbox entry after the undo window has elapsed.

    This coroutine is scheduled as an asyncio.Task and cancelled if the user
    removes the reaction within REACTION_UNDO_WINDOW_SECS.
    """
    # Note: pending reactions are dropped on bot restart — acceptable given the 5s window
    await asyncio.sleep(REACTION_UNDO_WINDOW_SECS)

    reacted_to_text = _lookup_reacted_to_text(tg_msg_id)
    msg_id = f"{int(time.time() * 1000)}_reaction_{tg_msg_id}"

    msg_data = {
        "id": msg_id,
        "source": "telegram",
        "type": "reaction",
        "chat_id": chat_id,
        "telegram_message_id": tg_msg_id,
        "emoji": emoji,
        "reacted_to_text": reacted_to_text,
        "text": f"[Reaction: {emoji} on message {tg_msg_id}]",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)
    log.info(f"Wrote reaction to inbox: {msg_id} emoji={emoji}")

    # Send acknowledgment (same pattern as text/image messages)
    if bot_app:
        try:
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=f"Reaction received: {emoji}",
            )
        except Exception as e:
            log.warning(f"Failed to send reaction ack: {e}")

    # Clean up the pending entry
    _pending_reactions.pop((chat_id, tg_msg_id), None)


async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle MessageReactionUpdated events from Telegram.

    Design:
    - New reactions are buffered for REACTION_UNDO_WINDOW_SECS before being
      written to the inbox.  If the user removes the reaction within the window,
      the pending task is cancelled and nothing is written.
    - All emoji reactions are delivered — the dispatcher interprets them in context.
    - Reaction removals (new_reaction is empty) cancel any pending task for that
      message, preventing spurious signals when the user quickly toggles.
    """
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    user = update.effective_user
    if not user:
        return

    chat = reaction_update.chat
    if not await _check_group_gating(user, chat, context):
        return

    chat_id: int = reaction_update.chat.id
    tg_msg_id: int = reaction_update.message_id
    pending_key = (chat_id, tg_msg_id)

    new_reactions = reaction_update.new_reaction or []
    old_reactions = reaction_update.old_reaction or []

    # Determine the newly added emoji (present in new but not old)
    old_emojis = {r.emoji for r in old_reactions if hasattr(r, "emoji")}
    new_emojis = {r.emoji for r in new_reactions if hasattr(r, "emoji")}
    added = new_emojis - old_emojis
    removed = old_emojis - new_emojis

    # Cancel pending task for this message on any removal (covers undo pattern)
    if removed and pending_key in _pending_reactions:
        _pending_reactions.pop(pending_key).cancel()
        log.debug(f"Reaction cancelled for chat={chat_id} msg={tg_msg_id}")

    for emoji in added:
        # Cancel any existing pending task for this (chat, message) pair
        existing = _pending_reactions.pop(pending_key, None)
        if existing:
            existing.cancel()

        # Schedule delivery after the undo window
        task = asyncio.create_task(
            _emit_reaction_signal(chat_id, tg_msg_id, emoji)
        )
        _pending_reactions[pending_key] = task
        log.info(
            f"Reaction buffered: emoji={emoji} "
            f"chat={chat_id} msg={tg_msg_id}"
        )


async def handle_audio_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    msg_id: str,
    audio_obj,
):
    """Handle voice messages and audio file attachments through a unified path.

    audio_obj is either a telegram.Voice (in-app recording, always .ogg) or a
    telegram.Audio (uploaded file attachment, any format).  Both expose:
      file_id, duration, file_size, mime_type.
    Only Audio additionally has: file_name, title, performer — accessed via
    getattr with a fallback so this function works for both types.

    Both message types are routed to pending-transcription/ so the transcription
    worker (src/transcription/worker.py) picks them up, runs whisper.cpp, and
    moves the enriched message (with "transcription" and updated "text") to
    inbox/ automatically.  Agents will only ever see the transcribed message.
    """
    user = update.effective_user
    message = update.message

    # Determine whether this is a voice recording or an uploaded audio file.
    is_voice = message.voice is not None
    msg_type = "voice" if is_voice else "audio"

    await send_typing_indicator(message.chat_id)

    try:
        # Derive a filename and extension.  Voice recordings are always .ogg;
        # audio attachments may carry an explicit file_name from the sender.
        original_filename = getattr(audio_obj, "file_name", None) or f"{msg_id}.ogg"
        ext = Path(original_filename).suffix or ".ogg"
        audio_path = AUDIO_DIR / f"{msg_id}{ext}"

        file = await context.bot.get_file(audio_obj.file_id)
        await file.download_to_drive(audio_path)
        log.info(f"Downloaded {msg_type} message to: {audio_path}")

        caption = message.caption or ""
        default_text = (
            "[Voice message - pending transcription]"
            if is_voice
            else "[Audio file - pending transcription]"
        )

        chat = message.chat
        _is_group = chat.type in ("group", "supergroup")
        msg_data = {
            "id": msg_id,
            "source": (
                get_source_for_chat(chat.type) if _GROUP_GATING_ENABLED else "telegram"
            ),
            "type": msg_type,
            "chat_id": message.chat_id,
            "telegram_message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": caption if caption else default_text,
            "audio_file": str(audio_path),
            "original_filename": original_filename,
            "audio_duration": audio_obj.duration,
            "audio_mime_type": audio_obj.mime_type or ("audio/ogg" if is_voice else "audio/mpeg"),
            "file_id": audio_obj.file_id,
            "timestamp": datetime.utcnow().isoformat(),
        }
        direct_inv = False
        engaged = False
        thread_root_id: Optional[int] = None
        if _is_group:
            bot_username = _get_bot_username()
            thread_root_id = _get_thread_root_id(message)
            direct_inv = _is_direct_invocation(message, bot_username)
            engaged = _is_in_engaged_thread(chat.id, thread_root_id)
            if direct_inv or engaged:
                _mark_thread_engaged(chat.id, thread_root_id)
                _mark_thread_engaged(chat.id, message.message_id)
            msg_data["group_chat_id"] = chat.id
            msg_data["group_title"] = chat.title
            msg_data["direct_invocation"] = direct_inv or engaged
            msg_data["thread_root_message_id"] = thread_root_id

        # Capture full reply-to context if this message is a reply
        reply_ctx = extract_reply_to_context(message)
        if reply_ctx:
            msg_data["reply_to"] = reply_ctx

        pending_file = PENDING_TRANSCRIPTION_DIR / f"{msg_id}.json"
        atomic_write_json(pending_file, msg_data)

        log.info(f"Wrote {msg_type} message to pending-transcription: {msg_id}")
        ack = "🎤 Voice message received. Transcribing..." if is_voice else "🎵 Audio file received. Transcribing..."
        if not _is_group:
            await message.reply_text(ack)
        elif direct_inv or engaged:
            await message.reply_text(ack)

    except Exception as e:
        log.error(f"Error handling {msg_type} message: {e}", exc_info=True)
        await message.reply_text(f"❌ Failed to process {msg_type} message.")


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
        log.warning(f"Media group {media_group_id} has no images — skipping")
        return

    msg_id = f"{int(time.time() * 1000)}_mg_{media_group_id}"
    caption = buf.caption or ""

    msg_data = {
        "id": msg_id,
        "source": "telegram",
        "type": "photo",
        "chat_id": buf.chat_id,
        "user_id": buf.user_id,
        "username": buf.username,
        "user_name": buf.user_name,
        "text": caption if caption else f"[{len(buf.image_paths)} photos]",
        "image_files": buf.image_paths,
        "image_file": buf.image_paths[0],  # backward compat: primary image
        "timestamp": datetime.utcnow().isoformat(),
    }

    if buf.reply_ctx:
        msg_data["reply_to"] = buf.reply_ctx

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    atomic_write_json(inbox_file, msg_data)
    log.info(f"Flushed media group {media_group_id}: {len(buf.image_paths)} photos → {msg_id}")

    # Send one ack for the whole group
    if bot_app:
        try:
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=f"📸 {len(buf.image_paths)} photos received. Processing...",
            )
        except Exception as e:
            log.warning(f"Failed to send media group ack: {e}")


async def send_onboarding(update: Update, user) -> None:
    """Send onboarding message to a first-time user and mark them as onboarded."""
    mark_user_onboarded(user.id)
    onboarding_msg = get_onboarding_message(user.first_name)
    chunks = split_message(onboarding_msg)
    for chunk in chunks:
        await update.message.reply_text(md_to_html(chunk), parse_mode="HTML")


async def process_reply(chat_id: int, text: str, reply_markup=None, thread_ts=None) -> None:
    """Send a reply to the user, splitting long messages if necessary.

    reply_markup: Optional InlineKeyboardMarkup for button support.
    thread_ts: Ignored (Telegram-only parameter placeholder for Slack parity).
    """
    if not bot_app:
        log.error("Bot app not initialized, cannot send reply")
        return

    send_items = _prepare_send_items(text)
    last_idx = len(send_items) - 1

    for idx, (md_chunk, html_chunk) in enumerate(send_items):
        # Only attach reply_markup to the last chunk
        chunk_markup = reply_markup if idx == last_idx else None
        try:
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=html_chunk,
                parse_mode="HTML",
                reply_markup=chunk_markup,
            )
        except Exception as e:
            log.warning(
                f"HTML send failed for chunk {idx+1}/{len(send_items)} "
                f"({len(html_chunk)} chars): {e}. Falling back to plain text."
            )
            try:
                plain = md_chunk  # Send raw markdown as plain text fallback
                await bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=plain,
                    reply_markup=chunk_markup,
                )
            except Exception as e2:
                log.error(f"Plain text fallback also failed: {e2}")


def build_inline_keyboard(buttons: list) -> InlineKeyboardMarkup:
    """Build a Telegram InlineKeyboardMarkup from a nested list of button labels.

    Each inner list represents a row of buttons.  Each element in the row is
    either a plain string label (callback_data == label) or a [label, data]
    two-element list where data is the callback_data payload.

    Pure function: no side effects.
    """
    keyboard = []
    for row in buttons:
        keyboard_row = []
        for item in row:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                label, data = item
            else:
                label = str(item)
                data = str(item)
            keyboard_row.append(InlineKeyboardButton(text=label, callback_data=data))
        keyboard.append(keyboard_row)
    return InlineKeyboardMarkup(keyboard)


class OutboxHandler(FileSystemEventHandler):
    """Watches outbox for reply files and sends them via Telegram.

    Note: Unlike the sync routers (Slack, SMS, WhatsApp), which all delegate
    to the shared ``src.channels.outbox.OutboxFileHandler``, the Telegram
    handler is intentionally kept here as a custom async implementation.

    Reasons for the async divergence:

    - Sending Telegram messages requires ``await bot.send_message()``, which
      must run on the bot's asyncio event loop (``main_loop``).
    - The handler also manages photo sends, Markdown->HTML conversion,
      multi-chunk long-message splitting, and inline keyboard markup —
      concerns that are Telegram-specific and not portable to the generic
      ``OutboxFileHandler`` interface.

    The handler satisfies the ``ChannelAdapter`` Protocol structurally
    (duck typing) even though it does not inherit from it.
    """

    def _schedule_processing(self, filepath):
        if filepath.endswith('.json') and not filepath.endswith('.tmp'):
            if bot_app and main_loop and main_loop.is_running():
                # Hold the lock for the full check-and-add so two watchdog
                # events for the same file cannot both pass the guard before
                # either has added the path (TOCTOU — fixes #922).
                with _processing_files_lock:
                    if filepath in _processing_files:
                        return
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
            try:
                with open(filepath, 'r') as f:
                    reply = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.error(f"process_reply: failed to parse {filepath}: {exc}")
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                return

            chat_id = reply.get('chat_id')
            text = reply.get('text', '')
            buttons = reply.get('buttons')
            reply_type = reply.get('type', 'text')
            photo_url = reply.get('photo_url', '')
            caption = reply.get('caption', '')
            reply_to_id = reply.get('reply_to_message_id')

            # Handle photo messages (from image-generation skill or other sources)
            if reply_type == 'photo' and photo_url and chat_id and bot_app:
                try:
                    caption_html = md_to_html(caption) if caption else None
                    photo_reply_params = ReplyParameters(message_id=int(reply_to_id)) if reply_to_id else None
                    await bot_app.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_url,
                        caption=caption_html,
                        parse_mode="HTML" if caption_html else None,
                        reply_parameters=photo_reply_params,
                    )
                    log.info(f"Sent photo to {chat_id}: {photo_url[:80]}...")
                    os.remove(filepath)
                    return
                except Exception as e:
                    log.warning(f"send_photo failed for {chat_id} ({photo_url[:60]}): {e} — falling back to URL in text")
                    # Fall back: send the URL as a text message so user gets something
                    fallback_text = f"Here's your generated image:\n{photo_url}"
                    if caption:
                        fallback_text = f"{caption}\n\n{fallback_text}"
                    try:
                        await bot_app.bot.send_message(chat_id=chat_id, text=fallback_text)
                        log.info(f"Sent photo URL as text fallback to {chat_id}")
                        os.remove(filepath)
                        return
                    except Exception as e2:
                        log.error(f"Fallback text send also failed for {chat_id}: {e2}")
                        os.remove(filepath)
                        return

            if chat_id and text and bot_app:
                reply_markup = build_inline_keyboard(buttons) if buttons else None
                reply_params = ReplyParameters(message_id=int(reply_to_id)) if reply_to_id else None
                send_items = _prepare_send_items(text)
                n = len(send_items)
                for i, (md_chunk, html_chunk) in enumerate(send_items):
                    # Only attach inline keyboard to the final chunk
                    chunk_markup = reply_markup if i == n - 1 else None
                    # Only thread the first chunk; subsequent chunks are continuations
                    chunk_reply_params = reply_params if i == 0 else None
                    sent_msg = None
                    try:
                        sent_msg = await bot_app.bot.send_message(
                            chat_id=chat_id,
                            text=html_chunk,
                            parse_mode="HTML",
                            reply_markup=chunk_markup,
                            reply_parameters=chunk_reply_params,
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
                        sent_msg = await bot_app.bot.send_message(
                            chat_id=chat_id,
                            text=plain,
                            reply_markup=chunk_markup,
                            reply_parameters=chunk_reply_params,
                        )
                # Buffer only the LAST chunk so that reactions (which reference the
                # final visible message_id) map to the correct text snippet.
                if sent_msg is not None:
                    _sent_message_buffer.append((sent_msg.message_id, md_chunk[:200]))
                if n > 1:
                    log.info(f"Sent reply to {chat_id} in {n} chunks: {text[:50]}...")
                else:
                    log.info(f"Sent reply to {chat_id}: {text[:50]}...")
                # Refresh per-user session TTL whenever the bot replies to a group.
                # This extends the engagement window so active conversations don't
                # time out mid-exchange.
                if _GROUP_SESSION_ENABLED and isinstance(chat_id, int) and chat_id < 0:
                    try:
                        refresh_session(chat_id)
                    except Exception as _e:
                        log.debug(f"Session refresh failed (non-fatal): {_e}")
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
                    from telegram.error import TimedOut as TelegramTimedOut
                    if isinstance(e, TelegramTimedOut):
                        # TimedOut means the HTTP request was dispatched but the response
                        # was lost. Telegram almost certainly received and delivered the
                        # message. Retrying would send a duplicate. Dead-letter immediately.
                        dest = DEAD_LETTER_DIR / filepath.name
                        shutil.move(fname, str(dest))
                        _outbox_fail_counts.pop(fname, None)
                        _processing_files.discard(fname)
                        log.error(
                            f"Dead-lettered after TimedOut (likely delivered): {filepath.name}"
                        )
                    else:
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


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bot being added to or removed from a group.

    When added by a whitelisted user: auto-enables the group in group-whitelist.json
    and seeds all ALLOWED_USERS as allowed members.
    When added by a non-whitelisted user: leaves the group immediately.
    When removed from a group: logs the removal only.
    """
    if not update.my_chat_member:
        return
    event = update.my_chat_member
    new_status = event.new_chat_member.status
    chat = event.chat
    adder = event.from_user

    if new_status in ("member", "administrator") and chat.type in ("group", "supergroup"):
        if adder and adder.id in ALLOWED_USERS:
            log.info(
                f"Bot added to group {chat.id} ({chat.title}) by whitelisted user "
                f"{adder.id} — auto-enabling"
            )
            if _GROUP_GATING_ENABLED:
                try:
                    store = load_whitelist()
                    store = enable_group(chat.id, chat.title or str(chat.id), store)
                    for uid in ALLOWED_USERS:
                        store = add_allowed_user(uid, chat.id, store)
                    save_whitelist(store)
                    log.info(f"Group {chat.id} auto-whitelisted with users {ALLOWED_USERS}")
                except Exception as e:
                    log.error(f"Failed to auto-whitelist group {chat.id}: {e}")
            else:
                log.warning("_GROUP_GATING_ENABLED is False — multiplayer-telegram-bot skill not installed; skipping whitelist update")
        else:
            adder_id = adder.id if adder else "unknown"
            log.info(
                f"Bot added to group {chat.id} by non-whitelisted user {adder_id} — leaving"
            )
            await context.bot.leave_chat(chat.id)
    elif new_status in ("left", "kicked"):
        log.info(f"Bot removed from group {chat.id} ({chat.title})")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        log.error(
            "Telegram Conflict: another bot instance is polling. "
            "Self-terminating so systemd can sequence restarts cleanly."
        )
        import sys
        sys.exit(1)
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
    bot_app = Application.builder().token(BOT_TOKEN).write_timeout(30).build()

    # Add handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("onboarding", onboarding_command))
    # Group management commands — registered before the generic MessageHandler so
    # they are dispatched as commands rather than falling through to Claude.
    bot_app.add_handler(CommandHandler("enable_group_bot", enable_group_bot_command))
    bot_app.add_handler(CommandHandler("whitelist", whitelist_command))
    bot_app.add_handler(CommandHandler("unwhitelist", unwhitelist_command))
    bot_app.add_handler(CommandHandler("list_groups", list_groups_command))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    bot_app.add_handler(CallbackQueryHandler(handle_callback_query))
    bot_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_message))
    # Requires python-telegram-bot >= v20.6 for Update.ALL_TYPES to include message_reaction
    bot_app.add_handler(MessageReactionHandler(handle_reaction))
    bot_app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
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
        await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
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
