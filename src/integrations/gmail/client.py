"""
Gmail API client.

Provides a clean, typed Python layer over the Gmail REST API so Lobster can
list recent emails and search a user's inbox on their behalf.

All HTTP calls go through ``_call_gmail_api``, the single point of contact
with the network.  Auth failures and HTTP errors are caught and converted to
domain exceptions; callers that want graceful degradation can use the
high-level helpers (``get_recent_emails``, ``search_emails``) which return
empty lists on auth failure rather than propagating exceptions.

Design principles (consistent with the Google Calendar client):
- Immutable value objects (frozen dataclasses)
- Pure helpers isolated from I/O
- Side effects (network calls, token refresh) kept at the boundaries
- No credentials or token values ever appear in logs or exception messages
- Timezone-aware datetimes throughout (UTC)

Gmail REST API docs:
    https://developers.google.com/gmail/api/reference/rest/v1/users.messages
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from integrations.gmail.token_store import get_valid_token

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gmail REST API constants
# ---------------------------------------------------------------------------

_GMAIL_API_BASE: str = "https://gmail.googleapis.com/gmail/v1"
_USER_ID: str = "me"  # Gmail API uses "me" for the authenticated user

# Timeout for HTTP requests to the Gmail API (seconds).
_HTTP_TIMEOUT: int = 15

# Maximum number of message IDs to fetch in a single list call.
_DEFAULT_MAX_RESULTS: int = 10


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailMessage:
    """Immutable representation of a single Gmail message.

    Attributes:
        id:        Gmail-assigned message identifier.
        thread_id: Thread this message belongs to.
        subject:   Message subject line (empty string if absent).
        sender:    Sender's name/address from the ``From`` header.
        date:      Message date as a timezone-aware UTC datetime.
        snippet:   Short plain-text preview of the message body.
        labels:    Tuple of Gmail label IDs applied to this message
                   (e.g. ``("INBOX", "UNREAD")``).
    """

    id: str
    thread_id: str
    subject: str
    sender: str
    date: datetime
    snippet: str
    labels: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GmailAPIError(RuntimeError):
    """Raised when the Gmail API returns a non-2xx response.

    The message includes the HTTP status code and a short description but
    never the raw response body (which might contain user data) and never
    any credential or token values.
    """

    def __init__(self, status_code: int, summary: str = "") -> None:
        self.status_code = status_code
        super().__init__(
            f"Gmail API error {status_code}"
            + (f": {summary}" if summary else "")
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_header(headers: list[dict], name: str) -> str:
    """Extract the value of a named header from a Gmail message part headers list.

    Pure function — no I/O.

    Args:
        headers: List of ``{"name": ..., "value": ...}`` dicts from the Gmail API.
        name:    Case-insensitive header name to find.

    Returns:
        Header value string, or empty string if the header is absent.
    """
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _parse_date_header(raw: str) -> datetime:
    """Parse a Gmail ``Date`` header into a timezone-aware UTC datetime.

    Gmail returns RFC 2822 date strings such as
    ``"Wed, 1 Apr 2026 14:30:00 +0000"``.  Falls back to the current UTC
    time if parsing fails so callers always receive a valid datetime.

    Pure function — only processes the input string.

    Args:
        raw: Raw date string from the Gmail API message headers.

    Returns:
        A timezone-aware datetime in UTC.
    """
    from email.utils import parsedate_to_datetime

    if not raw:
        return datetime.now(tz=timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(tz=timezone.utc)


def _parse_message(raw: dict) -> EmailMessage:
    """Convert a raw Gmail message dict (full format) into an EmailMessage.

    Handles missing optional fields by returning sensible defaults.

    Pure function — no I/O.

    Args:
        raw: A single message object from the Gmail API (format=``"full"``).

    Returns:
        A frozen EmailMessage instance.
    """
    payload: dict = raw.get("payload", {})
    headers: list[dict] = payload.get("headers", [])

    subject = _parse_header(headers, "Subject")
    sender = _parse_header(headers, "From")
    date_str = _parse_header(headers, "Date")
    date = _parse_date_header(date_str)

    labels_raw: list[str] = raw.get("labelIds", [])

    return EmailMessage(
        id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        subject=subject,
        sender=sender,
        date=date,
        snippet=raw.get("snippet", ""),
        labels=tuple(labels_raw),
    )


def _auth_header(access_token: str) -> dict[str, str]:
    """Return an Authorization header dict for a bearer token.

    Pure function — constructs the header without any side effects.

    Args:
        access_token: A valid Google OAuth access token.

    Returns:
        Dict with a single ``Authorization`` key.
    """
    return {"Authorization": f"Bearer {access_token}"}


# ---------------------------------------------------------------------------
# HTTP helper (side-effecting boundary)
# ---------------------------------------------------------------------------


def _call_gmail_api(
    method: str,
    url: str,
    token: str,
    **kwargs: Any,
) -> Any:
    """Make an authenticated HTTP call to the Gmail API.

    This is the single point of network contact for all Gmail API calls.
    All other helpers in this module call through here.

    Args:
        method: HTTP method string, e.g. ``"GET"``.
        url:    Full API endpoint URL.
        token:  Valid OAuth access token (used in Authorization header).
        **kwargs: Additional keyword arguments forwarded to ``requests.request``
                  (e.g. ``params``, ``json``).

    Returns:
        Parsed JSON response body (dict or list).

    Raises:
        GmailAPIError: If the response status code is not 2xx.
        requests.exceptions.RequestException: On network-level failures.
    """
    headers = {**_auth_header(token), "Accept": "application/json"}
    kwargs.setdefault("timeout", _HTTP_TIMEOUT)

    log.debug("Gmail API %s %s", method, url)

    response = requests.request(method, url, headers=headers, **kwargs)

    if not response.ok:
        try:
            err_body: dict = response.json()
            summary = err_body.get("error", {}).get("message", "")
        except Exception:
            summary = ""
        log.warning(
            "Gmail API returned %d for %s %s",
            response.status_code, method, url,
        )
        raise GmailAPIError(status_code=response.status_code, summary=summary)

    return response.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_recent_emails(
    user_id: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    token_dir=None,
) -> list[EmailMessage]:
    """Fetch recent emails from the user's Gmail inbox.

    Queries the INBOX label, ordered by most recent first.  Returns an
    empty list if the user has no valid token or if any API/network error
    occurs.

    Args:
        user_id:     Lobster user identifier (e.g. Telegram chat_id as str).
        max_results: Maximum number of messages to return.  Defaults to 10.
        token_dir:   Optional Path for token directory (injectable for testing).

    Returns:
        List of EmailMessage objects ordered by most recent first.
        Empty list on any failure.
    """
    kwargs = {}
    if token_dir is not None:
        kwargs["token_dir"] = token_dir
    token = get_valid_token(user_id, **kwargs)
    if token is None:
        log.info(
            "get_recent_emails: no valid Gmail token for user_id=%r — returning []",
            user_id,
        )
        return []

    list_url = f"{_GMAIL_API_BASE}/users/{_USER_ID}/messages"
    params = {
        "labelIds": "INBOX",
        "maxResults": max_results,
    }

    try:
        list_data = _call_gmail_api("GET", list_url, token.access_token, params=params)
    except (GmailAPIError, requests.exceptions.RequestException) as exc:
        log.warning(
            "get_recent_emails: list call failed for user_id=%r: %s",
            user_id, type(exc).__name__,
        )
        return []

    message_refs: list[dict] = list_data.get("messages", [])
    if not message_refs:
        log.info("get_recent_emails: inbox empty for user_id=%r", user_id)
        return []

    messages: list[EmailMessage] = []
    for ref in message_refs:
        msg_id = ref.get("id", "")
        if not msg_id:
            continue
        msg_url = f"{_GMAIL_API_BASE}/users/{_USER_ID}/messages/{msg_id}"
        try:
            raw_msg = _call_gmail_api(
                "GET", msg_url, token.access_token,
                params={"format": "full"},
            )
            messages.append(_parse_message(raw_msg))
        except (GmailAPIError, requests.exceptions.RequestException) as exc:
            log.warning(
                "get_recent_emails: fetch failed for message_id=%r user_id=%r: %s",
                msg_id, user_id, type(exc).__name__,
            )

    log.info(
        "get_recent_emails: fetched %d messages for user_id=%r",
        len(messages), user_id,
    )
    return messages


def search_emails(
    user_id: str,
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    token_dir=None,
) -> list[EmailMessage]:
    """Search Gmail using the Gmail search query syntax.

    Supports all Gmail search operators (from:, subject:, is:unread, etc.).
    Returns an empty list if the user has no valid token or on any error.

    Args:
        user_id:     Lobster user identifier.
        query:       Gmail search query string (e.g. ``"from:boss@example.com"``).
        max_results: Maximum number of messages to return.  Defaults to 10.
        token_dir:   Optional Path for token directory (injectable for testing).

    Returns:
        List of EmailMessage objects matching the query.  Empty list on failure.
    """
    kwargs = {}
    if token_dir is not None:
        kwargs["token_dir"] = token_dir
    token = get_valid_token(user_id, **kwargs)
    if token is None:
        log.info(
            "search_emails: no valid Gmail token for user_id=%r — returning []",
            user_id,
        )
        return []

    list_url = f"{_GMAIL_API_BASE}/users/{_USER_ID}/messages"
    params = {
        "q": query,
        "maxResults": max_results,
    }

    try:
        list_data = _call_gmail_api("GET", list_url, token.access_token, params=params)
    except (GmailAPIError, requests.exceptions.RequestException) as exc:
        log.warning(
            "search_emails: list call failed for user_id=%r query=%r: %s",
            user_id, query, type(exc).__name__,
        )
        return []

    message_refs: list[dict] = list_data.get("messages", [])
    if not message_refs:
        log.info(
            "search_emails: no results for user_id=%r query=%r",
            user_id, query,
        )
        return []

    messages: list[EmailMessage] = []
    for ref in message_refs:
        msg_id = ref.get("id", "")
        if not msg_id:
            continue
        msg_url = f"{_GMAIL_API_BASE}/users/{_USER_ID}/messages/{msg_id}"
        try:
            raw_msg = _call_gmail_api(
                "GET", msg_url, token.access_token,
                params={"format": "full"},
            )
            messages.append(_parse_message(raw_msg))
        except (GmailAPIError, requests.exceptions.RequestException) as exc:
            log.warning(
                "search_emails: fetch failed for message_id=%r user_id=%r: %s",
                msg_id, user_id, type(exc).__name__,
            )

    log.info(
        "search_emails: fetched %d messages for user_id=%r query=%r",
        len(messages), user_id, query,
    )
    return messages
