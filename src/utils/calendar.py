"""
Google Calendar deep-link utilities.

Generates "Add to Calendar" URLs that open Google Calendar pre-filled with
event details. No OAuth or API key required — these are plain deep links.

URL format:
    https://calendar.google.com/calendar/r/eventedit
        ?text=TITLE
        &dates=YYYYMMDDTHHMMSSZ/YYYYMMDDTHHMMSSZ
        &details=DESCRIPTION
        &location=LOCATION

All parameter values are URL-encoded. Dates must be in UTC, formatted as
compact ISO 8601 without dashes or colons (e.g. 20260307T190000Z).

Example usage:
    >>> from datetime import datetime, timezone
    >>> start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
    >>> end = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
    >>> gcal_add_link("Doctor appointment", start, end)
    'https://calendar.google.com/calendar/r/eventedit?text=Doctor+appointment&dates=20260307T150000Z%2F20260307T160000Z'

    >>> gcal_add_link_md("Doctor appointment", start, end)
    '[Add to Google Calendar](https://calendar.google.com/calendar/r/eventedit?text=Doctor+appointment&dates=20260307T150000Z%2F20260307T160000Z)'
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode


_GCAL_BASE_URL = "https://calendar.google.com/calendar/r/eventedit"


def _format_gcal_datetime(dt: datetime) -> str:
    """Format a datetime as a compact UTC string for Google Calendar.

    Converts to UTC if timezone-aware, assumes UTC if naive.
    Output format: YYYYMMDDTHHMMSSZ

    Args:
        dt: The datetime to format.

    Returns:
        Compact UTC datetime string, e.g. '20260307T150000Z'.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def gcal_add_link(
    title: str,
    start: datetime,
    end: datetime | None = None,
    description: str = "",
    location: str = "",
) -> str:
    """Generate a Google Calendar 'Add to Calendar' deep link.

    Args:
        title: Event title / summary.
        start: Event start time. Timezone-aware datetimes are converted to UTC;
               naive datetimes are treated as UTC.
        end: Event end time. Defaults to start + 1 hour if not provided.
        description: Optional event description / notes.
        location: Optional event location string.

    Returns:
        Full Google Calendar event-creation URL with all parameters URL-encoded.

    Example:
        >>> from datetime import datetime, timezone
        >>> start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        >>> end = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        >>> gcal_add_link("Doctor appointment", start, end, location="123 Main St")
        'https://calendar.google.com/calendar/r/eventedit?text=Doctor+appointment&dates=20260307T150000Z%2F20260307T160000Z&location=123+Main+St'
    """
    if end is None:
        end = start + timedelta(hours=1)

    dates = f"{_format_gcal_datetime(start)}/{_format_gcal_datetime(end)}"

    params: dict[str, str] = {
        "text": title,
        "dates": dates,
    }

    # Only include optional params when they have content — keeps URLs clean
    if description:
        params["details"] = description
    if location:
        params["location"] = location

    return f"{_GCAL_BASE_URL}?{urlencode(params)}"


def gcal_add_link_md(
    title: str,
    start: datetime,
    end: datetime | None = None,
    description: str = "",
    location: str = "",
) -> str:
    """Return a Telegram-compatible markdown 'Add to Google Calendar' link.

    Wraps gcal_add_link() in Telegram markdown syntax: [label](url).

    Args:
        title: Event title / summary.
        start: Event start time.
        end: Event end time. Defaults to start + 1 hour if not provided.
        description: Optional event description / notes.
        location: Optional event location string.

    Returns:
        Markdown string: '[Add to Google Calendar](url)'

    Example:
        >>> from datetime import datetime, timezone
        >>> start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        >>> gcal_add_link_md("Doctor appointment", start)
        '[Add to Google Calendar](https://calendar.google.com/calendar/r/eventedit?text=Doctor+appointment&dates=20260307T150000Z%2F20260307T160000Z)'
    """
    url = gcal_add_link(title, start, end=end, description=description, location=location)
    return f"[Add to Google Calendar]({url})"
