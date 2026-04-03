"""Slack Connector — Onboarding Helpers.

Provides setup instructions and token validation for both bot and person
account paths. Pure validation functions at the core, side-effectful I/O
at the edges.

Design principles:
- Pure functions for instruction text generation and token format validation
- Side effects isolated at boundaries (token validation, config writes)
- Composable: bot and person paths share common config-write helpers
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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
