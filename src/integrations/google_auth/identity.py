"""
Token identity metadata — capturing and validating *who* actually granted
an OAuth token, not just trusting the chat_id it was pushed for.

Why this exists (issue #2153)
------------------------------
The Calendar/Gmail/Workspace push-token receivers in ``inbox_server_http.py``
validate a signed, single-use session token proving "this push corresponds
to the OAuth transaction requested by chat_id=X". That proves the
*transaction* is legitimate. It does NOT prove that the Google account
which completed the consent screen belongs to the person who owns chat_id=X
— a consent link that gets shared, forwarded, or clicked while a different
Google account is active in the browser produces a token that is bound to
the right chat_id but authenticated by the wrong human. Without any
identity claim captured from Google itself, that mixup is invisible until
something explicitly reads the account's data.

This module captures the authenticated email at grant time (via Google's
``userinfo`` endpoint, called with the just-issued access_token — no client
secret required) and provides pure comparison functions so callers can
detect a mismatch immediately instead of silently.

Two independent checks are supported, since neither alone covers every case:

- ``check_identity_consistency`` — self-consistency: does the newly granted
  email match the email captured for this chat_id last time? Works out of
  the box with no external data, but has nothing to compare against on a
  chat_id's very first grant.
- ``check_expected_identity`` — checks the newly granted email against an
  optional ``known-users.json`` registry (chat_id -> expected email), which
  *does* cover the first-grant case, provided the registry has been
  populated for that chat_id.

Design principles
------------------
- Side effects (the HTTP call to Google, reading the registry file) are
  isolated to dedicated functions; the comparison logic is pure.
- Never raises on failure to fetch or parse — callers get ``None``/a
  descriptive status, never an exception, so a userinfo hiccup can never
  block a token from being saved and used.
- No token or email values are ever logged at a level that would leak
  outside this instance's own log files; log messages here only echo
  chat_id and the (non-secret) email address for operator visibility.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google userinfo endpoint
# ---------------------------------------------------------------------------

#: Google's OpenID-Connect-compatible userinfo endpoint. Requires the access
#: token to have been granted with at least one of the `email`, `profile`,
#: or `openid` scopes -- if the consent grant didn't request one of those,
#: this call fails with 403 and fetch_authenticated_email returns None.
GOOGLE_USERINFO_URL: str = "https://www.googleapis.com/oauth2/v3/userinfo"

_HTTP_TIMEOUT: int = 10


def fetch_authenticated_email(
    access_token: str,
    *,
    userinfo_url: str = GOOGLE_USERINFO_URL,
    timeout: int = _HTTP_TIMEOUT,
) -> Optional[str]:
    """Return the email address of the Google account that granted this token.

    Calls Google's userinfo endpoint directly with the access_token as
    bearer auth -- this requires no client secret, so it works from an
    instance (like this one) that never holds GCP credentials locally.

    Args:
        access_token: The just-issued (or just-refreshed) access token.
        userinfo_url: Injectable for testing.
        timeout:      HTTP timeout in seconds.

    Returns:
        The account's email address, or None if the call fails for any
        reason (network error, non-2xx response, missing/empty email in the
        payload, insufficient scope). Never raises.
    """
    if not access_token:
        return None

    try:
        resp = requests.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("userinfo fetch failed (network error): %s", exc)
        return None

    if not resp.ok:
        log.warning(
            "userinfo endpoint returned %d -- token may lack email/profile "
            "scope, or the access_token is invalid/expired.",
            resp.status_code,
        )
        return None

    try:
        data = resp.json()
        email = data.get("email")
    except (ValueError, AttributeError) as exc:
        log.warning("userinfo endpoint returned unparseable payload: %s", exc)
        return None

    if not email or not isinstance(email, str):
        log.warning("userinfo endpoint response had no usable email field.")
        return None

    return email


# ---------------------------------------------------------------------------
# Identity check result (pure comparison logic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityCheckResult:
    """Outcome of comparing a newly granted email against a baseline.

    Attributes:
        status:   One of:
                  - "match"             baseline and new email agree
                  - "mismatch"          baseline and new email disagree
                  - "no_baseline"       nothing to compare against (first
                                        grant for this chat_id / registry has
                                        no entry) -- not an error, just
                                        unverifiable
                  - "email_unavailable" the new grant's email could not be
                                        captured at all (userinfo call
                                        failed) -- can't compare
        baseline_email: The email being compared against (previously stored,
                        or expected from the registry). None if unavailable.
        new_email:      The newly granted email. None if unavailable.
    """

    status: str
    baseline_email: Optional[str]
    new_email: Optional[str]

    @property
    def is_mismatch(self) -> bool:
        return self.status == "mismatch"


def check_identity_consistency(
    previous_email: Optional[str],
    new_email: Optional[str],
) -> IdentityCheckResult:
    """Compare a newly granted email against the previously stored one.

    Pure function -- no I/O. Self-consistency check: catches a re-auth
    (reconnect) landing on a different Google account than the one already
    on file for this chat_id. Cannot catch a mixup on the very first grant
    (nothing to compare against) -- see ``check_expected_identity`` for that.
    """
    if new_email is None:
        return IdentityCheckResult("email_unavailable", previous_email, None)
    if previous_email is None:
        return IdentityCheckResult("no_baseline", None, new_email)
    if previous_email == new_email:
        return IdentityCheckResult("match", previous_email, new_email)
    return IdentityCheckResult("mismatch", previous_email, new_email)


def check_expected_identity(
    expected_email: Optional[str],
    new_email: Optional[str],
) -> IdentityCheckResult:
    """Compare a newly granted email against a caller-supplied expected value.

    Pure function -- no I/O (see ``expected_email_for_chat_id`` for the
    registry lookup). Semantically identical comparison to
    ``check_identity_consistency`` but kept as a separate entry point since
    the two baselines (prior grant vs. registry) are conceptually distinct
    and callers may want to run both.
    """
    return check_identity_consistency(expected_email, new_email)


def format_mismatch_warning(result: IdentityCheckResult, *, chat_id: str) -> Optional[str]:
    """Return a short, user-facing warning string for a mismatch, or None.

    Pure function. Only ever returns non-None for status == "mismatch" --
    "no_baseline" and "email_unavailable" are not surfaced to the user as
    warnings (they're not evidence of anything wrong, just "couldn't verify").
    """
    if result.status != "mismatch":
        return None
    return (
        "\n\nHeads up: this connection is authenticated as "
        f"{result.new_email}, but the account previously on file for this "
        f"chat was {result.baseline_email}. If that's not expected, "
        "someone may have connected the wrong Google account -- consider "
        "disconnecting and reconnecting with the correct one."
    )


# ---------------------------------------------------------------------------
# known-users.json registry (optional, for the first-grant case)
# ---------------------------------------------------------------------------

_HOME: Path = Path.home()
_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages")))

#: Optional operator-maintained registry mapping chat_id -> expected email,
#: e.g. {"1111111111": "account-a@example.com", "2222222222": "account-b@example.com"}.
#: Entirely optional: absent or empty file means "no expectation on file",
#: which check_expected_identity reports as "no_baseline", never an error.
KNOWN_USERS_PATH: Path = _MESSAGES_DIR / "config" / "known-users.json"


def expected_email_for_chat_id(
    chat_id: str,
    known_users_path: Path = KNOWN_USERS_PATH,
) -> Optional[str]:
    """Look up the expected email for a chat_id in the known-users registry.

    Degrades gracefully to None if the file is absent, unreadable, not valid
    JSON, or has no entry for this chat_id -- this registry is optional
    metadata, never a hard dependency.
    """
    if not known_users_path.exists():
        return None
    try:
        data = json.loads(known_users_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to parse known-users.json: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    email = data.get(chat_id)
    if not email or not isinstance(email, str):
        return None
    return email
