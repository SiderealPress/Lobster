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
from datetime import datetime
from pathlib import Path
from threading import Thread

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

# Configuration from environment
SLACK_BOT_TOKEN = os.environ.get("LOBSTER_SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("LOBSTER_SLACK_APP_TOKEN", "")

if not SLACK_BOT_TOKEN:
    raise ValueError("LOBSTER_SLACK_BOT_TOKEN environment variable is required")
if not SLACK_APP_TOKEN:
    raise ValueError("LOBSTER_SLACK_APP_TOKEN environment variable is required (starts with xapp-)")

# Optional: Restrict to specific channel IDs or user IDs
ALLOWED_CHANNELS = [x.strip() for x in os.environ.get("LOBSTER_SLACK_ALLOWED_CHANNELS", "").split(",") if x.strip()]
ALLOWED_USERS = [x.strip() for x in os.environ.get("LOBSTER_SLACK_ALLOWED_USERS", "").split(",") if x.strip()]

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

    # Check authorization
    if not is_authorized(channel_id, user_id):
        log.warning(f"Unauthorized message from channel={channel_id} user={user_id}")
        return

    # Get user and channel info
    user_info = get_user_info(user_id)
    channel_info = get_channel_info(channel_id)

    username = user_info.get("name", user_id)
    display_name = user_info.get("profile", {}).get("display_name") or user_info.get("real_name", username)
    channel_name = channel_info.get("name", channel_id)
    is_dm = channel_info.get("is_im", False)

    # Clean the text
    cleaned_text = clean_slack_text(text, BOT_USER_ID)

    # For channel messages, only respond if mentioned
    # For DMs, always respond
    if not is_dm and BOT_USER_ID:
        # Check if bot was mentioned in original text
        if f"<@{BOT_USER_ID}>" not in text:
            return

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
        "timestamp": datetime.utcnow().isoformat(),
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
    """
    channel_id = reply.get("chat_id", "")
    text = reply.get("text", "")
    thread_ts = reply.get("thread_ts")

    try:
        kwargs: dict = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        client.chat_postMessage(**kwargs)
        log.info("Sent Slack reply to %s: %s...", channel_id, text[:50])
        return True
    except SlackApiError as exc:
        log.error("Error sending Slack message: %s", exc)
        return False


# Backward-compatible alias -- code that imports OutboxHandler directly still works.
OutboxHandler = OutboxFileHandler


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

    # Start Socket Mode handler
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    try:
        log.info("Starting Socket Mode connection...")
        handler.start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
