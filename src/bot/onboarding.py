"""
Onboarding module for Lobster Bot.

Handles:
- Tracking which users have been onboarded
- Sending the onboarding/welcome message on first contact or /onboarding command
- Persisting onboarded state to disk
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("lobster")

CONFIG_DIR = Path.home() / "messages" / "config"
ONBOARDED_FILE = CONFIG_DIR / "onboarded_users.json"

# Ensure config directory exists
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _load_onboarded_users() -> dict:
    """Load the set of onboarded user IDs from disk.

    Returns a dict mapping user_id (str) -> onboarded timestamp.
    """
    if ONBOARDED_FILE.exists():
        try:
            with open(ONBOARDED_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.error(f"Error loading onboarded users file: {e}")
            return {}
    return {}


def _save_onboarded_users(data: dict) -> None:
    """Persist the onboarded users dict to disk."""
    try:
        with open(ONBOARDED_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        log.error(f"Error saving onboarded users file: {e}")


def is_user_onboarded(user_id: int) -> bool:
    """Check whether a user has already been onboarded."""
    data = _load_onboarded_users()
    return str(user_id) in data


def mark_user_onboarded(user_id: int) -> None:
    """Mark a user as onboarded and persist to disk."""
    from datetime import datetime

    data = _load_onboarded_users()
    data[str(user_id)] = datetime.utcnow().isoformat()
    _save_onboarded_users(data)
    log.info(f"Marked user {user_id} as onboarded")


def get_onboarding_message(user_name: str) -> str:
    """Return the full onboarding/welcome message for a user."""
    return (
        f"Welcome, {user_name}! I'm *Lobster* -- your always-on AI assistant.\n"
        "\n"
        "Here's what I can do:\n"
        "\n"
        "*Text* -- Ask me anything. I reply in seconds.\n"
        "\n"
        "*Photos* -- Send an image and I'll see it. "
        "Add a caption for context (e.g. \"What breed is this?\").\n"
        "\n"
        "*Voice notes* -- Hold the mic and talk. "
        "I transcribe locally with whisper.cpp -- no cloud, no API key.\n"
        "\n"
        "*Brain dumps* -- Record a stream-of-consciousness voice note. "
        "I'll triage it into a GitHub issue with action items. "
        "Start with \"brain dump\" or just ramble.\n"
        "\n"
        "*LobsterDrop* -- Sync large files via Syncthing. "
        "Bypasses Telegram's 20MB limit. Ask me \"how do I set up LobsterDrop?\" for steps.\n"
        "\n"
        "*Scheduled tasks* -- \"Check my GitHub PRs every morning at 9am.\" "
        "I'll set up a cron job and report back.\n"
        "\n"
        "*Code work* -- \"Work on issue #42\" -- "
        "I'll read the issue, branch, implement, and open a PR.\n"
        "\n"
        "*Change my behavior* -- Just tell me: "
        "\"be more concise\", \"use bullet points\", \"check on me daily\".\n"
        "\n"
        "*Commands*\n"
        "/start -- Show greeting\n"
        "/onboarding -- Show this guide again\n"
        "\n"
        "Send me a message anytime. I'm always listening."
    )


# Standalone constants for use by Lobster's main loop via send_reply.
# These can be imported directly when composing Telegram messages.

WELCOME_SHORT = (
    "Hey {name}! I'm Lobster, your always-on AI assistant.\n"
    "\n"
    "Send me a message and I'll reply. That's it.\n"
    "\n"
    "Type *help* to see what I can do."
)

HELP_MESSAGE = (
    "*Things you can send me:*\n"
    "- Text messages (questions, tasks, conversation)\n"
    "- Photos (with optional caption)\n"
    "- Voice notes (transcribed locally)\n"
    "- Files/documents (up to 20MB via Telegram)\n"
    "\n"
    "*Things you can ask me to do:*\n"
    "- \"Schedule a daily check-in at 9am\"\n"
    "- \"Work on GitHub issue #42\"\n"
    "- \"Summarize this document\" (attach file)\n"
    "- \"Change your behavior to be more concise\"\n"
    "- \"What are my open tasks?\"\n"
    "\n"
    "*Brain dumps:*\n"
    "Record a voice note starting with \"brain dump\" or just stream your thoughts. "
    "I'll create a GitHub issue with action items.\n"
    "\n"
    "*LobsterDrop:*\n"
    "For large files, ask \"how do I set up LobsterDrop?\""
)
