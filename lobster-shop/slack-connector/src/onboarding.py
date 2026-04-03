"""Slack Connector — Bot Account Onboarding

Interactive setup flow for Slack bot account (xoxb-) path.
Validates prerequisites, collects tokens, writes config, and guides users
through Slack App creation.

Design: pure validation functions at the core, side-effectful I/O at the edges.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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
    # xoxb- tokens have the structure: xoxb-{team_id}-{bot_id}-{secret}
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

    # Append missing tokens with a section header if neither existed
    additions = []
    if not bot_found:
        additions.append(f"LOBSTER_SLACK_BOT_TOKEN={bot_token}")
    if not app_found:
        additions.append(f"LOBSTER_SLACK_APP_TOKEN={app_token}")

    if additions:
        # Add a blank line separator if the file doesn't end with one
        if result_lines and result_lines[-1].strip():
            result_lines.append("")
        # Add section comment if both are new
        if not bot_found and not app_found:
            result_lines.append("# Slack Integration")
        result_lines.extend(additions)

    # Ensure trailing newline
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
        # Injectable I/O for testability
        self._input = input_fn or input
        self._print = print_fn or print

    def check_prerequisites(self) -> list[str]:
        """Returns list of failed prerequisite descriptions, empty if all pass."""
        results = check_all_prerequisites(self.config_path)
        failures = failed_prerequisites(results)
        return [f.message for f in failures]

    def validate_bot_token(self, token: str) -> tuple[bool, str]:
        """Validate bot token format and call auth.test.

        Returns (valid, workspace_name_or_error).
        """
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
        """Prompt user for a token with validation retry loop.

        Returns (token, validation_info) or (None, error_message).
        """
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
        """Full interactive setup flow. Returns True on success.

        Orchestrates all steps: prerequisites, token check, guided setup,
        collection, validation, and config writing.
        """
        self._print("\n=== Slack Connector — Bot Account Setup ===\n")

        # Step 1: Prerequisites
        self._print("Step 1: Checking prerequisites...")
        failures = self.check_prerequisites()
        if failures:
            self._print("\nPrerequisite checks failed:")
            for f in failures:
                self._print(f"  ✗ {f}")
            return False
        self._print("  ✓ All prerequisites met\n")

        # Step 2: Check existing tokens
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

        # Step 3: Show Slack App creation guide
        self._print("Step 3: Slack App Setup")
        self._print(SLACK_APP_CREATION_GUIDE)
        self._input("Press Enter when you've completed the steps above...")

        # Step 4: Collect and validate tokens
        self._print("\nStep 4: Token Collection\n")

        # Collect bot token (skip if already set)
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

        # Collect app token (skip if already set)
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

        # Write tokens
        self._print("  Writing tokens to config.env...")
        self.write_tokens_to_config(bot_token, app_token)
        self._print("  ✓ Tokens saved\n")

        # Success
        self._print(
            format_success_message(
                workspace=workspace,
                bot_token=bot_token,
                app_token=app_token,
                config_path=str(self.config_path),
            )
        )
        return True
