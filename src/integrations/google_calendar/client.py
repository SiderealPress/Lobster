"""
Google Calendar API client — Phase 3.

Provides a clean, typed Python layer over the Google Calendar REST API so
Lobster can list upcoming events and create new events on a user's behalf.

All HTTP calls go through ``_call_calendar_api``, which is the single point of
contact with the network.  Auth failures and HTTP errors are caught and
converted to domain exceptions; callers that want graceful degradation can use
the high-level helpers (``get_upcoming_events``, ``create_event``) which return
empty lists / None on auth failure rather than propagating exceptions.

Design principles (consistent with Phase 1 & 2):
- Immutable value objects (frozen dataclasses)
- Pure helpers isolated from I/O
- Side effects (network calls, token refresh) kept at the boundaries
- No credentials or token values ever appear in logs or exception messages
- Timezone-aware datetimes throughout (UTC)

Google Calendar REST API docs:
    https://developers.google.com/calendar/api/v3/reference/events

Environment variables:
    GOOGLE_CLIENT_ID      — loaded via config.py
    GOOGLE_CLIENT_SECRET  — loaded via config.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from integrations.google_calendar.config import GoogleOAuthCredentials
from integrations.google_calendar.token_store import get_valid_token

# Re-export so callers can do:
#   from integrations.google_calendar.client import gcal_add_link
from utils.calendar import gcal_add_link  # noqa: F401 — intentional re-export

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Calendar REST API constants
# ---------------------------------------------------------------------------

_CALENDAR_API_BASE: str = "https://www.googleapis.com/calendar/v3"
_CALENDAR_ID_PRIMARY: str = "primary"

# Timeout for HTTP requests to the Calendar API (seconds).
_HTTP_TIMEOUT: int = 15


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarEvent:
    """Immutable representation of a single Google Calendar event.

    Attributes:
        id:          Google-assigned event identifier.
        title:       Event summary / title.
        start:       Event start time (timezone-aware UTC datetime).
        end:         Event end time (timezone-aware UTC datetime).
        description: Optional event description / notes.
        location:    Optional event location string.
        url:         ``htmlLink`` from the Google Calendar API — a browser URL
                     that opens the event in Google Calendar.  None when the
                     event was constructed locally before an API round-trip.
    """

    id: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    url: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CalendarAPIError(RuntimeError):
    """Raised when the Google Calendar API returns a non-2xx response.

    The message includes the HTTP status code and a short description but
    never the raw response body (which might contain user data) and never
    any credential or token values.
    """

    def __init__(self, status_code: int, summary: str = "") -> None:
        self.status_code = status_code
        super().__init__(
            f"Google Calendar API error {status_code}"
            + (f": {summary}" if summary else "")
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_datetime(raw: str) -> datetime:
    """Parse a Google Calendar datetime string into a timezone-aware UTC datetime.

    Google returns datetimes in RFC 3339 format (e.g. ``2026-03-07T15:00:00Z``
    or ``2026-03-07T15:00:00+05:00``).  All-day events use a date-only string
    (``2026-03-07``) — these are treated as midnight UTC.

    Args:
        raw: ISO datetime or date string from the Google Calendar API.

    Returns:
        A timezone-aware datetime in UTC.
    """
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Fallback: strip trailing 'Z' (Python < 3.11 fromisoformat limitation)
        dt = datetime.fromisoformat(raw.rstrip("Z").replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt


def _parse_event(raw: dict) -> CalendarEvent:
    """Convert a raw Google Calendar event dict into a CalendarEvent.

    Handles both datetime events (``dateTime`` key) and all-day events
    (``date`` key) gracefully.  Missing optional fields default to empty
    strings per the CalendarEvent definition.

    Args:
        raw: A single event object from the Google Calendar API response.

    Returns:
        A frozen CalendarEvent instance.
    """
    start_obj: dict = raw.get("start", {})
    end_obj: dict = raw.get("end", {})

    # dateTime is present for timed events; date for all-day events
    start_raw: str = start_obj.get("dateTime") or start_obj.get("date", "")
    end_raw: str = end_obj.get("dateTime") or end_obj.get("date", "")

    start_dt = _parse_datetime(start_raw) if start_raw else datetime.now(tz=timezone.utc)
    end_dt = _parse_datetime(end_raw) if end_raw else start_dt + timedelta(hours=1)

    return CalendarEvent(
        id=raw.get("id", ""),
        title=raw.get("summary", ""),
        start=start_dt,
        end=end_dt,
        description=raw.get("description", ""),
        location=raw.get("location", ""),
        url=raw.get("htmlLink"),
    )


def _build_event_body(
    title: str,
    start: datetime,
    end: datetime,
    description: str,
    location: str,
) -> dict:
    """Construct the request body dict for a Calendar API event create/update.

    This is a pure function — no I/O, no side effects.

    Args:
        title:       Event summary / title.
        start:       Event start time (timezone-aware UTC).
        end:         Event end time (timezone-aware UTC).
        description: Optional event description.
        location:    Optional event location.

    Returns:
        Dict suitable for JSON-encoding in a Calendar API request body.
    """
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start.astimezone(timezone.utc).isoformat()},
        "end": {"dateTime": end.astimezone(timezone.utc).isoformat()},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    return body


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


def _call_calendar_api(
    method: str,
    url: str,
    token: str,
    **kwargs: Any,
) -> Any:
    """Make an authenticated HTTP call to the Google Calendar API.

    This is the single point of network contact for all Calendar API calls.
    All other helpers in this module call through here.

    Args:
        method: HTTP method string, e.g. ``"GET"``, ``"POST"``.
        url:    Full API endpoint URL.
        token:  Valid OAuth access token (used in Authorization header).
        **kwargs: Additional keyword arguments forwarded to ``requests.request``
                  (e.g. ``params``, ``json``).

    Returns:
        Parsed JSON response body (dict or list).

    Raises:
        CalendarAPIError: If the response status code is not 2xx.
        requests.exceptions.RequestException: On network-level failures
            (timeouts, connection errors).  Callers that want graceful
            degradation should catch this alongside CalendarAPIError.
    """
    headers = {**_auth_header(token), "Accept": "application/json"}
    kwargs.setdefault("timeout", _HTTP_TIMEOUT)

    log.debug("Calendar API %s %s", method, url)

    response = requests.request(method, url, headers=headers, **kwargs)

    if not response.ok:
        # Extract a brief summary from the error body if available, but
        # never log the full body in case it contains sensitive data.
        try:
            err_body: dict = response.json()
            summary = err_body.get("error", {}).get("message", "")
        except Exception:
            summary = ""
        log.warning(
            "Calendar API returned %d for %s %s",
            response.status_code, method, url,
        )
        raise CalendarAPIError(status_code=response.status_code, summary=summary)

    return response.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_upcoming_events(
    user_id: str,
    days: int = 7,
    credentials: Optional[GoogleOAuthCredentials] = None,
) -> list[CalendarEvent]:
    """Fetch upcoming calendar events for a user.

    Queries the user's primary calendar for events from now through
    ``now + days``.  Returns an empty list if the user has no valid token
    (not authenticated) or if any API/network error occurs.

    Args:
        user_id:     Lobster user identifier (e.g. Telegram chat_id as str).
        days:        Number of days ahead to fetch.  Defaults to 7.
        credentials: Optional pre-loaded Google OAuth credentials.  If None,
                     credentials are loaded from environment variables when a
                     token refresh is needed.

    Returns:
        List of CalendarEvent objects ordered by start time (ascending), as
        returned by the Google Calendar API.  Empty list on any failure.
    """
    token = get_valid_token(user_id, credentials=credentials)
    if token is None:
        log.info(
            "get_upcoming_events: no valid token for user_id=%r — returning []",
            user_id,
        )
        return []

    now = datetime.now(tz=timezone.utc)
    time_max = now + timedelta(days=days)

    url = f"{_CALENDAR_API_BASE}/calendars/{_CALENDAR_ID_PRIMARY}/events"
    params = {
        "timeMin": now.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
    }

    try:
        data = _call_calendar_api("GET", url, token.access_token, params=params)
    except (CalendarAPIError, requests.exceptions.RequestException) as exc:
        log.warning(
            "get_upcoming_events: API call failed for user_id=%r: %s",
            user_id, type(exc).__name__,
        )
        return []

    items: list[dict] = data.get("items", [])
    events = [_parse_event(item) for item in items]

    log.info(
        "get_upcoming_events: fetched %d events for user_id=%r (next %d days)",
        len(events), user_id, days,
    )
    return events


def create_event(
    user_id: str,
    title: str,
    start: datetime,
    end: Optional[datetime] = None,
    description: str = "",
    location: str = "",
    credentials: Optional[GoogleOAuthCredentials] = None,
) -> Optional[CalendarEvent]:
    """Create a new event on a user's primary Google Calendar.

    If ``end`` is not provided it defaults to ``start + 1 hour``.  Returns
    the created CalendarEvent (with the Google-assigned ``id`` and ``url``),
    or None if the user has no valid token or any API/network error occurs.

    Args:
        user_id:     Lobster user identifier.
        title:       Event title / summary.
        start:       Event start time.  Must be timezone-aware.
        end:         Event end time.  Defaults to start + 1 hour if None.
        description: Optional event description.
        location:    Optional event location.
        credentials: Optional pre-loaded credentials.  Falls back to env vars
                     when a token refresh is needed.

    Returns:
        The created CalendarEvent, or None on auth failure or API error.
    """
    token = get_valid_token(user_id, credentials=credentials)
    if token is None:
        log.info(
            "create_event: no valid token for user_id=%r — returning None",
            user_id,
        )
        return None

    effective_end: datetime = end if end is not None else start + timedelta(hours=1)

    url = f"{_CALENDAR_API_BASE}/calendars/{_CALENDAR_ID_PRIMARY}/events"
    body = _build_event_body(title, start, effective_end, description, location)

    try:
        created_raw = _call_calendar_api("POST", url, token.access_token, json=body)
    except (CalendarAPIError, requests.exceptions.RequestException) as exc:
        log.warning(
            "create_event: API call failed for user_id=%r: %s",
            user_id, type(exc).__name__,
        )
        return None

    event = _parse_event(created_raw)
    log.info(
        "create_event: created event id=%r for user_id=%r",
        event.id, user_id,
    )
    return event
