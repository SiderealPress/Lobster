"""
Central timezone utility for Lobster.

Rules:
  - All internal storage uses UTC (timezone-aware datetimes).
  - All display/output to users adapts to the owner's timezone, read from
    owner.toml (field: owner.timezone).
  - Falls back to UTC if no timezone is configured.

Public API
----------
  utcnow()                          -> datetime (UTC-aware)
  to_utc(dt)                        -> datetime (UTC-aware)
  to_user_tz(dt, user_tz=None)      -> datetime (user-tz-aware)
  format_for_user(dt, fmt=..., user_tz=None) -> str
  format_iso_for_user(iso_str, fmt=..., user_tz=None) -> str
  get_owner_tz_name()               -> str  (IANA name, e.g. 'America/Los_Angeles')
  get_owner_zoneinfo()              -> ZoneInfo

Per-user timezone overrides
---------------------------
  Pass ``user_tz`` (IANA string or ZoneInfo) to any ``to_user_tz`` /
  ``format_for_user`` call.  The owner.toml timezone is the default; per-user
  overrides are stored in the user model (observation key "timezone") and can be
  passed through by callers who have already resolved the preference.

  Example::

      from utils.timezone import format_for_user
      display = format_for_user(dt, user_tz="Europe/London")

Stdlib-only — no third-party dependencies required.
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timezone as _tz_utc
from typing import Union

# Type alias accepted by public helpers
_TZish = Union[str, zoneinfo.ZoneInfo, None]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_zoneinfo(name: str) -> zoneinfo.ZoneInfo:
    """Load a ZoneInfo by IANA name, falling back to UTC on any error."""
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


def _resolve_tz(user_tz: _TZish) -> zoneinfo.ZoneInfo:
    """
    Resolve *user_tz* to a ZoneInfo object.

    Priority:
      1. Explicit *user_tz* argument (str or ZoneInfo).
      2. Owner timezone from owner.toml.
      3. UTC fallback.
    """
    if user_tz is not None:
        if isinstance(user_tz, zoneinfo.ZoneInfo):
            return user_tz
        if isinstance(user_tz, str) and user_tz:
            return _load_zoneinfo(user_tz)
    return get_owner_zoneinfo()


# ---------------------------------------------------------------------------
# Owner timezone resolution
# ---------------------------------------------------------------------------

def get_owner_tz_name() -> str:
    """
    Return the owner's IANA timezone string from owner.toml.

    Falls back to 'UTC' if not configured or on any error.
    """
    try:
        from user_model.owner import get_owner_timezone as _get
        name = _get()
        return name if name else "UTC"
    except Exception:
        pass
    # Secondary fallback: try importing via package path
    try:
        from mcp.user_model.owner import get_owner_timezone as _get2
        name = _get2()
        return name if name else "UTC"
    except Exception:
        return "UTC"


def get_owner_zoneinfo() -> zoneinfo.ZoneInfo:
    """Return a ZoneInfo for the owner's configured timezone."""
    return _load_zoneinfo(get_owner_tz_name())


# ---------------------------------------------------------------------------
# UTC helpers (for internal storage)
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    """
    Return the current time as a timezone-aware UTC datetime.

    Drop-in replacement for ``datetime.utcnow()`` that is actually tz-aware.
    """
    return datetime.now(_tz_utc.utc)


def to_utc(dt: datetime) -> datetime:
    """
    Ensure *dt* is a UTC-aware datetime.

    - If *dt* is already tz-aware, convert it to UTC.
    - If *dt* is naive, assume UTC and attach the UTC tzinfo.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_tz_utc.utc)
    return dt.astimezone(_tz_utc.utc)


# ---------------------------------------------------------------------------
# Display helpers (for user-facing output)
# ---------------------------------------------------------------------------

_DEFAULT_FMT = "%Y-%m-%d %I:%M %p %Z"


def to_user_tz(dt: datetime, user_tz: _TZish = None) -> datetime:
    """
    Convert *dt* to the user's local timezone.

    Naive datetimes are assumed to be UTC.
    """
    return to_utc(dt).astimezone(_resolve_tz(user_tz))


def format_for_user(
    dt: datetime,
    fmt: str = _DEFAULT_FMT,
    user_tz: _TZish = None,
) -> str:
    """
    Convert *dt* to the user's timezone and return a formatted string.

    Args:
        dt:      Datetime to format (naive assumed UTC).
        fmt:     strftime format string.  Default: '%Y-%m-%d %I:%M %p %Z'
                 which produces e.g. '2026-04-10 09:30 AM PDT'.
        user_tz: Override timezone (IANA string or ZoneInfo).  Defaults to
                 the owner's configured timezone from owner.toml.

    Returns:
        Formatted string in the user's local time.
    """
    local_dt = to_user_tz(dt, user_tz)
    return local_dt.strftime(fmt)


def format_iso_for_user(
    iso_str: str,
    fmt: str = _DEFAULT_FMT,
    user_tz: _TZish = None,
) -> str:
    """
    Parse an ISO 8601 string and format it in the user's timezone.

    Handles trailing 'Z' for UTC.  Falls back to the raw string on parse error.
    """
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return format_for_user(dt, fmt=fmt, user_tz=user_tz)
    except Exception:
        return iso_str


def format_with_utc_and_local(
    ts_str: str,
    user_tz: _TZish = None,
) -> str:
    """
    Format a timestamp as 'YYYY-MM-DDTHH:MM:SS UTC (H:MM AM/PM TZ)'.

    This is the canonical display format for inbox timestamps: keeps the UTC
    value for auditability and appends the owner's local time in parentheses.

    Example output: '2026-04-10T14:30:00 UTC (7:30 AM PDT)'
    """
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        utc_dt = to_utc(dt)
        utc_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%S UTC")
        local_dt = to_user_tz(utc_dt, user_tz)
        local_str = local_dt.strftime("%-I:%M %p %Z")
        return f"{utc_str} ({local_str})"
    except Exception:
        return ts_str
