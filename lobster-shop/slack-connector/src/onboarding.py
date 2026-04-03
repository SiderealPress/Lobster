"""Slack Connector — Onboarding Helpers.

Provides setup instructions and token validation for both bot and person
account paths. Pure validation functions at the core, side-effectful I/O
at the edges.

Design principles:
- Pure functions for instruction text generation and token format validation
- Side effects isolated at boundaries (token validation, config writes)
- Composable: bot and person paths share common config-write helpers
- Telegram-native interactive flow: multi-step guided setup over chat
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import account_mode

log = logging.getLogger("slack-onboarding")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIG_ENV_PATH = Path(
    os.environ.get("LOBSTER_CONFIG_DIR", Path.home() / "lobster-config")
) / "config.env"

BOT_TOKEN_PREFIX = "xoxb-"
APP_TOKEN_PREFIX = "xapp-"

REQUIRED_BOT_SCOPES = frozenset({
    "channels:history", "channels:read",
    "groups:history", "groups:read",
    "im:history", "im:read",
    "mpim:history", "mpim:read",
    "chat:write", "users:read",
    "reactions:read", "files:read",
})

REQUIRED_BOT_EVENTS = frozenset({
    "message.channels", "message.groups",
    "message.im", "message.mpim",
    "reaction_added", "app_mention",
    "file_shared",
})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenPair:
    """Immutable container for a validated Slack token pair."""
    bot_token: str
    app_token: str
    workspace_name: str


@dataclass(frozen=True)
class PrerequisiteResult:
    """Result of a single prerequisite check."""
    name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Result of token validation."""
    valid: bool
    message: str


# ---------------------------------------------------------------------------
# Pure validation functions
# ---------------------------------------------------------------------------

def validate_bot_token_format(token: str) -> ValidationResult:
    """Check that a bot token has valid xoxb- format.

    Pure function — no network calls.
    """
    token = token.strip()
    if not token:
        return ValidationResult(False, "Token is empty")
    if not token.startswith(BOT_TOKEN_PREFIX):
        return ValidationResult(
            False,
            f"Bot token must start with '{BOT_TOKEN_PREFIX}' — got '{token[:10]}...'"
        )
    parts = token.split("-")
    if len(parts) < 4:
        return ValidationResult(
            False,
            "Bot token format invalid — expected xoxb-TEAM_ID-BOT_ID-SECRET"
        )
    return ValidationResult(True, "Bot token format valid")


def validate_app_token_format(token: str) -> ValidationResult:
    """Check that an app-level token has valid xapp- format.

    Pure function — no network calls.
    """
    token = token.strip()
    if not token:
        return ValidationResult(False, "Token is empty")
    if not token.startswith(APP_TOKEN_PREFIX):
        return ValidationResult(
            False,
            f"App token must start with '{APP_TOKEN_PREFIX}' — got '{token[:10]}...'"
        )
    return ValidationResult(True, "App token format valid")


def check_python_version(
    major: int = 3,
    minor: int = 11,
    current: tuple[int, ...] | None = None,
) -> PrerequisiteResult:
    """Check that Python version meets minimum requirement."""
    version = current or sys.version_info[:2]
    passed = version >= (major, minor)
    return PrerequisiteResult(
        name="Python version",
        passed=passed,
        message=(
            f"Python {version[0]}.{version[1]} (>= {major}.{minor} required)"
            if passed
            else f"Python {version[0]}.{version[1]} found — {major}.{minor}+ required"
        ),
    )


def check_slack_bolt_installed() -> PrerequisiteResult:
    """Check that slack-bolt is importable."""
    try:
        import slack_bolt  # noqa: F401
        return PrerequisiteResult(
            name="slack-bolt",
            passed=True,
            message="slack-bolt is installed",
        )
    except ImportError:
        return PrerequisiteResult(
            name="slack-bolt",
            passed=False,
            message="slack-bolt not found — run: uv pip install 'slack-bolt>=1.18'",
        )


def check_config_env_writable(config_path: str | Path) -> PrerequisiteResult:
    """Check that config.env exists and is writable."""
    path = Path(config_path)
    if not path.exists():
        return PrerequisiteResult(
            name="config.env",
            passed=False,
            message=f"{path} does not exist",
        )
    if not os.access(path, os.W_OK):
        return PrerequisiteResult(
            name="config.env",
            passed=False,
            message=f"{path} is not writable",
        )
    return PrerequisiteResult(
        name="config.env",
        passed=True,
        message=f"{path} is writable",
    )


def check_all_prerequisites(
    config_path: str | Path,
) -> list[PrerequisiteResult]:
    """Run all prerequisite checks. Returns list of results."""
    return [
        check_python_version(),
        check_slack_bolt_installed(),
        check_config_env_writable(config_path),
    ]


def failed_prerequisites(results: list[PrerequisiteResult]) -> list[PrerequisiteResult]:
    """Filter to only failed prerequisites. Pure."""
    return [r for r in results if not r.passed]


# ---------------------------------------------------------------------------
# Pure functions — instruction text generation
# ---------------------------------------------------------------------------

def bot_instructions() -> str:
    """Return step-by-step instructions for bot account setup.

    Pure function: no I/O.
    """
    scopes = ", ".join(account_mode.get_required_scopes(account_mode.BOT))
    return f"""\
=== Slack Connector: Bot Account Setup ===

Step 1: Create a Slack App
  - Go to https://api.slack.com/apps -> "Create New App" -> "From scratch"
  - App name: "Lobster" (or any name you prefer)
  - Select your workspace

Step 2: Enable Socket Mode
  - App settings -> "Socket Mode" -> Enable
  - Create an App-Level Token with scope: connections:write
  - Save the token (starts with xapp-) -- this is LOBSTER_SLACK_APP_TOKEN

Step 3: Add Bot Token Scopes
  - OAuth & Permissions -> Bot Token Scopes:
    Required: {scopes}
    Optional: commands (for slash commands)

Step 4: Subscribe to Events
  - Event Subscriptions -> Enable -> Subscribe to Bot Events:
    message.channels, message.groups, message.im, message.mpim,
    reaction_added, app_mention, file_shared

Step 5: Install App to Workspace
  - OAuth & Permissions -> "Install to Workspace" -> Allow
  - Save the Bot User OAuth Token (starts with xoxb-) -- this is LOBSTER_SLACK_BOT_TOKEN

Step 6: Run the installer
  bash ~/lobster/lobster-shop/slack-connector/install.sh

Step 7: Invite Lobster to channels
  - In Slack: /invite @Lobster to each channel you want monitored
"""


def person_instructions() -> str:
    """Return step-by-step instructions for person (user-seat) account setup.

    Pure function: no I/O.
    """
    scopes = ", ".join(account_mode.get_required_scopes(account_mode.PERSON))
    return f"""\
=== Slack Connector: Person Account Setup ===

This path uses a real Slack user account. Lobster will appear as a human
team member, can read all messages in joined channels without @mentions,
and consumes a paid workspace seat.

Step 1: Create a dedicated Slack user account
  - Create an account with a distinct email (e.g., lobster@yourcompany.com)
  - Add the account to your Slack workspace
  - Set up 2FA and save credentials securely

Step 2: Obtain a user token (xoxp-)

  Option A (recommended): OAuth App with user scopes
    - Go to https://api.slack.com/apps -> "Create New App" -> "From scratch"
    - Under OAuth & Permissions, add these USER Token Scopes (not Bot):
      {scopes}
    - Install the app to your workspace
    - When prompted, authorize AS THE LOBSTER USER ACCOUNT (not your own!)
    - Copy the User OAuth Token (starts with xoxp-)

  Option B (development only -- DEPRECATED by Slack):
    - Go to https://api.slack.com/legacy/custom-integrations/legacy-tokens
    - Generate a legacy token while logged in as the Lobster user
    - WARNING: Legacy tokens are deprecated and may stop working.
      Use Option A for production setups.

Step 3: Run the installer with person mode
  SLACK_ACCOUNT_TYPE=person bash ~/lobster/lobster-shop/slack-connector/install.sh

Step 4: Add the Lobster user to channels
  - Invite the Lobster user account to channels in the Slack UI
  - In person mode, Lobster reads ALL messages in joined channels (not just @mentions)

=== Behavior Differences (Person vs Bot) ===

  - Person mode logs ALL channel messages, not just @mentions
  - Lobster will NOT respond to its own messages (self-message filtering)
  - The Lobster user appears in channel member lists
  - Rate limits follow user-tier (not bot-tier) rules
  - No Socket Mode -- person mode uses the Slack Web API for polling

=== Switching Back to Bot Mode ===

  /skill set slack-connector account_type bot
  -- or --
  Set LOBSTER_SLACK_ACCOUNT_TYPE=bot in config.env and restart
"""


def instructions_for_mode(mode: str) -> str:
    """Return setup instructions for the given account mode.

    Pure function: dispatches to bot_instructions() or person_instructions().
    """
    if mode == account_mode.PERSON:
        return person_instructions()
    return bot_instructions()


# ---------------------------------------------------------------------------
# Config file operations (side-effectful, isolated)
# ---------------------------------------------------------------------------

def read_config_tokens(config_path: str | Path) -> dict[str, str]:
    """Read existing Slack tokens from config.env.

    Returns dict with keys 'bot_token' and 'app_token', values empty string
    if not found.
    """
    path = Path(config_path)
    bot_token = ""
    app_token = ""

    if path.exists():
        content = path.read_text()
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("LOBSTER_SLACK_BOT_TOKEN="):
                bot_token = line.split("=", 1)[1].strip().strip("'\"")
            elif line.startswith("LOBSTER_SLACK_APP_TOKEN="):
                app_token = line.split("=", 1)[1].strip().strip("'\"")

    return {"bot_token": bot_token, "app_token": app_token}


def build_updated_config(
    existing_content: str,
    bot_token: str,
    app_token: str,
) -> str:
    """Build updated config.env content with new token values.

    Pure function: takes existing content string, returns new content string.
    Replaces existing token lines or appends new ones. Never duplicates.
    """
    lines = existing_content.splitlines()
    bot_found = False
    app_found = False
    result_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("LOBSTER_SLACK_BOT_TOKEN="):
            result_lines.append(f"LOBSTER_SLACK_BOT_TOKEN={bot_token}")
            bot_found = True
        elif stripped.startswith("LOBSTER_SLACK_APP_TOKEN="):
            result_lines.append(f"LOBSTER_SLACK_APP_TOKEN={app_token}")
            app_found = True
        else:
            result_lines.append(line)

    additions = []
    if not bot_found:
        additions.append(f"LOBSTER_SLACK_BOT_TOKEN={bot_token}")
    if not app_found:
        additions.append(f"LOBSTER_SLACK_APP_TOKEN={app_token}")

    if additions:
        if result_lines and result_lines[-1].strip():
            result_lines.append("")
        if not bot_found and not app_found:
            result_lines.append("# Slack Integration")
        result_lines.extend(additions)

    result = "\n".join(result_lines)
    if not result.endswith("\n"):
        result += "\n"

    return result


def write_tokens_to_config(
    config_path: str | Path,
    bot_token: str,
    app_token: str,
) -> None:
    """Write/update Slack tokens in config.env.

    Side effect: writes to disk. Idempotent — safe to call multiple times
    with the same values.
    """
    path = Path(config_path)
    existing = path.read_text() if path.exists() else ""
    updated = build_updated_config(existing, bot_token, app_token)
    path.write_text(updated)


def _read_config_env(config_path: Path | None = None) -> dict[str, str]:
    """Read config.env into a dict. Side effect: file read."""
    path = config_path or _CONFIG_ENV_PATH
    if not path.exists():
        return {}

    entries: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            value = value.strip().strip("'\"")
            entries[key.strip()] = value
    return entries


def _write_config_env(
    entries: dict[str, str], config_path: Path | None = None
) -> None:
    """Write/update config.env with new entries. Side effect: file write.

    Preserves existing entries and comments. Updates values for existing keys,
    appends new keys at the end.
    """
    path = config_path or _CONFIG_ENV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text().splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in entries:
                new_lines.append(f"{key}={entries[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    for key, value in entries.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    path.write_text("\n".join(new_lines) + "\n")
    path.chmod(0o600)


def _write_tokens_to_config(config_path: Path, tokens: dict[str, str]) -> None:
    """Idempotently update config.env with token key=value pairs. Sets mode 0o600.

    Uses re.sub to replace existing lines in-place, or appends new ones.
    Delegates to _write_config_env for the actual write (which also sets chmod 600).
    """
    _write_config_env(tokens, config_path=config_path)


def write_person_config(
    token: str, config_path: Path | None = None
) -> tuple[bool, str]:
    """Validate a person token and write it to config.env.

    Side effects: HTTP validation call, file write.

    Returns:
        (success, message) tuple.
    """
    valid, info = account_mode.validate_person_token(token)
    if not valid:
        return False, f"Token validation failed: {info.get('error', 'unknown error')}"

    _write_config_env(
        {
            "LOBSTER_SLACK_USER_TOKEN": token,
            "LOBSTER_SLACK_ACCOUNT_TYPE": "person",
        },
        config_path=config_path,
    )

    name = info.get("name", "unknown")
    team = info.get("team", "unknown")
    return True, f"Connected as {name} in workspace {team}. Config written."


def write_bot_config(
    bot_token: str,
    app_token: str,
    config_path: Path | None = None,
) -> tuple[bool, str]:
    """Validate a bot token and write both tokens to config.env.

    Side effects: HTTP validation call, file write.
    """
    valid, info = account_mode.validate_bot_token(bot_token)
    if not valid:
        return False, f"Token validation failed: {info.get('error', 'unknown error')}"

    _write_config_env(
        {
            "LOBSTER_SLACK_BOT_TOKEN": bot_token,
            "LOBSTER_SLACK_APP_TOKEN": app_token,
            "LOBSTER_SLACK_ACCOUNT_TYPE": "bot",
        },
        config_path=config_path,
    )

    name = info.get("name", "unknown")
    team = info.get("team", "unknown")
    return True, f"Connected as bot {name} in workspace {team}. Config written."


# ---------------------------------------------------------------------------
# Slack API validation (side-effectful — network call)
# ---------------------------------------------------------------------------

def validate_bot_token_with_api(token: str) -> tuple[bool, str]:
    """Call Slack auth.test to validate a bot token.

    Returns (valid, workspace_name_or_error).
    Side effect: makes an HTTP call to Slack API.
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return False, "slack-sdk not installed — cannot validate token"

    try:
        client = WebClient(token=token)
        response = client.auth_test()

        if response.get("ok"):
            team = response.get("team", "unknown workspace")
            user = response.get("user", "unknown bot")
            return True, f"{team} (bot: {user})"
        else:
            err = response.get("error", "unknown error")
            return False, f"auth.test failed: {err}"

    except SlackApiError as e:
        return False, f"Slack API error: {e.response.get('error', str(e))}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


# ---------------------------------------------------------------------------
# Setup instructions (pure — returns formatted text)
# ---------------------------------------------------------------------------

SLACK_APP_CREATION_GUIDE = """
┌─────────────────────────────────────────────────────────────────┐
│                  Slack App Setup Guide                          │
└─────────────────────────────────────────────────────────────────┘

Follow these steps to create your Slack App:

  1. Go to https://api.slack.com/apps
     → Click "Create New App" → "From scratch"
     → App name: "Lobster" (or any name you prefer)
     → Pick your workspace

  2. Enable Socket Mode
     → App settings → "Socket Mode" → Enable
     → Create an App-Level Token with scope: connections:write
     → Save the token (starts with xapp-) — this is your App Token

  3. Add Bot Token Scopes
     → OAuth & Permissions → Bot Token Scopes → Add:
       • channels:history    • channels:read
       • groups:history      • groups:read
       • im:history          • im:read
       • mpim:history        • mpim:read
       • chat:write          • users:read
       • reactions:read      • files:read

  4. Subscribe to Bot Events
     → Event Subscriptions → Enable → Subscribe to Bot Events:
       • message.channels    • message.groups
       • message.im          • message.mpim
       • reaction_added      • app_mention
       • file_shared

  5. Install App to Workspace
     → OAuth & Permissions → "Install to Workspace" → Allow
     → Save the Bot User OAuth Token (starts with xoxb-)
       — this is your Bot Token

After completing these steps, you'll need both tokens:
  • Bot Token  (xoxb-...) → LOBSTER_SLACK_BOT_TOKEN
  • App Token  (xapp-...) → LOBSTER_SLACK_APP_TOKEN
"""

SUCCESS_MESSAGE_TEMPLATE = """
┌─────────────────────────────────────────────────────────────────┐
│              Slack Connector Setup Complete!                     │
└─────────────────────────────────────────────────────────────────┘

  Workspace: {workspace}
  Bot Token: {bot_masked}
  App Token: {app_masked}

  Next steps:
    1. Invite Lobster to channels:  /invite @Lobster
    2. Activate the skill:          /skill activate slack-connector
    3. Configure channels:          edit {config_path}
"""


def mask_token(token: str) -> str:
    """Mask a token for display, showing only prefix and last 4 chars. Pure."""
    if len(token) <= 10:
        return token[:5] + "****"
    return token[:5] + "****" + token[-4:]


def format_success_message(
    workspace: str,
    bot_token: str,
    app_token: str,
    config_path: str,
) -> str:
    """Format the success message with masked tokens. Pure."""
    return SUCCESS_MESSAGE_TEMPLATE.format(
        workspace=workspace,
        bot_masked=mask_token(bot_token),
        app_masked=mask_token(app_token),
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# Telegram-native onboarding state
# ---------------------------------------------------------------------------

# Onboarding steps — defines the state machine
STEP_MODE_SELECT = "mode_select"
STEP_BOT_GUIDE_1 = "bot_guide_1"
STEP_BOT_GUIDE_2 = "bot_guide_2"
STEP_BOT_GUIDE_3 = "bot_guide_3"
STEP_BOT_GUIDE_4 = "bot_guide_4"
STEP_BOT_GUIDE_5 = "bot_guide_5"
STEP_BOT_TOKEN = "bot_token"
STEP_APP_TOKEN = "app_token"
STEP_CHANNEL_SELECT = "channel_select"
STEP_CHANNEL_MODES = "channel_modes"
STEP_CONFIRM = "confirm"
STEP_DONE = "done"
STEP_CANCELLED = "cancelled"

STEP_PERSON_TOKEN = "person_token"
STEP_PERSON_CHANNEL_SELECT = "person_channel_select"
STEP_PERSON_CONFIRM = "person_confirm"

_STATE_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
) / "slack-connector" / "state"


@dataclass
class OnboardingState:
    """Persistent state for a single user's onboarding flow."""

    chat_id: str
    step: str = STEP_MODE_SELECT
    mode: str = ""           # "bot" or "person"
    bot_token: str = ""
    app_token: str = ""
    person_token: str = ""
    workspace_name: str = ""
    available_channels: list[dict[str, Any]] = field(default_factory=list)
    selected_channels: list[str] = field(default_factory=list)
    channel_modes: dict[str, str] = field(default_factory=dict)
    # Stores the Telegram message_id of the last token message so it can be deleted
    last_token_message_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict. Pure."""
        return {
            "chat_id": self.chat_id,
            "step": self.step,
            "mode": self.mode,
            "bot_token": self.bot_token,
            "app_token": self.app_token,
            "person_token": self.person_token,
            "workspace_name": self.workspace_name,
            "available_channels": self.available_channels,
            "selected_channels": self.selected_channels,
            "channel_modes": self.channel_modes,
            "last_token_message_id": self.last_token_message_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OnboardingState":
        """Deserialize from dict. Pure."""
        return cls(
            chat_id=str(data.get("chat_id", "")),
            step=data.get("step", STEP_MODE_SELECT),
            mode=data.get("mode", ""),
            bot_token=data.get("bot_token", ""),
            app_token=data.get("app_token", ""),
            person_token=data.get("person_token", ""),
            workspace_name=data.get("workspace_name", ""),
            available_channels=data.get("available_channels", []),
            selected_channels=data.get("selected_channels", []),
            channel_modes=data.get("channel_modes", {}),
            last_token_message_id=data.get("last_token_message_id"),
        )


def _state_path(chat_id: str, state_dir: Path | None = None) -> Path:
    """Return the file path for onboarding state for a given chat_id. Pure."""
    d = state_dir or _STATE_DIR
    return d / f"onboarding_{chat_id}.json"


def get_onboarding_state(
    chat_id: str,
    state_dir: Path | None = None,
) -> OnboardingState:
    """Read onboarding state for chat_id from disk.

    Side effect: file read.
    Returns a fresh OnboardingState if none exists.
    """
    path = _state_path(str(chat_id), state_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return OnboardingState.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            pass
    return OnboardingState(chat_id=str(chat_id))


def save_onboarding_state(
    state: OnboardingState,
    state_dir: Path | None = None,
) -> None:
    """Write onboarding state to disk.

    Side effect: file write.
    """
    path = _state_path(state.chat_id, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2))
    path.chmod(0o600)


def clear_onboarding_state(
    chat_id: str,
    state_dir: Path | None = None,
) -> None:
    """Delete onboarding state file for chat_id.

    Side effect: file delete.
    """
    path = _state_path(str(chat_id), state_dir)
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Slack API helpers — side-effectful
# ---------------------------------------------------------------------------

def list_workspace_channels(
    token: str,
    _conversations_list_fn: Any = None,
) -> list[dict[str, Any]]:
    """Fetch public and private channels from Slack using conversations.list.

    Side effect: HTTP call to Slack API.

    Args:
        token: Bot or user token.
        _conversations_list_fn: Optional override for testing (dependency injection).

    Returns:
        List of dicts with keys: id, name, is_member, is_private.
        Returns empty list on error.
    """
    if _conversations_list_fn is not None:
        return _conversations_list_fn(token)

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        log.warning("slack_sdk not installed — cannot list channels")
        return []

    client = WebClient(token=token)
    channels: list[dict[str, Any]] = []

    try:
        cursor = None
        while True:
            kwargs: dict[str, Any] = {
                "types": "public_channel,private_channel",
                "exclude_archived": True,
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor

            resp = client.conversations_list(**kwargs)
            if not resp.get("ok"):
                log.warning("conversations.list failed: %s", resp.get("error"))
                break

            for ch in resp.get("channels", []):
                channels.append({
                    "id": ch.get("id", ""),
                    "name": ch.get("name", ""),
                    "is_member": ch.get("is_member", False),
                    "is_private": ch.get("is_private", False),
                })

            meta = resp.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor:
                break

    except SlackApiError as e:
        log.warning("Slack API error listing channels: %s", e)
    except Exception as e:
        log.warning("Unexpected error listing channels: %s", e)

    return channels


# ---------------------------------------------------------------------------
# Telegram helpers — side-effectful
# ---------------------------------------------------------------------------

def delete_telegram_message(
    chat_id: str | int,
    message_id: int,
    bot_token: str | None = None,
    _delete_fn: Any = None,
) -> bool:
    """Delete a Telegram message via Bot API.

    Used to remove token messages immediately after reading them for privacy.

    Side effect: HTTP call to Telegram Bot API.

    Args:
        chat_id: Telegram chat ID.
        message_id: The Telegram message ID to delete.
        bot_token: Telegram bot token. Reads TELEGRAM_BOT_TOKEN env var if None.
        _delete_fn: Optional override for testing.

    Returns:
        True if deletion succeeded, False otherwise.
    """
    if _delete_fn is not None:
        return _delete_fn(chat_id, message_id)

    tg_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tg_token:
        log.warning("delete_telegram_message: no bot token available")
        return False

    try:
        import urllib.request
        import urllib.error

        url = f"https://api.telegram.org/bot{tg_token}/deleteMessage"
        data = json.dumps({"chat_id": chat_id, "message_id": message_id}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as e:
        log.warning("Failed to delete Telegram message %s: %s", message_id, e)
        return False


# ---------------------------------------------------------------------------
# Config write helpers — side-effectful
# ---------------------------------------------------------------------------

_CHANNELS_CONFIG_PATH = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
) / "slack-connector" / "config" / "channels.yaml"

# Valid routing modes for channel configuration
CHANNEL_MODE_MONITOR = "monitor"
CHANNEL_MODE_MENTIONS = "mentions"
CHANNEL_MODE_FULL = "full"
CHANNEL_MODES = (CHANNEL_MODE_MONITOR, CHANNEL_MODE_MENTIONS, CHANNEL_MODE_FULL)


def build_channels_config(
    channel_selections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a channels.yaml structure from a list of channel selections.

    Pure function — takes channel data, returns a dict suitable for YAML serialization.

    Each item in channel_selections should have:
        id (str): Slack channel ID
        name (str): channel name
        mode (str): one of monitor / mentions / full

    Returns:
        Dict with "channels" key mapping channel IDs to config blocks.
    """
    channels: dict[str, Any] = {}
    for ch in channel_selections:
        ch_id = ch.get("id", "")
        if not ch_id:
            continue
        mode = ch.get("mode", CHANNEL_MODE_MONITOR)
        if mode not in CHANNEL_MODES:
            mode = CHANNEL_MODE_MONITOR
        channels[ch_id] = {
            "name": ch.get("name", ch_id),
            "mode": mode,
            "log_messages": True,
            "log_reactions": True,
            "log_edits": True,
            "log_deletes": False,
            "log_files": True,
        }
    return {"channels": channels}


def write_channels_config(
    channel_selections: list[dict[str, Any]],
    config_path: Path | None = None,
) -> None:
    """Write channels.yaml from a list of channel selections.

    Side effect: file write.

    Args:
        channel_selections: List of dicts with id, name, mode keys.
        config_path: Override path (defaults to workspace channels.yaml).
    """
    path = config_path or _CHANNELS_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    config_data = build_channels_config(channel_selections)
    path.write_text(yaml.dump(config_data, default_flow_style=False, sort_keys=False))


def restart_ingress_service(
    service_name: str = "lobster-slack-connector",
    _run_fn: Any = None,
) -> tuple[bool, str]:
    """Restart the Slack ingress service via systemctl.

    Side effect: subprocess call to systemctl.

    Args:
        service_name: systemd service name to restart.
        _run_fn: Optional override for testing (dependency injection).

    Returns:
        (success, message) tuple.
    """
    if _run_fn is not None:
        return _run_fn(service_name)

    try:
        result = subprocess.run(
            ["systemctl", "restart", service_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, f"Service {service_name} restarted."
        return False, f"systemctl exited {result.returncode}: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "systemctl not found — service restart skipped."
    except subprocess.TimeoutExpired:
        return False, f"systemctl restart timed out for {service_name}."
    except Exception as e:
        return False, f"Unexpected error restarting {service_name}: {e}"


# ---------------------------------------------------------------------------
# SlackOnboarding — orchestrator class
# ---------------------------------------------------------------------------

class SlackOnboarding:
    """Interactive setup flow for Slack bot account onboarding.

    Orchestrates the side-effectful steps (user prompts, API calls, file writes)
    while delegating validation to pure functions.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        input_fn=None,
        print_fn=None,
    ):
        self.config_path = Path(
            config_path or os.path.expanduser("~/lobster-config/config.env")
        )
        self._input = input_fn or input
        self._print = print_fn or print

    def check_prerequisites(self) -> list[str]:
        """Returns list of failed prerequisite descriptions, empty if all pass."""
        results = check_all_prerequisites(self.config_path)
        failures = failed_prerequisites(results)
        return [f.message for f in failures]

    def validate_bot_token(self, token: str) -> tuple[bool, str]:
        """Validate bot token format and call auth.test."""
        format_result = validate_bot_token_format(token)
        if not format_result.valid:
            return False, format_result.message
        return validate_bot_token_with_api(token)

    def validate_app_token(self, token: str) -> bool:
        """Validate xapp- token format."""
        return validate_app_token_format(token).valid

    def write_tokens_to_config(self, bot_token: str, app_token: str) -> None:
        """Write/update tokens in config.env."""
        write_tokens_to_config(self.config_path, bot_token, app_token)

    def _prompt_token(self, prompt: str, validator, max_attempts: int = 3):
        """Prompt user for a token with validation retry loop."""
        for attempt in range(max_attempts):
            token = self._input(prompt).strip()
            if not token:
                self._print("  Token cannot be empty. Please try again.")
                continue
            result = validator(token)
            if isinstance(result, tuple):
                valid, info = result
            else:
                valid, info = result, ""
            if valid:
                return token, info
            self._print(f"  Invalid: {info}")
            if attempt < max_attempts - 1:
                self._print("  Please try again.")
        return None, "Maximum attempts exceeded"

    def run_setup_wizard(self) -> bool:
        """Full interactive setup flow. Returns True on success."""
        self._print("\n=== Slack Connector — Bot Account Setup ===\n")

        self._print("Step 1: Checking prerequisites...")
        failures = self.check_prerequisites()
        if failures:
            self._print("\nPrerequisite checks failed:")
            for f in failures:
                self._print(f"  ✗ {f}")
            return False
        self._print("  ✓ All prerequisites met\n")

        self._print("Step 2: Checking existing tokens...")
        existing = read_config_tokens(self.config_path)
        if existing["bot_token"] and existing["app_token"]:
            self._print(f"  Bot token: {mask_token(existing['bot_token'])}")
            self._print(f"  App token: {mask_token(existing['app_token'])}")
            self._print("  Both tokens already configured — skipping to finalize.\n")
            return True

        if existing["bot_token"]:
            self._print(f"  Bot token: {mask_token(existing['bot_token'])} (found)")
        else:
            self._print("  Bot token: not configured")
        if existing["app_token"]:
            self._print(f"  App token: {mask_token(existing['app_token'])} (found)")
        else:
            self._print("  App token: not configured")
        self._print("")

        self._print("Step 3: Slack App Setup")
        self._print(SLACK_APP_CREATION_GUIDE)
        self._input("Press Enter when you've completed the steps above...")

        self._print("\nStep 4: Token Collection\n")

        if existing["bot_token"]:
            bot_token = existing["bot_token"]
            self._print(f"  Using existing bot token: {mask_token(bot_token)}")
            workspace = "(existing)"
        else:
            bot_token, workspace = self._prompt_token(
                "  Enter your Bot Token (xoxb-...): ",
                self.validate_bot_token,
            )
            if bot_token is None:
                self._print(f"\n  ✗ Bot token validation failed: {workspace}")
                return False
            self._print(f"  ✓ Bot token valid — workspace: {workspace}\n")

        if existing["app_token"]:
            app_token = existing["app_token"]
            self._print(f"  Using existing app token: {mask_token(app_token)}")
        else:
            app_token, _ = self._prompt_token(
                "  Enter your App Token (xapp-...): ",
                lambda t: (self.validate_app_token(t), ""),
            )
            if app_token is None:
                self._print("\n  ✗ App token validation failed")
                return False
            self._print("  ✓ App token format valid\n")

        self._print("  Writing tokens to config.env...")
        self.write_tokens_to_config(bot_token, app_token)
        self._print("  ✓ Tokens saved\n")

        self._print(
            format_success_message(
                workspace=workspace,
                bot_token=bot_token,
                app_token=app_token,
                config_path=str(self.config_path),
            )
        )
        return True
