#!/usr/bin/env python3
"""
Lobster Slack Router - File-based message passing to master Claude session

Similar to the Telegram bot, this router:
1. Writes incoming Slack messages to ~/messages/inbox/
2. Watches ~/messages/outbox/ for replies with source="slack"
3. Sends replies back to Slack

Uses Socket Mode for simplicity (no public webhook URL required).
"""

import asyncio
import collections
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread, Event

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
# ---------------------------------------------------------------------------
# Shared outbox handler (src/channels/outbox.py)
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
from channels.outbox import OutboxFileHandler, OutboxWatcher, drain_outbox  # noqa: E402

# ---------------------------------------------------------------------------
# Slack Connector ingress logger (logs every event to JSONL before LLM routing)
# ---------------------------------------------------------------------------
try:
    _shop_root = Path(__file__).parent.parent.parent / "lobster-shop" / "slack-connector"
    _sys.path.insert(0, str(_shop_root))
    from src.ingress_logger import SlackIngressLogger  # noqa: E402
    _ingress_logger = SlackIngressLogger()
    _INGRESS_LOGGING_ENABLED = True
except ImportError:
    _ingress_logger = None  # type: ignore[assignment]
    _INGRESS_LOGGING_ENABLED = False

# ---------------------------------------------------------------------------
# Slack Connector channel config + user permissions (Phase 3)
# ---------------------------------------------------------------------------
try:
    from src.channel_config import ChannelConfig  # noqa: E402
    from src.user_permissions import UserPermissions  # noqa: E402
    _channel_config = ChannelConfig()
    _user_permissions = UserPermissions()
    _CHANNEL_CONFIG_ENABLED = True
except ImportError:
    _channel_config = None  # type: ignore[assignment]
    _user_permissions = None  # type: ignore[assignment]
    _CHANNEL_CONFIG_ENABLED = False

# Configuration from environment
SLACK_BOT_TOKEN = os.environ.get("LOBSTER_SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("LOBSTER_SLACK_APP_TOKEN", "")
# Optional user token (xoxp-) for outbound messages — makes replies appear as
# the user rather than the app bot.  Falls back to the bot token if not set.
SLACK_USER_TOKEN = os.environ.get("LOBSTER_SLACK_USER_TOKEN", "")

if not SLACK_BOT_TOKEN:
    raise ValueError("LOBSTER_SLACK_BOT_TOKEN environment variable is required")
if not SLACK_APP_TOKEN:
    raise ValueError("LOBSTER_SLACK_APP_TOKEN environment variable is required (starts with xapp-)")

# Optional: Restrict to specific channel IDs or user IDs
ALLOWED_CHANNELS = [x.strip() for x in os.environ.get("LOBSTER_SLACK_ALLOWED_CHANNELS", "").split(",") if x.strip()]
ALLOWED_USERS = [x.strip() for x in os.environ.get("LOBSTER_SLACK_ALLOWED_USERS", "").split(",") if x.strip()]

# Channel-conversation mode: channel IDs where the bot responds to ALL messages
# without requiring an @mention. Treat these channels like DM conversations.
# Set LOBSTER_SLACK_CHANNEL_CONVERSATIONS to a comma-separated list of channel IDs.
# When empty, the bot does not respond to any non-DM channel messages (safe default).
CHANNEL_CONVERSATIONS = [
    x.strip()
    for x in os.environ.get("LOBSTER_SLACK_CHANNEL_CONVERSATIONS", "").split(",")
    if x.strip()
]

# Typing indicator (post-then-update pattern).
# When enabled, outbound replies are first posted as a "..." placeholder and
# then immediately updated with the real text — so the user sees the message
# appear at once rather than waiting in silence until the full reply is ready.
# Disable with LOBSTER_SLACK_TYPING_INDICATOR=false (or 0) if the Slack plan
# does not permit chat.update (e.g. free-tier restrictions).
_TYPING_INDICATOR_RAW = os.environ.get("LOBSTER_SLACK_TYPING_INDICATOR", "true").strip().lower()
SLACK_TYPING_INDICATOR: bool = _TYPING_INDICATOR_RAW not in ("false", "0", "no", "off")

# The placeholder text posted before the real reply is ready.
SLACK_TYPING_PLACEHOLDER = "..."


def parse_channel_remap(raw: str) -> dict:
    """Parse ``LOBSTER_SLACK_CHANNEL_REMAP`` into a channel-ID mapping dict.

    Format: ``SRC1:DST1,SRC2:DST2``

    This mapping covers both inbound and outbound channel remapping.  The
    canonical use-case is redirecting the bot-DM channel (which the xoxp-
    user token cannot post to) to the equivalent user-DM channel.  Both the
    source and destination values are workspace-specific and must come from
    config — they must never be hardcoded.

    Entries that do not contain a colon separator are silently skipped.
    Leading/trailing whitespace is stripped from each token.

    Returns an empty dict when *raw* is empty or blank.
    """
    result: dict = {}
    if not raw or not raw.strip():
        return result
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        src, dst = entry.split(":", 1)
        src, dst = src.strip(), dst.strip()
        if src and dst:
            result[src] = dst
    return result


# Channel remap: maps source channel IDs to destination channel IDs for both
# inbound and outbound message paths.  Populated from LOBSTER_SLACK_CHANNEL_REMAP
# at startup — no channel IDs are hardcoded in the source.
#
# Example config.env entry:
#   LOBSTER_SLACK_CHANNEL_REMAP=<bot-dm-channel-id>:<user-dm-channel-id>
CHANNEL_REMAP: dict = parse_channel_remap(
    os.environ.get("LOBSTER_SLACK_CHANNEL_REMAP", "")
)


def _remap_channel(channel_id: str) -> str:
    """Return the remapped channel ID, or *channel_id* unchanged if no mapping exists.

    Both the inbound (Socket Mode) and outbound (chat_postMessage) paths share
    this helper so the remap logic lives in exactly one place.

    The mapping is workspace-specific and comes entirely from the
    ``LOBSTER_SLACK_CHANNEL_REMAP`` config variable — see ``parse_channel_remap``
    for the format.  A typical deployment maps the bot-DM channel (which the
    xoxp- user token cannot post to) to the user-DM channel.
    """
    return CHANNEL_REMAP.get(channel_id, channel_id)

# Directories
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))

INBOX_DIR = _MESSAGES / "inbox"
OUTBOX_DIR = _MESSAGES / "outbox"
IMAGES_DIR = _MESSAGES / "images"
FILES_DIR = _MESSAGES / "files"

# Ensure directories exist
INBOX_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

# Logging
LOG_DIR = _WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("lobster-slack")
log.setLevel(logging.INFO)
_file_handler = RotatingFileHandler(
    LOG_DIR / "slack-router.log",
    maxBytes=5 * 1024 * 1024,  # 5MB
    backupCount=3,
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_file_handler)
log.addHandler(logging.StreamHandler())

# Initialize Slack app
app = App(token=SLACK_BOT_TOKEN)
client = WebClient(token=SLACK_BOT_TOKEN)
# Separate client for outbound postMessage — uses user token if available so
# replies appear as the user rather than the app bot.  Falls back to the bot
# token if not set.
_outbound_token = SLACK_USER_TOKEN or SLACK_BOT_TOKEN
user_client = WebClient(token=_outbound_token)
if SLACK_USER_TOKEN:
    log.info("Outbound messages will use user token (xoxp-) — replies appear as user")
else:
    log.info("No LOBSTER_SLACK_USER_TOKEN set — outbound messages use bot token")

# Cache for user info and channel info
user_cache = {}
channel_cache = {}

# ---------------------------------------------------------------------------
# Inbound typing-indicator state
#
# Maps channel_id → deque of (slack_ts, placeholder_ts) pairs in arrival order.
# Populated when a "..." placeholder is posted on message arrival; consumed by
# _send_slack_reply when the real reply is ready.
#
# Using a deque-per-channel FIFO so that if multiple messages arrive in quick
# succession for the same channel, each real reply consumes the correct
# placeholder in arrival order.
# ---------------------------------------------------------------------------
_placeholder_queue: dict[str, collections.deque] = collections.defaultdict(
    collections.deque
)


def get_user_info(user_id: str) -> dict:
    """Get user information from Slack API with caching."""
    if user_id in user_cache:
        return user_cache[user_id]

    try:
        result = client.users_info(user=user_id)
        user_info = result.get("user", {})
        user_cache[user_id] = user_info
        return user_info
    except SlackApiError as e:
        log.warning(f"Error fetching user info for {user_id}: {e}")
        return {}


def get_channel_info(channel_id: str) -> dict:
    """Get channel information from Slack API with caching."""
    if channel_id in channel_cache:
        return channel_cache[channel_id]

    try:
        result = client.conversations_info(channel=channel_id)
        channel_info = result.get("channel", {})
        channel_cache[channel_id] = channel_info
        return channel_info
    except SlackApiError as e:
        log.warning(f"Error fetching channel info for {channel_id}: {e}")
        return {}


def is_authorized(channel_id: str, user_id: str) -> bool:
    """Check if the message is from an authorized channel/user."""
    # If no restrictions configured, allow all
    if not ALLOWED_CHANNELS and not ALLOWED_USERS:
        return True

    # Check channel allowlist
    if ALLOWED_CHANNELS and channel_id in ALLOWED_CHANNELS:
        return True

    # Check user allowlist
    if ALLOWED_USERS and user_id in ALLOWED_USERS:
        return True

    return False


def is_dm_channel(channel_id: str) -> bool:
    """Check if a channel is a direct message channel."""
    channel_info = get_channel_info(channel_id)
    return channel_info.get("is_im", False)


def clean_slack_text(text: str, bot_user_id: str = None) -> str:
    """Clean Slack message text, removing bot mentions and converting user mentions."""
    if not text:
        return ""

    # Remove bot mention if present (for @mentions in channels)
    if bot_user_id:
        text = re.sub(rf'<@{bot_user_id}>\s*', '', text)

    # Convert user mentions from <@U123ABC> to @username
    def replace_user_mention(match):
        uid = match.group(1)
        user_info = get_user_info(uid)
        display_name = user_info.get("profile", {}).get("display_name") or user_info.get("name", uid)
        return f"@{display_name}"

    text = re.sub(r'<@(U[A-Z0-9]+)>', replace_user_mention, text)

    # Convert channel mentions from <#C123ABC|channel-name> to #channel-name
    text = re.sub(r'<#[A-Z0-9]+\|([^>]+)>', r'#\1', text)

    # Convert URLs from <http://example.com|example.com> to http://example.com
    text = re.sub(r'<(https?://[^|>]+)\|[^>]+>', r'\1', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)

    return text.strip()


def write_message_to_inbox(msg_data: dict) -> None:
    """Write a message to the inbox directory."""
    msg_id = msg_data.get("id", f"{int(time.time() * 1000)}_slack")
    inbox_file = INBOX_DIR / f"{msg_id}.json"

    with open(inbox_file, 'w') as f:
        json.dump(msg_data, f, indent=2)

    log.info(f"Wrote message to inbox: {msg_id}")


def _post_typing_placeholder(channel_id: str, thread_ts: str | None, slack_ts: str) -> str | None:
    """Post a "..." placeholder to *channel_id* and register it in the queue.

    Returns the Slack ``ts`` of the placeholder message, or None if the post
    failed (non-fatal — the message is still written to inbox without it).

    The placeholder is posted using the user token so it appears as the user
    identity and can be updated later by ``chat.update`` (which also requires
    the same token that created the message).

    This function is only called when ``SLACK_TYPING_INDICATOR`` is enabled.
    """
    if not SLACK_TYPING_INDICATOR:
        return None
    try:
        kwargs: dict = {"channel": channel_id, "text": SLACK_TYPING_PLACEHOLDER}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = user_client.chat_postMessage(**kwargs)
        placeholder_ts = resp.get("ts")
        if placeholder_ts:
            _placeholder_queue[channel_id].append((slack_ts, placeholder_ts))
            log.info(
                "Posted inbound typing placeholder to %s (ts=%s)",
                channel_id, placeholder_ts,
            )
        return placeholder_ts
    except SlackApiError as exc:
        log.warning(
            "Failed to post inbound typing placeholder to %s: %s — proceeding without it",
            channel_id, exc,
        )
        return None


# Get bot user ID on startup
try:
    auth_response = client.auth_test()
    BOT_USER_ID = auth_response.get("user_id")
    BOT_NAME = auth_response.get("user")
    log.info(f"Connected as bot: {BOT_NAME} ({BOT_USER_ID})")
except SlackApiError as e:
    log.error(f"Failed to get bot info: {e}")
    BOT_USER_ID = None
    BOT_NAME = None

# Resolve the xoxp- user identity at startup so the poll loop can filter out
# Lobster's own outbound messages.  This must come from auth.test on the user
# token — the value is workspace-specific and must never be hardcoded.
POLL_SELF_USER_ID: str | None = None
if SLACK_USER_TOKEN:
    try:
        _user_auth = user_client.auth_test()
        POLL_SELF_USER_ID = _user_auth.get("user_id")
        log.info("User token identity resolved: %s", POLL_SELF_USER_ID)
    except SlackApiError as e:
        log.error("Failed to resolve user token identity: %s", e)


@app.event("message")
def handle_message_events(body, say, logger):
    """Handle incoming message events."""
    event = body.get("event", {})

    # Ignore bot messages, message_changed, message_deleted, etc.
    subtype = event.get("subtype")
    if subtype in ["bot_message", "message_changed", "message_deleted", "channel_join", "channel_leave"]:
        return

    # Ignore messages from bots (including ourselves)
    if event.get("bot_id"):
        return

    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts")
    ts = event.get("ts")

    if not user_id or not channel_id:
        return

    # Deduplicate by ts — Socket Mode delivers each event once, but guard anyway
    # (also prevents double-processing if the same event arrives via both
    # Socket Mode and any future polling path).
    if ts and ts in _seen_ts:
        log.debug("Skipping already-seen ts=%s from channel=%s", ts, channel_id)
        return
    if ts:
        _seen_ts.add(ts)
        _trim_seen_ts()

    # --- Inbound channel remap (BEFORE authorization check) ---
    # Remap must run first so that ALLOWED_CHANNELS is checked against the
    # canonical (post-remap) channel ID.  If the remap ran after the allowlist
    # check, a pre-remap channel ID that is absent from ALLOWED_CHANNELS would
    # be silently dropped before remapping could occur.
    remapped_id = _remap_channel(channel_id)
    if remapped_id != channel_id:
        log.info("Inbound: remapping channel %s → %s", channel_id, remapped_id)
        channel_id = remapped_id

    # --- Ingress logging (BEFORE authorization / LLM routing) ---
    if _INGRESS_LOGGING_ENABLED and _ingress_logger is not None:
        try:
            _user_info = get_user_info(user_id)
            _channel_info = get_channel_info(channel_id)
            _ingress_logger.log_message(
                event=event,
                channel_id=channel_id,
                channel_name=_channel_info.get("name", channel_id),
                user_id=user_id,
                username=_user_info.get("name", user_id),
                display_name=(
                    _user_info.get("profile", {}).get("display_name")
                    or _user_info.get("real_name", "")
                ),
                is_dm=_channel_info.get("is_im", False),
            )
        except Exception:
            log.exception("Ingress logging failed (non-fatal)")

    # --- Channel config routing gate (Phase 3) ---
    # If channel config is available, use it for routing decisions.
    # Otherwise, fall back to the legacy authorization + mention check.
    _is_mention = bool(BOT_USER_ID and f"<@{BOT_USER_ID}>" in text)

    # Get user and channel info (needed by both paths)
    user_info = get_user_info(user_id)
    channel_info = get_channel_info(channel_id)

    username = user_info.get("name", user_id)
    display_name = user_info.get("profile", {}).get("display_name") or user_info.get("real_name", username)
    channel_name = channel_info.get("name", channel_id)
    is_dm = channel_info.get("is_im", False)

    if _CHANNEL_CONFIG_ENABLED and _channel_config is not None:
        # Phase 3 routing: channel mode + user permissions
        if not _channel_config.should_route_to_llm(
            channel_id, event_type="message", is_mention=_is_mention, is_dm=is_dm,
        ):
            log.debug(
                "Channel config: not routing channel=%s mode=%s",
                channel_id, _channel_config.get_channel_mode(channel_id),
            )
            return

        # User permissions check (allowlist)
        if _user_permissions is not None and not _user_permissions.can_address_lobster(user_id):
            log.debug("User %s not permitted by allowlist, skipping LLM routing", user_id)
            return
    else:
        # Legacy path: env-var authorization + mention check
        if not is_authorized(channel_id, user_id):
            log.warning(f"Unauthorized message from channel={channel_id} user={user_id}")
            return

        # For channel messages: respond if mentioned OR if the channel is in
        # CHANNEL_CONVERSATIONS (bot responds to all messages in those channels).
        # DMs always respond unconditionally.
        if not is_dm and BOT_USER_ID:
            is_channel_conversation = channel_id in CHANNEL_CONVERSATIONS
            if not is_channel_conversation and f"<@{BOT_USER_ID}>" not in text:
                return

    # Clean the text
    cleaned_text = clean_slack_text(text, BOT_USER_ID)

    # Generate message ID
    msg_id = f"{int(time.time() * 1000)}_{ts.replace('.', '')}"

    # Create message data
    msg_data = {
        "id": msg_id,
        "source": "slack",
        "type": "text",
        "chat_id": channel_id,
        "user_id": user_id,
        "username": username,
        "user_name": display_name,
        "text": cleaned_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slack_ts": ts,
        "channel_name": channel_name,
        "is_dm": is_dm,
    }

    # Add thread info if this is a thread reply
    if thread_ts:
        msg_data["thread_ts"] = thread_ts

    # Handle file attachments
    files = event.get("files", [])
    if files:
        msg_data["files"] = []
        for f in files:
            file_info = {
                "id": f.get("id"),
                "name": f.get("name"),
                "mimetype": f.get("mimetype"),
                "size": f.get("size"),
                "url": f.get("url_private"),
            }
            msg_data["files"].append(file_info)

            # Download images
            mimetype = f.get("mimetype", "")
            if mimetype.startswith("image/"):
                try:
                    download_slack_file(f, msg_id, msg_data)
                except Exception as e:
                    log.error(f"Error downloading file: {e}")

    # Post "..." typing indicator immediately so the user sees a response
    # appear right away, before Lobster has had a chance to process the message.
    # Store placeholder_ts in msg_data so the outbox watcher can update it
    # in-place with the real reply.
    placeholder_ts = _post_typing_placeholder(channel_id, thread_ts, ts)
    if placeholder_ts:
        msg_data["placeholder_ts"] = placeholder_ts

    write_message_to_inbox(msg_data)


@app.event("app_mention")
def handle_app_mention(body, say, logger):
    """Handle @mentions of the bot - processed via message handler."""
    # The message event handler already handles mentions
    pass


def download_slack_file(file_info: dict, msg_id: str, msg_data: dict) -> None:
    """Download a file from Slack."""
    import urllib.request

    url = file_info.get("url_private")
    name = file_info.get("name", "file")
    mimetype = file_info.get("mimetype", "")

    if not url:
        return

    # Determine save path
    ext = Path(name).suffix
    if mimetype.startswith("image/"):
        save_path = IMAGES_DIR / f"{msg_id}{ext}"
        msg_data["image_file"] = str(save_path)
        msg_data["type"] = "photo"
    else:
        save_path = FILES_DIR / f"{msg_id}{ext}"
        msg_data["file_path"] = str(save_path)
        msg_data["type"] = "document"

    # Download with authorization header
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {SLACK_BOT_TOKEN}")

    with urllib.request.urlopen(req) as response:
        with open(save_path, 'wb') as f:
            f.write(response.read())

    log.info(f"Downloaded file to: {save_path}")


def _send_slack_reply(reply: dict) -> bool:
    """Pure send function for the shared OutboxFileHandler.

    Handles optional thread replies by passing ``thread_ts`` from the reply
    dict to ``chat_postMessage``.

    All outbound messages use ``user_client`` (xoxp-) when a user token is
    configured — this makes replies appear as the user identity rather than
    the app bot.  If the reply targets a channel that LOBSTER_SLACK_CHANNEL_REMAP
    maps to a different channel (e.g. the bot-DM channel that xoxp- cannot
    access), it is remapped before posting.

    When ``SLACK_TYPING_INDICATOR`` is enabled (the default), this function
    uses a two-phase inbound-then-outbound typing pattern:

    Phase 1 (inbound): When a message arrives, ``_post_typing_placeholder``
    immediately posts "..." to Slack and enqueues ``(slack_ts, placeholder_ts)``
    in ``_placeholder_queue[channel_id]``.  The user sees the message appear
    the moment their message lands.

    Phase 2 (outbound, here): When the real reply is ready, pop the oldest
    placeholder for the channel and call ``chat.update`` with the real text.

    Fallback: if no pending placeholder exists in the queue (e.g. proactive
    messages, or placeholder post failed earlier), use the legacy
    ``_send_with_typing_indicator`` path which posts "..." and immediately
    updates it.  If ``chat.update`` fails, fall back to ``chat_postMessage``.
    """
    channel_id = reply.get("chat_id", "")
    text = reply.get("text", "")
    thread_ts = reply.get("thread_ts")

    # Apply outbound channel remap from config — no hardcoded channel IDs.
    remapped = _remap_channel(channel_id)
    if remapped != channel_id:
        log.info("Outbound: remapping channel %s → %s", channel_id, remapped)
        channel_id = remapped

    if SLACK_TYPING_INDICATOR:
        # Check if an inbound typing placeholder is waiting for this channel.
        pending = _placeholder_queue.get(channel_id)
        if pending:
            _slack_ts, placeholder_ts = pending.popleft()
            return _update_placeholder(channel_id, placeholder_ts, text, thread_ts)
        # No pending inbound placeholder (proactive message or placeholder post
        # failed earlier) — fall back to the legacy post-then-update pattern.
        return _send_with_typing_indicator(channel_id, text, thread_ts)
    return _send_direct(channel_id, text, thread_ts)


def _build_post_kwargs(channel_id: str, text: str, thread_ts: str | None) -> dict:
    """Return the kwargs dict for chat.postMessage or chat.update (pure helper)."""
    kwargs: dict = {"channel": channel_id, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    return kwargs


def _send_direct(channel_id: str, text: str, thread_ts: str | None) -> bool:
    """Post *text* to *channel_id* directly via chat.postMessage.

    Returns True on success, False on SlackApiError.
    """
    try:
        user_client.chat_postMessage(**_build_post_kwargs(channel_id, text, thread_ts))
        log.info("Sent Slack reply to %s: %s...", channel_id, text[:50])
        return True
    except SlackApiError as exc:
        log.error("Error sending Slack message: %s", exc)
        return False


def _update_placeholder(
    channel_id: str, placeholder_ts: str, text: str, thread_ts: str | None
) -> bool:
    """Update an existing inbound "..." placeholder with the real reply text.

    Calls ``chat.update`` on the placeholder message.  If that fails (e.g.
    message deleted by the user), falls back to ``chat.postMessage`` with the
    real text so the reply is never silently lost.

    Returns True whenever the real text reaches Slack (via update or fallback).
    """
    try:
        update_kwargs: dict = {"channel": channel_id, "ts": placeholder_ts, "text": text}
        user_client.chat_update(**update_kwargs)
        log.info(
            "Updated inbound placeholder ts=%s → real reply in %s: %s...",
            placeholder_ts, channel_id, text[:50],
        )
        return True
    except SlackApiError as exc:
        log.warning(
            "chat.update failed for inbound placeholder ts=%s in %s: %s — "
            "falling back to postMessage",
            placeholder_ts, channel_id, exc,
        )
        return _send_direct(channel_id, text, thread_ts)


def _send_with_typing_indicator(channel_id: str, text: str, thread_ts: str | None) -> bool:
    """Deliver *text* to *channel_id* using the legacy post-then-update typing pattern.

    Used as a fallback when no inbound placeholder is queued (e.g. proactive
    messages sent without a prior user message).

    Posts ``SLACK_TYPING_PLACEHOLDER`` ("...") first so the user sees the
    message appear, then calls ``chat.update`` with the real text.

    Fallback: if the placeholder post fails, posts the real text directly.
    If the ``chat.update`` call fails after a successful placeholder, logs
    a warning and returns True (the placeholder is already in Slack; re-queuing
    would cause a duplicate).

    Returns True when the real text has been delivered (either via update or
    direct fallback), False only when all delivery attempts fail.
    """
    placeholder_kwargs = _build_post_kwargs(channel_id, SLACK_TYPING_PLACEHOLDER, thread_ts)
    try:
        post_response = user_client.chat_postMessage(**placeholder_kwargs)
        placeholder_ts = post_response.get("ts")
        log.info(
            "Posted typing placeholder to %s (ts=%s)",
            channel_id, placeholder_ts,
        )
    except SlackApiError as exc:
        log.warning(
            "Typing indicator placeholder failed (%s) — falling back to direct post",
            exc,
        )
        return _send_direct(channel_id, text, thread_ts)

    # Replace the placeholder with the real reply text.
    try:
        update_kwargs: dict = {"channel": channel_id, "ts": placeholder_ts, "text": text}
        if thread_ts:
            update_kwargs["thread_ts"] = thread_ts
        user_client.chat_update(**update_kwargs)
        log.info("Updated placeholder → real reply in %s: %s...", channel_id, text[:50])
        return True
    except SlackApiError as exc:
        # The placeholder "..." is already visible in Slack.  Returning False
        # would leave the outbox file in place and trigger a duplicate send.
        # Log the failure and return True to consume the outbox file.
        log.warning(
            "chat.update failed for placeholder ts=%s in %s: %s — "
            "placeholder remains visible; not re-queuing to avoid duplicate",
            placeholder_ts, channel_id, exc,
        )
        return True


# Backward-compatible alias -- code that imports OutboxHandler directly still works.
OutboxHandler = OutboxFileHandler


# ---------------------------------------------------------------------------
# User-DM poller: monitors channels that Socket Mode cannot see (e.g. the
# user-token DM channel where messages are sent to the user identity).
# ---------------------------------------------------------------------------

# Comma-separated list of channel IDs to poll with the user token.
# Set LOBSTER_SLACK_POLL_CHANNELS in config.env to the user-DM channel ID(s).
# No default is provided — the correct channel ID is workspace-specific.
_POLL_CHANNELS = [
    c.strip()
    for c in os.environ.get("LOBSTER_SLACK_POLL_CHANNELS", "").split(",")
    if c.strip()
]
_POLL_INTERVAL = int(os.environ.get("LOBSTER_SLACK_POLL_INTERVAL", "2"))  # seconds

# ---------------------------------------------------------------------------
# Outbox fallback scanner: periodic re-scan to catch files missed by watchdog.
#
# The watchdog inotify Observer can stop delivering events silently when:
#   - The kernel inotify queue overflows (IN_Q_OVERFLOW)
#   - An unhandled exception kills the observer thread
# In both cases the service stays alive but queued outbox files are not sent.
#
# This scanner runs in a separate thread and calls drain_outbox() on every
# tick so that any files the watchdog missed are caught within one interval.
# It also checks observer.is_alive() on every tick and logs a WARNING when
# the observer thread has died, making the failure visible in logs rather than
# silently accumulating queued messages.
# ---------------------------------------------------------------------------
OUTBOX_SCAN_INTERVAL: int = int(
    os.environ.get("LOBSTER_SLACK_OUTBOX_SCAN_INTERVAL", "30")
)

# Inform at startup if the user token is configured but no poll channels are set.
# In channel-conversation mode, this is expected — the user token is still used
# for outbound replies but DM polling is intentionally disabled.
if SLACK_USER_TOKEN and not _POLL_CHANNELS:
    log.info(
        "LOBSTER_SLACK_USER_TOKEN is set but LOBSTER_SLACK_POLL_CHANNELS is empty — "
        "user DM polling disabled (expected when using channel-conversation mode)."
    )

# State file to persist the last-seen timestamp across restarts
_POLL_STATE_FILE = _WORKSPACE / "data" / "slack-poll-state.json"

# In-memory seen-ts set to deduplicate within a run (fallback).
# Capped at _SEEN_TS_MAX_SIZE entries to prevent unbounded growth over long sessions.
_SEEN_TS_MAX_SIZE = 1000
_seen_ts: set = set()


def _trim_seen_ts() -> None:
    """Evict the oldest half of _seen_ts when the set exceeds _SEEN_TS_MAX_SIZE.

    Timestamps are Slack's float-string format (e.g. "1234567890.000100").
    Sorting them lexicographically is safe because they share the same integer
    prefix length and the decimal portion is zero-padded by Slack.
    """
    if len(_seen_ts) > _SEEN_TS_MAX_SIZE:
        # Keep the most-recent half; discard the oldest half.
        keep = sorted(_seen_ts)[len(_seen_ts) // 2:]
        _seen_ts.clear()
        _seen_ts.update(keep)


def _load_poll_state() -> dict:
    """Load last-seen timestamps from disk."""
    if _POLL_STATE_FILE.exists():
        try:
            return json.loads(_POLL_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_poll_state(state: dict) -> None:
    """Persist last-seen timestamps to disk."""
    try:
        _POLL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _POLL_STATE_FILE.write_text(json.dumps(state))
    except Exception as exc:
        log.warning("Could not save poll state: %s", exc)


def _poll_user_dm_channels(stop_event: Event) -> None:
    """Poll user-token DM channels for new messages and write them to inbox.

    This is the inbound path for DMs sent to the user identity (xoxp-).
    Socket Mode only receives events for the bot identity, so DMs sent to
    the user account are invisible to it.  We compensate by polling
    conversations.history at regular intervals.

    The self-user filter uses POLL_SELF_USER_ID, resolved from auth.test on
    the user token at startup — never a hardcoded user ID.
    """
    if not SLACK_USER_TOKEN:
        log.info("No LOBSTER_SLACK_USER_TOKEN — user DM polling disabled")
        return

    if not _POLL_CHANNELS:
        log.info("No LOBSTER_SLACK_POLL_CHANNELS — user DM polling disabled")
        return

    poll_client = WebClient(token=SLACK_USER_TOKEN)
    state = _load_poll_state()

    log.info(
        "User DM poller started: channels=%s interval=%ds",
        _POLL_CHANNELS, _POLL_INTERVAL,
    )

    while not stop_event.is_set():
        for channel_id in _POLL_CHANNELS:
            oldest = state.get(channel_id)
            try:
                kwargs: dict = {"channel": channel_id, "limit": 20}
                if oldest:
                    # Use exclusive lower bound: oldest + epsilon so the
                    # already-processed message is not re-delivered on restart.
                    # Format to exactly 6 decimal places — Slack ts format is
                    # XXXXXXXXXX.YYYYYY. Using str() on a Python float can
                    # produce 7+ decimal places (e.g. 1778907601.4552999)
                    # which Slack misparses: it absorbs the extra digit into
                    # the integer part, making the query jump far into the
                    # future and return zero messages.
                    kwargs["oldest"] = f"{float(oldest) + 0.000001:.6f}"

                resp = poll_client.conversations_history(**kwargs)
                messages = resp.get("messages", [])

                # API returns newest-first; reverse to process chronologically
                for msg in reversed(messages):
                    ts = msg.get("ts", "")
                    msg_user = msg.get("user", "")

                    if not ts:
                        continue

                    # Skip messages we've already seen
                    if ts in _seen_ts:
                        continue

                    # Skip messages sent by this Lobster instance's user identity.
                    # POLL_SELF_USER_ID is resolved from auth.test at startup —
                    # it is workspace-specific and is never hardcoded.
                    if POLL_SELF_USER_ID and msg_user == POLL_SELF_USER_ID:
                        _seen_ts.add(ts)
                        _trim_seen_ts()
                        if not oldest or float(ts) > float(oldest):
                            state[channel_id] = ts
                        continue

                    _seen_ts.add(ts)
                    _trim_seen_ts()

                    # Resolve display name
                    user_info = get_user_info(msg_user) if msg_user else {}
                    username = user_info.get("name", msg_user)
                    display_name = (
                        user_info.get("profile", {}).get("display_name")
                        or user_info.get("real_name", username)
                    )

                    text = clean_slack_text(msg.get("text", ""), BOT_USER_ID)
                    msg_id = f"{int(time.time() * 1000)}_{ts.replace('.', '')}"

                    msg_data = {
                        "id": msg_id,
                        "source": "slack",
                        "type": "text",
                        "chat_id": channel_id,
                        "user_id": msg_user,
                        "username": username,
                        "user_name": display_name,
                        "text": text,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "slack_ts": ts,
                        "channel_name": channel_id,
                        "is_dm": True,
                        "via_poll": True,
                    }

                    thread_ts = msg.get("thread_ts")
                    if thread_ts:
                        msg_data["thread_ts"] = thread_ts

                    # Post "..." typing indicator immediately (same as Socket Mode path)
                    placeholder_ts = _post_typing_placeholder(channel_id, thread_ts, ts)
                    if placeholder_ts:
                        msg_data["placeholder_ts"] = placeholder_ts

                    write_message_to_inbox(msg_data)
                    log.info(
                        "Polled new message from %s in %s: %s",
                        username, channel_id, repr(text[:60]),
                    )

                    # Advance the oldest pointer past this message
                    if not oldest or float(ts) > float(oldest):
                        state[channel_id] = ts

                if messages:
                    _save_poll_state(state)

            except SlackApiError as exc:
                log.warning("Poll error for channel %s: %s", channel_id, exc)
            except Exception as exc:
                log.exception("Unexpected poll error for channel %s: %s", channel_id, exc)

        stop_event.wait(timeout=_POLL_INTERVAL)

    log.info("User DM poller stopped")


# ---------------------------------------------------------------------------
# Channel-conversation poller: polls conversations.history for each channel in
# LOBSTER_SLACK_CHANNEL_CONVERSATIONS using the user token.
#
# Rate limit rationale:
#   conversations.history is Tier 3 (50+ calls/min across the method, not per
#   channel).  Polling 3 channels every 2s yields 90 calls/min — too high.
#   Safe approach: poll all channels in parallel within a single 2-second tick
#   (so N channels = N calls per tick), but enforce a token-bucket rate limiter
#   capped at 40 calls/min across all conversations.history calls.  A 429
#   response triggers exponential backoff per-channel.
# ---------------------------------------------------------------------------

# Per-channel backoff state (seconds to wait before next poll attempt).
_channel_backoff: dict[str, float] = {}
# Minimum seconds between consecutive polls of the same channel.
_CHANNEL_POLL_INTERVAL = int(os.environ.get("LOBSTER_SLACK_CHANNEL_POLL_INTERVAL", "2"))

# IDs to skip when polling channel conversations — prevents routing Lobster's
# own outbound messages back to itself.  These are resolved from auth.test at
# startup; the list here is populated at module load time using POLL_SELF_USER_ID
# which is set after the user token auth.test call above.
# Additional bot/user IDs from LOBSTER_SLACK_SKIP_USER_IDS env var are merged in.
_SKIP_USER_IDS_RAW = [
    u.strip()
    for u in os.environ.get("LOBSTER_SLACK_SKIP_USER_IDS", "").split(",")
    if u.strip()
]

# ---------------------------------------------------------------------------
# Simple token-bucket rate limiter for conversations.history calls.
# Limit: 40 calls/min = 1 call per 1.5 seconds on average.
# ---------------------------------------------------------------------------
_RATE_LIMIT_MAX_CALLS = 40      # calls allowed per minute
_RATE_LIMIT_WINDOW = 60.0       # window in seconds
_rate_limit_timestamps: collections.deque = collections.deque()


def _rate_limit_acquire() -> None:
    """Block until a conversations.history call token is available.

    Implements a sliding-window counter: keeps a deque of call timestamps and
    discards entries older than ``_RATE_LIMIT_WINDOW`` seconds.  If the window
    is full, sleeps until the oldest entry expires and a slot opens up.
    """
    while True:
        now = time.monotonic()
        # Drop timestamps outside the window
        while _rate_limit_timestamps and now - _rate_limit_timestamps[0] >= _RATE_LIMIT_WINDOW:
            _rate_limit_timestamps.popleft()
        if len(_rate_limit_timestamps) < _RATE_LIMIT_MAX_CALLS:
            _rate_limit_timestamps.append(now)
            return
        # Window is full — sleep until the oldest slot expires
        sleep_for = _RATE_LIMIT_WINDOW - (now - _rate_limit_timestamps[0]) + 0.1
        log.debug("Rate limiter: sleeping %.2fs before next conversations.history call", sleep_for)
        time.sleep(max(sleep_for, 0.1))


# State file key prefix for channel-conversation poller (separate from DM poller)
_CHANNEL_POLL_STATE_KEY_PREFIX = "conv_"


def _poll_channel_conversations(stop_event: Event) -> None:
    """Poll conversations.history for CHANNEL_CONVERSATIONS channels.

    Uses the user token so that messages sent by real workspace users (not just
    the bot) are visible.  Runs in a dedicated daemon thread alongside Socket
    Mode to catch messages that Socket Mode misses.

    Rate limit discipline:
    - Token-bucket limiter caps all conversations.history calls at 40/min
    - 429 responses trigger per-channel exponential backoff (8s, 16s, 32s, …)
    - Channels are polled in parallel within each 2-second tick

    Filtering:
    - Skip messages with a ``subtype`` field (bot_message, channel_join, etc.)
    - Skip messages whose ``user`` field matches POLL_SELF_USER_ID or any ID in
      LOBSTER_SLACK_SKIP_USER_IDS — prevents Lobster's own outbound messages
      from being routed back as inbound
    - Skip already-seen timestamps via ``_seen_ts``
    """
    if not SLACK_USER_TOKEN:
        log.info("No LOBSTER_SLACK_USER_TOKEN — channel-conversation polling disabled")
        return

    if not CHANNEL_CONVERSATIONS:
        log.info("No LOBSTER_SLACK_CHANNEL_CONVERSATIONS — channel-conversation polling disabled")
        return

    poll_client = WebClient(token=SLACK_USER_TOKEN)

    # Load persisted state; use a namespaced key to avoid collision with DM poller
    state = _load_poll_state()

    # Build the set of user IDs to skip (own identity + any configured extras)
    skip_ids: set[str] = set(_SKIP_USER_IDS_RAW)
    if POLL_SELF_USER_ID:
        skip_ids.add(POLL_SELF_USER_ID)

    # Initialise last_ts for any channel not already in state.
    # Look back LOBSTER_SLACK_CHANNEL_POLL_LOOKBACK seconds (default 300 = 5
    # minutes) so that messages sent while the router was restarting are not
    # silently dropped.  Once the channel's state is in the file it persists
    # across restarts, so the lookback only applies to first-ever startup for
    # a given channel.
    _LOOKBACK_SECS = int(os.environ.get("LOBSTER_SLACK_CHANNEL_POLL_LOOKBACK", "300"))
    lookback_ts = str(time.time() - _LOOKBACK_SECS)
    for ch in CHANNEL_CONVERSATIONS:
        key = _CHANNEL_POLL_STATE_KEY_PREFIX + ch
        if key not in state:
            state[key] = lookback_ts
            log.info(
                "Channel poller: initialising %s with lookback %ds (ts=%s)",
                ch, _LOOKBACK_SECS, lookback_ts,
            )
    _save_poll_state(state)

    log.info(
        "Channel-conversation poller started: channels=%s interval=%ds skip_ids=%s",
        CHANNEL_CONVERSATIONS, _CHANNEL_POLL_INTERVAL, skip_ids,
    )

    def _poll_one_channel(channel_id: str) -> None:
        """Poll a single channel; updates state in-place."""
        key = _CHANNEL_POLL_STATE_KEY_PREFIX + channel_id

        # Honour per-channel backoff
        backoff_until = _channel_backoff.get(channel_id, 0.0)
        if time.monotonic() < backoff_until:
            return

        oldest = state.get(key)

        # Acquire a rate-limit token before making the API call
        _rate_limit_acquire()

        try:
            kwargs: dict = {"channel": channel_id, "limit": 20}
            if oldest:
                kwargs["oldest"] = f"{float(oldest) + 0.000001:.6f}"

            resp = poll_client.conversations_history(**kwargs)
            messages = resp.get("messages", [])

            # API returns newest-first; reverse to process chronologically
            for msg in reversed(messages):
                ts = msg.get("ts", "")
                if not ts:
                    continue

                # Skip already-seen timestamps
                if ts in _seen_ts:
                    continue

                # Only add to seen after all skip checks so we don't
                # permanently ignore a message we should have processed.
                msg_user = msg.get("user", "")

                # Skip noise subtypes but allow file_share through so that
                # file uploads posted in channel-conversations are not silently
                # discarded.  file_share is the subtype Slack attaches to
                # messages that contain uploaded files.
                _SKIP_SUBTYPES = {
                    "bot_message",
                    "message_deleted",
                    "message_changed",
                    "channel_join",
                    "channel_leave",
                }
                if msg.get("subtype") in _SKIP_SUBTYPES:
                    _seen_ts.add(ts)
                    _trim_seen_ts()
                    if not oldest or float(ts) > float(oldest):
                        state[key] = ts
                    continue

                # Skip bot_id messages (bots that lack a subtype)
                if msg.get("bot_id"):
                    _seen_ts.add(ts)
                    _trim_seen_ts()
                    if not oldest or float(ts) > float(oldest):
                        state[key] = ts
                    continue

                # Skip messages from Lobster's own user identity or extra skip IDs
                if msg_user and msg_user in skip_ids:
                    _seen_ts.add(ts)
                    _trim_seen_ts()
                    if not oldest or float(ts) > float(oldest):
                        state[key] = ts
                    continue

                # Mark as seen
                _seen_ts.add(ts)
                _trim_seen_ts()

                # Resolve display name
                user_info = get_user_info(msg_user) if msg_user else {}
                username = user_info.get("name", msg_user)
                display_name = (
                    user_info.get("profile", {}).get("display_name")
                    or user_info.get("real_name", username)
                )

                # Resolve channel name
                ch_info = get_channel_info(channel_id)
                channel_name = ch_info.get("name", channel_id)

                text = clean_slack_text(msg.get("text", ""), BOT_USER_ID)
                msg_id = f"{int(time.time() * 1000)}_{ts.replace('.', '')}"

                msg_data = {
                    "id": msg_id,
                    "source": "slack",
                    "type": "text",
                    "chat_id": channel_id,
                    "user_id": msg_user,
                    "username": username,
                    "user_name": display_name,
                    "text": text,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "slack_ts": ts,
                    "channel_name": channel_name,
                    "is_dm": False,
                    "via_channel_poll": True,
                }

                thread_ts = msg.get("thread_ts")
                if thread_ts:
                    msg_data["thread_ts"] = thread_ts

                # Handle file attachments — mirrors the Socket Mode handler
                # (lines ~506-525).  file_share messages carry a "files" list
                # on the message dict just like Socket Mode events do.
                poll_files = msg.get("files", [])
                if poll_files:
                    msg_data["files"] = []
                    for f in poll_files:
                        file_info = {
                            "id": f.get("id"),
                            "name": f.get("name"),
                            "mimetype": f.get("mimetype"),
                            "size": f.get("size"),
                            "url": f.get("url_private"),
                        }
                        msg_data["files"].append(file_info)

                        # Download images to ~/messages/images/
                        mimetype = f.get("mimetype", "")
                        if mimetype.startswith("image/"):
                            try:
                                download_slack_file(f, msg_id, msg_data)
                            except Exception as e:
                                log.error(f"Channel poll: error downloading file: {e}")

                # Post "..." typing indicator immediately (same as Socket Mode path)
                placeholder_ts = _post_typing_placeholder(channel_id, thread_ts, ts)
                if placeholder_ts:
                    msg_data["placeholder_ts"] = placeholder_ts

                write_message_to_inbox(msg_data)
                log.info(
                    "Channel poll: new message from %s in %s: %s",
                    username, channel_name, repr(text[:60]),
                )

                # Advance the oldest pointer past this message
                if not oldest or float(ts) > float(oldest):
                    state[key] = ts
                    oldest = ts

            if messages:
                _save_poll_state(state)

        except SlackApiError as exc:
            resp_data = getattr(exc, "response", {}) or {}
            if resp_data.get("error") == "ratelimited":
                retry_after = int(
                    getattr(exc.response, "headers", {}).get("Retry-After", 8)
                    if hasattr(exc, "response") and exc.response is not None
                    else 8
                )
                # Double the Retry-After header for safety
                backoff = max(retry_after * 2, 8)
                _channel_backoff[channel_id] = time.monotonic() + backoff
                log.warning(
                    "Rate limited on channel %s — backing off %ds (Retry-After=%ds)",
                    channel_id, backoff, retry_after,
                )
            else:
                log.warning("Channel poll error for %s: %s", channel_id, exc)
        except Exception as exc:
            log.exception("Unexpected channel poll error for %s: %s", channel_id, exc)

    while not stop_event.is_set():
        # Poll all channels; run sequentially to keep rate-limiter straightforward.
        # The token-bucket handles throttling across all calls.
        for channel_id in CHANNEL_CONVERSATIONS:
            _poll_one_channel(channel_id)

        stop_event.wait(timeout=_CHANNEL_POLL_INTERVAL)

    log.info("Channel-conversation poller stopped")


def _scan_outbox_periodically(stop_event: Event, observer: object) -> None:
    """Periodically drain the outbox directory as a watchdog fallback.

    The watchdog inotify Observer can silently stop delivering ``on_created``
    events when the kernel inotify queue overflows or the observer thread
    dies.  This function runs in a dedicated daemon thread and calls
    ``drain_outbox`` on every ``OUTBOX_SCAN_INTERVAL``-second tick so that
    any files the watchdog missed are delivered within one scan period.

    On each tick it also calls ``observer.is_alive()`` and logs a WARNING
    when the observer thread has died, surfacing the failure in the service
    log rather than silently accumulating undelivered outbox files.

    Args:
        stop_event: Threading Event that signals the loop to exit.
        observer:   The watchdog Observer instance whose liveness is checked.
    """
    log.info(
        "Outbox fallback scanner started (interval=%ds)", OUTBOX_SCAN_INTERVAL
    )
    while not stop_event.is_set():
        if not observer.is_alive():
            log.warning(
                "Outbox watcher (watchdog Observer) thread is no longer alive — "
                "inotify events are not being delivered. "
                "Fallback scanner is compensating; consider restarting the service."
            )
        drain_outbox(OUTBOX_DIR, source="slack", send_fn=_send_slack_reply, log=log)
        stop_event.wait(timeout=OUTBOX_SCAN_INTERVAL)
    log.info("Outbox fallback scanner stopped")


def process_existing_outbox() -> None:
    """Process any Slack outbox files that exist on startup."""
    drain_outbox(OUTBOX_DIR, source="slack", send_fn=_send_slack_reply, log=log)


def main():
    """Main entry point."""
    log.info("Starting Lobster Slack Router...")
    log.info(f"Inbox: {INBOX_DIR}")
    log.info(f"Outbox: {OUTBOX_DIR}")

    if ALLOWED_CHANNELS:
        log.info(f"Allowed channels: {ALLOWED_CHANNELS}")
    if ALLOWED_USERS:
        log.info(f"Allowed users: {ALLOWED_USERS}")
    if not ALLOWED_CHANNELS and not ALLOWED_USERS:
        log.info("No restrictions configured - all channels and users allowed")
    if CHANNEL_REMAP:
        log.info("Channel remap active: %s", CHANNEL_REMAP)
    if CHANNEL_CONVERSATIONS:
        log.info("Channel-conversation mode active for: %s", CHANNEL_CONVERSATIONS)
    else:
        log.info("No LOBSTER_SLACK_CHANNEL_CONVERSATIONS configured — channel-conversation mode inactive")

    # Set up outbox watcher
    observer = Observer()
    observer.schedule(
        OutboxWatcher(source="slack", send_fn=_send_slack_reply, log=log),
        str(OUTBOX_DIR),
        recursive=False,
    )
    observer.start()
    log.info("Watching outbox for Slack replies...")

    # Process any existing outbox files
    drain_outbox(OUTBOX_DIR, source="slack", send_fn=_send_slack_reply, log=log)

    # Start user-DM poller thread (only when poll channels are configured)
    _poll_stop = Event()
    if _POLL_CHANNELS:
        _poll_thread = Thread(
            target=_poll_user_dm_channels,
            args=(_poll_stop,),
            daemon=True,
            name="user-dm-poller",
        )
        _poll_thread.start()
        log.info("User DM poller started for channels: %s", _POLL_CHANNELS)
    else:
        _poll_thread = None
        log.info("LOBSTER_SLACK_POLL_CHANNELS is empty — user DM polling disabled")

    # Start channel-conversation poller thread (only when channel conversations are configured)
    _conv_poll_stop = Event()
    if CHANNEL_CONVERSATIONS and SLACK_USER_TOKEN:
        _conv_poll_thread = Thread(
            target=_poll_channel_conversations,
            args=(_conv_poll_stop,),
            daemon=True,
            name="channel-conv-poller",
        )
        _conv_poll_thread.start()
        log.info(
            "Channel-conversation poller started for channels: %s", CHANNEL_CONVERSATIONS
        )
    else:
        _conv_poll_thread = None
        if not CHANNEL_CONVERSATIONS:
            log.info("LOBSTER_SLACK_CHANNEL_CONVERSATIONS is empty — channel-conversation polling disabled")
        elif not SLACK_USER_TOKEN:
            log.info("LOBSTER_SLACK_USER_TOKEN not set — channel-conversation polling disabled")

    # Start outbox fallback scanner thread.
    # This periodically re-scans the outbox directory to catch files missed by
    # the watchdog Observer (e.g. after an inotify queue overflow or observer
    # thread crash).  The shared stop event keeps shutdown clean.
    _scan_stop = Event()
    _scan_thread = Thread(
        target=_scan_outbox_periodically,
        args=(_scan_stop, observer),
        daemon=True,
        name="outbox-scan-fallback",
    )
    _scan_thread.start()

    # Start Socket Mode handler
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    try:
        log.info("Starting Socket Mode connection...")
        handler.start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        _poll_stop.set()
        _conv_poll_stop.set()
        _scan_stop.set()
        if _poll_thread is not None:
            _poll_thread.join(timeout=5)
        if _conv_poll_thread is not None:
            _conv_poll_thread.join(timeout=5)
        _scan_thread.join(timeout=5)
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
