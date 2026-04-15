"""
Slack Connector — Account Mode Detection and Validation.

Determines whether Lobster operates as a bot (xoxb- token) or a person
(xoxp- user token) and validates tokens accordingly.

Design principles:
- Pure functions for token classification and scope computation
- Side effects isolated to validate_person_token() (HTTP call to Slack API)
- Exhaustive pattern matching via prefix-based detection
- All public functions have clear input/output contracts
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("slack-account-mode")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOT = "bot"
PERSON = "person"

_TOKEN_PREFIX_TO_MODE: dict[str, str] = {
    "xoxb-": BOT,
    "xoxp-": PERSON,
}

_BOT_SCOPES: list[str] = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "chat:write",
    "users:read",
    "reactions:read",
    "files:read",
]

_PERSON_SCOPES: list[str] = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "channels:write",
    "users:read",
    "reactions:read",
    "files:read",
]

_REQUIRED_SCOPES: dict[str, list[str]] = {
    BOT: _BOT_SCOPES,
    PERSON: _PERSON_SCOPES,
}


# ---------------------------------------------------------------------------
# Pure functions — token classification and scope lookup
# ---------------------------------------------------------------------------


def detect_from_token(token: str) -> str:
    """Classify a Slack token as 'bot' or 'person' based on its prefix.

    Pure function: string in, string out.

    Returns:
        'bot' for xoxb- tokens, 'person' for xoxp- tokens.

    Raises:
        ValueError: If the token prefix is unrecognized.
    """
    for prefix, mode in _TOKEN_PREFIX_TO_MODE.items():
        if token.startswith(prefix):
            return mode
    raise ValueError(
        f"Unrecognized token prefix. Expected xoxb- (bot) or xoxp- (person), "
        f"got: {token[:5]}..."
    )


def get_required_scopes(mode: str) -> list[str]:
    """Return the required OAuth scopes for a given account mode.

    Pure function: mode string in, scope list out.

    Args:
        mode: Either 'bot' or 'person'.

    Returns:
        Sorted list of required scope strings.

    Raises:
        ValueError: If mode is not 'bot' or 'person'.
    """
    if mode not in _REQUIRED_SCOPES:
        raise ValueError(f"Invalid mode: {mode!r}. Must be 'bot' or 'person'.")
    return sorted(_REQUIRED_SCOPES[mode])


def validate_token_mode_match(token: str, expected_mode: str) -> tuple[bool, str]:
    """Check that a token's prefix matches the expected account mode.

    Pure function: returns (is_match, error_message).
    """
    try:
        detected = detect_from_token(token)
    except ValueError as e:
        return False, str(e)

    if detected != expected_mode:
        mode_labels = {BOT: "bot token (xoxb-)", PERSON: "user token (xoxp-)"}
        return False, (
            f"Expected {mode_labels.get(expected_mode, expected_mode)}, "
            f"got {mode_labels.get(detected, detected)}"
        )
    return True, ""


def resolve_account_type(
    *,
    env_override: str | None = None,
    preference: str = BOT,
) -> str:
    """Resolve effective account type from env var and preference.

    Priority: env var (LOBSTER_SLACK_ACCOUNT_TYPE) > skill preference > default (bot).
    Pure function when env_override is passed explicitly.
    """
    effective = env_override or preference or BOT
    if effective not in (BOT, PERSON):
        log.warning("Invalid account_type %r, falling back to 'bot'", effective)
        return BOT
    return effective


def resolve_account_type_from_env(preference: str = BOT) -> str:
    """Convenience wrapper that reads LOBSTER_SLACK_ACCOUNT_TYPE from os.environ.

    Side effect: reads environment variable.
    """
    return resolve_account_type(
        env_override=os.environ.get("LOBSTER_SLACK_ACCOUNT_TYPE"),
        preference=preference,
    )


# ---------------------------------------------------------------------------
# Validation — side-effect boundary (HTTP call to Slack API)
# ---------------------------------------------------------------------------


def _call_auth_test(token: str) -> tuple[bool, dict[str, Any]]:
    """Call Slack's auth.test endpoint. Isolated side-effect function.

    Returns (ok, response_data_or_error).
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return False, {"error": "slack_sdk not installed. Run: uv pip install slack-sdk"}

    client = WebClient(token=token)

    try:
        resp = client.auth_test()
        if not resp.get("ok"):
            return False, {"error": resp.get("error", "auth.test failed")}
        return True, dict(resp)
    except SlackApiError as e:
        return False, {"error": f"Slack API error: {e.response.get('error', str(e))}"}
    except Exception as e:
        return False, {"error": f"Connection error: {e}"}


def validate_person_token(
    token: str,
    _auth_test_fn: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Validate a person (xoxp-) token via the Slack auth.test API.

    Side effect: makes an HTTP request to Slack (via _auth_test_fn).

    Args:
        token: The xoxp- user token.
        _auth_test_fn: Optional override for testing (dependency injection).

    Returns:
        (valid, info_dict) where info_dict contains:
        - user_id, name, team on success
        - error on failure
    """
    is_match, err = validate_token_mode_match(token, PERSON)
    if not is_match:
        return False, {"error": err}

    auth_test = _auth_test_fn or _call_auth_test
    ok, resp = auth_test(token)
    if not ok:
        return False, resp

    return True, {
        "user_id": resp.get("user_id", ""),
        "name": resp.get("user", ""),
        "team": resp.get("team", ""),
        "url": resp.get("url", ""),
    }


def validate_bot_token(
    token: str,
    _auth_test_fn: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Validate a bot (xoxb-) token via auth.test.

    Side effect: makes an HTTP request to Slack (via _auth_test_fn).

    Args:
        token: The xoxb- bot token.
        _auth_test_fn: Optional override for testing (dependency injection).
    """
    is_match, err = validate_token_mode_match(token, BOT)
    if not is_match:
        return False, {"error": err}

    auth_test = _auth_test_fn or _call_auth_test
    ok, resp = auth_test(token)
    if not ok:
        return False, resp

    return True, {
        "user_id": resp.get("user_id", ""),
        "bot_id": resp.get("bot_id", ""),
        "name": resp.get("user", ""),
        "team": resp.get("team", ""),
    }
