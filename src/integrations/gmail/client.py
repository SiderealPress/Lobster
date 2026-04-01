"""
Gmail API client — read-only.

Provides a typed Python layer over the Gmail REST API so Lobster can read a
user's inbox on their behalf.  Auth is fully delegated to the existing
``token_store.get_valid_token`` function — no new OAuth infrastructure needed.

All public functions return empty lists or None on auth failure or API error
rather than propagating exceptions.  Callers can use ``has_gmail_scope`` to
distinguish "no token at all" from "token present but missing gmail.readonly"
and surface the appropriate re-consent prompt to the user.

Design principles (consistent with the Calendar integration):
- Immutable value objects (frozen dataclasses in ``models.py``).
- Pure helpers isolated from I/O.
- Side effects (network calls, token resolution) kept at the boundaries.
- No credential or token values appear in logs.
- Timezone-aware datetimes throughout (UTC).

Gmail REST API base URL:
    https://www.googleapis.com/gmail/v1/users/me
"""

from __future__ import annotations

import base64
import email
import logging
import re
from datetime import datetime, timezone
from email.header import decode_header
from typing import Any, Optional

import requests

from integrations.gmail.models import GmailMessage, GmailThread
from integrations.google_calendar.oauth import TokenData
from integrations.google_calendar.token_store import get_valid_token

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GMAIL_API_BASE: str = "https://www.googleapis.com/gmail/v1/users/me"
_HTTP_TIMEOUT: int = 15
_GMAIL_SCOPE_FRAGMENT: str = "gmail.readonly"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GmailAPIError(RuntimeError):
    """Raised when the Gmail API returns a non-2xx response.

    Never includes raw response body or credential values in the message.
    """

    def __init__(self, status_code: int, summary: str = "") -> None:
        self.status_code = status_code
        super().__init__(
            f"Gmail API error {status_code}" + (f": {summary}" if summary else "")
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def has_gmail_scope(token: TokenData) -> bool:
    """Return True if ``token`` includes the ``gmail.readonly`` scope.

    Checks the space-separated ``token.scope`` string for the Gmail scope
    fragment.  This is a pure function — no I/O.

    Args:
        token: A TokenData instance loaded from local disk.

    Returns:
        True if ``gmail.readonly`` appears in the granted scopes.
    """
    return _GMAIL_SCOPE_FRAGMENT in token.scope


def _auth_header(access_token: str) -> dict[str, str]:
    """Return an Authorization header dict for a bearer token.

    Pure function — no side effects.
    """
    return {"Authorization": f"Bearer {access_token}"}


def _decode_mime_header(raw: str) -> str:
    """Decode a MIME-encoded header value to a plain Unicode string.

    Handles RFC 2047 encoded-word syntax (``=?utf-8?b?...?=`` etc.) and
    falls back gracefully to the raw string on decode errors.

    Args:
        raw: Raw header string, possibly MIME-encoded.

    Returns:
        Decoded Unicode string.
    """
    parts = decode_header(raw)
    decoded_parts: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def _parse_date(raw: str) -> datetime:
    """Parse an RFC 2822 email date string into a timezone-aware UTC datetime.

    Falls back to epoch UTC on parse failure so callers always get a valid
    datetime rather than an exception.

    Args:
        raw: RFC 2822 date string from the Gmail API (e.g. ``internalDate``
             is milliseconds since epoch; email header ``Date`` is RFC 2822).

    Returns:
        Timezone-aware datetime in UTC.
    """
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)

    # Gmail internalDate field is milliseconds since epoch (numeric string)
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)

    # Try standard email date parsing
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _extract_header(headers: list[dict], name: str) -> str:
    """Extract the value of a named header from a Gmail message headers list.

    Gmail returns headers as ``[{"name": "Subject", "value": "..."}, ...]``.
    The search is case-insensitive.  Returns empty string if not found.

    Args:
        headers: List of header dicts from the Gmail API message payload.
        name:    Header name to find (case-insensitive).

    Returns:
        Header value string, or empty string if absent.
    """
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _extract_plain_body(payload: dict) -> str:
    """Recursively extract the plain-text body from a Gmail message payload.

    Handles multipart messages by walking the ``parts`` tree and returning
    the first ``text/plain`` part found.  Returns empty string if no plain
    body is available.

    Args:
        payload: The ``payload`` dict from a Gmail API message (``format=full``).

    Returns:
        Decoded plain-text body string, or empty string.
    """
    mime_type: str = payload.get("mimeType", "")
    body: dict = payload.get("body", {})
    parts: list[dict] = payload.get("parts", [])

    if mime_type == "text/plain":
        data = body.get("data", "")
        if data:
            try:
                return base64.urlsafe_b64decode(data + "==").decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                return ""

    # Recurse into multipart parts
    for part in parts:
        result = _extract_plain_body(part)
        if result:
            return result

    return ""


def _parse_message(raw: dict) -> GmailMessage:
    """Convert a raw Gmail API message dict into a GmailMessage.

    Works for all three ``format`` modes (``metadata``, ``full``, ``minimal``).
    Fields unavailable in the requested format default to empty strings.

    Args:
        raw: A single message object from the Gmail API response.

    Returns:
        A frozen GmailMessage instance.
    """
    message_id: str = raw.get("id", "")
    thread_id: str = raw.get("threadId", "")
    snippet: str = raw.get("snippet", "")
    label_ids: list[str] = raw.get("labelIds", [])
    is_unread: bool = "UNREAD" in label_ids

    payload: dict = raw.get("payload", {})
    headers: list[dict] = payload.get("headers", [])

    # Decode MIME-encoded header values for safe display
    subject = _decode_mime_header(_extract_header(headers, "Subject"))
    sender = _decode_mime_header(_extract_header(headers, "From"))
    to_raw = _extract_header(headers, "To")
    recipients: list[str] = (
        [addr.strip() for addr in to_raw.split(",") if addr.strip()]
        if to_raw
        else []
    )

    date_raw = _extract_header(headers, "Date")
    # Fall back to internalDate (ms since epoch) if Date header is absent
    if not date_raw:
        date_raw = str(raw.get("internalDate", "0"))
    date: datetime = _parse_date(date_raw)

    body_text: str = _extract_plain_body(payload)

    return GmailMessage(
        id=message_id,
        thread_id=thread_id,
        subject=subject,
        sender=sender,
        recipients=recipients,
        date=date,
        snippet=snippet,
        body_text=body_text,
        label_ids=label_ids,
        is_unread=is_unread,
    )


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

    Single point of network contact for all Gmail API calls.

    Args:
        method: HTTP method string (e.g. ``"GET"``).
        url:    Full API endpoint URL.
        token:  Valid OAuth access token.
        **kwargs: Additional keyword arguments forwarded to ``requests.request``.

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
        log.warning("Gmail API returned %d for %s %s", response.status_code, method, url)
        raise GmailAPIError(status_code=response.status_code, summary=summary)

    return response.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_recent_messages(
    user_id: str,
    max_results: int = 20,
    query: str = "",
    label_ids: list[str] | None = None,
) -> list[GmailMessage]:
    """List recent Gmail messages for a user.

    Fetches message stubs via ``messages.list``, then retrieves metadata for
    each via ``messages.get`` (format=metadata).  Returns an empty list on
    auth failure, missing Gmail scope, or any API/network error.

    Args:
        user_id:    Lobster user identifier (Telegram chat_id as str).
        max_results: Maximum number of messages to return.  Defaults to 20.
        query:      Gmail search query string (e.g. ``"is:unread"``).
                    Empty string means no filter beyond label_ids.
        label_ids:  Optional list of Gmail label IDs to filter by (e.g.
                    ``["INBOX"]``).  None means no label filter.

    Returns:
        List of GmailMessage objects.  Empty on any failure.
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("get_recent_messages: no valid token for user_id=%r", user_id)
        return []
    if not has_gmail_scope(token):
        log.info(
            "get_recent_messages: token for user_id=%r lacks gmail.readonly scope",
            user_id,
        )
        return []

    url = f"{_GMAIL_API_BASE}/messages"
    params: dict[str, Any] = {"maxResults": max_results, "format": "metadata"}
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = label_ids

    try:
        data = _call_gmail_api("GET", url, token.access_token, params=params)
    except (GmailAPIError, requests.exceptions.RequestException) as exc:
        log.warning(
            "get_recent_messages: list call failed for user_id=%r: %s",
            user_id, type(exc).__name__,
        )
        return []

    message_stubs: list[dict] = data.get("messages", [])
    if not message_stubs:
        return []

    messages: list[GmailMessage] = []
    for stub in message_stubs:
        msg_id: str = stub.get("id", "")
        if not msg_id:
            continue
        msg = get_message(user_id, msg_id, format="metadata")
        if msg is not None:
            messages.append(msg)

    log.info(
        "get_recent_messages: fetched %d messages for user_id=%r",
        len(messages), user_id,
    )
    return messages


def get_message(
    user_id: str,
    message_id: str,
    format: str = "metadata",
) -> GmailMessage | None:
    """Fetch a single Gmail message by ID.

    Args:
        user_id:    Lobster user identifier.
        message_id: Gmail message ID.
        format:     Gmail API format: ``"metadata"`` (headers + snippet),
                    ``"full"`` (headers + decoded body), or ``"minimal"``
                    (IDs + labels only).  Defaults to ``"metadata"``.

    Returns:
        GmailMessage on success, or None on auth failure or API error.
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("get_message: no valid token for user_id=%r", user_id)
        return None
    if not has_gmail_scope(token):
        log.info(
            "get_message: token for user_id=%r lacks gmail.readonly scope", user_id
        )
        return None

    url = f"{_GMAIL_API_BASE}/messages/{message_id}"
    params = {"format": format}

    try:
        raw = _call_gmail_api("GET", url, token.access_token, params=params)
    except (GmailAPIError, requests.exceptions.RequestException) as exc:
        log.warning(
            "get_message: API call failed for user_id=%r message_id=%r: %s",
            user_id, message_id, type(exc).__name__,
        )
        return None

    return _parse_message(raw)


def get_thread(
    user_id: str,
    thread_id: str,
) -> GmailThread | None:
    """Fetch a full Gmail thread (all messages in order).

    Args:
        user_id:   Lobster user identifier.
        thread_id: Gmail thread ID.

    Returns:
        GmailThread with messages ordered oldest-first, or None on failure.
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("get_thread: no valid token for user_id=%r", user_id)
        return None
    if not has_gmail_scope(token):
        log.info(
            "get_thread: token for user_id=%r lacks gmail.readonly scope", user_id
        )
        return None

    url = f"{_GMAIL_API_BASE}/threads/{thread_id}"
    params = {"format": "metadata"}

    try:
        raw = _call_gmail_api("GET", url, token.access_token, params=params)
    except (GmailAPIError, requests.exceptions.RequestException) as exc:
        log.warning(
            "get_thread: API call failed for user_id=%r thread_id=%r: %s",
            user_id, thread_id, type(exc).__name__,
        )
        return None

    raw_messages: list[dict] = raw.get("messages", [])
    messages: list[GmailMessage] = [_parse_message(m) for m in raw_messages]
    subject: str = messages[0].subject if messages else ""

    log.info(
        "get_thread: fetched thread id=%r (%d messages) for user_id=%r",
        thread_id, len(messages), user_id,
    )
    return GmailThread(id=thread_id, messages=messages, subject=subject)


def search_messages(
    user_id: str,
    query: str,
    max_results: int = 20,
) -> list[GmailMessage]:
    """Search Gmail messages using Gmail query syntax.

    Equivalent to ``get_recent_messages`` with a non-empty query.  Provided
    as a named entry point for clarity in callers.

    Args:
        user_id:     Lobster user identifier.
        query:       Gmail search query (e.g. ``"from:boss@corp.com invoice"``).
        max_results: Maximum number of results.  Defaults to 20.

    Returns:
        List of GmailMessage objects matching the query.  Empty on failure.
    """
    return get_recent_messages(user_id, max_results=max_results, query=query)
