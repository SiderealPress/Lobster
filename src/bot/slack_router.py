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

    # --- Inbound channel remap (BEFORE authorization check) ---
    # Remap must run first so that ALLOWED_CHANNELS is checked against the
    # canonical (post-remap) channel ID.  If the remap ran after the allowlist
    # check, a pre-remap channel ID that is absent from ALLOWED_CHANNELS would
    # be silently dropped before remapping could occur.
    if channel_id in CHANNEL_REMAP:
        remapped_id = CHANNEL_REMAP[channel_id]
        log.info(
            "Inbound: remapping channel %s → %s",
            channel_id, remapped_id,
        )
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

        # For channel messages, only respond if mentioned; DMs always respond
        if not is_dm and BOT_USER_ID:
            if f"<@{BOT_USER_ID}>" not in text:
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
    """
    channel_id = reply.get("chat_id", "")
    text = reply.get("text", "")
    thread_ts = reply.get("thread_ts")

    # Apply outbound channel remap from config — no hardcoded channel IDs.
    if channel_id in CHANNEL_REMAP:
        remapped = CHANNEL_REMAP[channel_id]
        log.info(
            "Outbound: remapping channel %s → %s",
            channel_id, remapped,
        )
        channel_id = remapped

    try:
        kwargs: dict = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        user_client.chat_postMessage(**kwargs)
        log.info("Sent Slack reply to %s: %s...", channel_id, text[:50])
        return True
    except SlackApiError as exc:
        log.error("Error sending Slack message: %s", exc)
        return False


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
_POLL_INTERVAL = int(os.environ.get("LOBSTER_SLACK_POLL_INTERVAL", "10"))  # seconds

# Warn at startup if the user token is configured but no poll channels are set.
# Without poll channels, DMs sent to the user identity will not be received.
if SLACK_USER_TOKEN and not _POLL_CHANNELS:
    log.warning(
        "LOBSTER_SLACK_USER_TOKEN is set but LOBSTER_SLACK_POLL_CHANNELS is empty — "
        "user DM polling disabled. Set LOBSTER_SLACK_POLL_CHANNELS to the channel "
        "ID(s) you want to poll so that DMs to the user identity are received."
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
                    kwargs["oldest"] = str(float(oldest) + 0.000001)

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

    # Start user-DM poller thread
    _poll_stop = Event()
    _poll_thread = Thread(
        target=_poll_user_dm_channels,
        args=(_poll_stop,),
        daemon=True,
        name="user-dm-poller",
    )
    _poll_thread.start()

    # Start Socket Mode handler
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    try:
        log.info("Starting Socket Mode connection...")
        handler.start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        _poll_stop.set()
        _poll_thread.join(timeout=5)
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
