"""
Google Sheets API client — read operations (Slice 5) and write operations (Slice 6).

Provides:
  - ``gsheets_read``  — fetch a cell range as a 2D list of strings (Slice 5)
  - ``gsheets_write`` — write values to a cell range (Slice 6)
  - ``gsheets_create`` — create a new spreadsheet (Slice 6)

Design principles (consistent with other workspace clients):
- Immutable value objects (frozen dataclasses)
- Pure helpers isolated from I/O
- Side effects (network calls, token refresh) at the boundaries
- No credentials or token values ever appear in logs
- Returns empty list / False / None on auth failure or API error

Google Sheets REST API v4 docs:
    https://developers.google.com/sheets/api/reference/rest
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from integrations.google_workspace.token_store import get_valid_token
from integrations.google_workspace.drive_client import DriveFile

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API constants
# ---------------------------------------------------------------------------

_SHEETS_API_BASE: str = "https://sheets.googleapis.com/v4/spreadsheets"
_DRIVE_API_BASE: str = "https://www.googleapis.com/drive/v3"
_HTTP_TIMEOUT: int = 15

_MIME_SHEET: str = "application/vnd.google-apps.spreadsheet"


# ---------------------------------------------------------------------------
# Helpers (pure functions)
# ---------------------------------------------------------------------------


def _sheet_id_from_url(sheet_id_or_url: str) -> str:
    """Extract the spreadsheet ID from a full Google Sheets URL, or return as-is.

    Accepts:
    - A raw spreadsheet ID
    - A full URL: ``https://docs.google.com/spreadsheets/d/<ID>/edit``

    Returns the spreadsheet ID string.
    """
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_id_or_url)
    if match:
        return match.group(1)
    return sheet_id_or_url.strip()


def _normalise_row(row: list) -> list[str]:
    """Convert all values in a row to strings."""
    return [str(v) if v is not None else "" for v in row]


# ---------------------------------------------------------------------------
# Internal I/O helpers
# ---------------------------------------------------------------------------


def _call_sheets_api(
    path: str,
    access_token: str,
    method: str = "GET",
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> Optional[dict]:
    """Make an authenticated call to the Google Sheets API.

    Args:
        path:         URL path after the Sheets API base.
        access_token: Bearer token for Google API auth.
        method:       ``"GET"``, ``"POST"``, or ``"PUT"``.
        params:       Optional query parameters.
        json_body:    Optional JSON body for write requests.

    Returns:
        Parsed JSON response dict, or None on any error.
    """
    url = f"{_SHEETS_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, params=params or {}, timeout=_HTTP_TIMEOUT)
        elif method == "PUT":
            resp = requests.put(
                url, headers=headers, params=params or {}, json=json_body, timeout=_HTTP_TIMEOUT
            )
        else:
            resp = requests.post(url, headers=headers, json=json_body, timeout=_HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        log.warning("Sheets API network error (%s %s): %s", method, path, exc)
        return None

    if resp.status_code == 401:
        log.warning("Sheets API: 401 Unauthorized — token may be expired (path=%s)", path)
        return None

    if not resp.ok:
        log.warning("Sheets API returned %d for %s %s", resp.status_code, method, path)
        return None

    try:
        return resp.json()
    except ValueError as exc:
        log.warning("Sheets API returned non-JSON for %s %s: %s", method, path, exc)
        return None


# ---------------------------------------------------------------------------
# Public API — Slice 5 (read)
# ---------------------------------------------------------------------------


def gsheets_read(
    user_id: str,
    sheet_id_or_url: str,
    range_a1: str,
) -> list[list[str]]:
    """Read a range of cells from a Google Sheet.

    Args:
        user_id:         Telegram chat_id used to look up the workspace token.
        sheet_id_or_url: Spreadsheet ID or full Google Sheets URL.
        range_a1:        A1 notation range (e.g. ``"A1:C10"``, ``"Sheet1!A1:B5"``).

    Returns:
        A 2D list of string values. Empty cells are represented as empty strings.
        Returns ``[]`` on any error (no token, sheet not found, API error).
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("gsheets_read: no valid workspace token for user_id=%r", user_id)
        return []

    sheet_id = _sheet_id_from_url(sheet_id_or_url)

    data = _call_sheets_api(
        f"/{sheet_id}/values/{range_a1}",
        token.access_token,
        method="GET",
    )
    if data is None:
        return []

    raw_rows = data.get("values", [])
    return [_normalise_row(row) for row in raw_rows]


# ---------------------------------------------------------------------------
# Public API — Slice 6 (write)
# ---------------------------------------------------------------------------


def gsheets_write(
    user_id: str,
    sheet_id_or_url: str,
    range_a1: str,
    values: list[list],
) -> bool:
    """Write values to a range in a Google Sheet.

    Uses the ``values.update`` endpoint with ``valueInputOption=USER_ENTERED``
    so formulas and dates are interpreted correctly.

    Args:
        user_id:         Telegram chat_id used to look up the workspace token.
        sheet_id_or_url: Spreadsheet ID or full Google Sheets URL.
        range_a1:        A1 notation range for the top-left anchor.
        values:          2D list of values to write.

    Returns:
        True on success, False on any error.
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("gsheets_write: no valid workspace token for user_id=%r", user_id)
        return False

    sheet_id = _sheet_id_from_url(sheet_id_or_url)

    body = {
        "range": range_a1,
        "majorDimension": "ROWS",
        "values": values,
    }

    data = _call_sheets_api(
        f"/{sheet_id}/values/{range_a1}",
        token.access_token,
        method="PUT",
        params={"valueInputOption": "USER_ENTERED"},
        json_body=body,
    )

    return data is not None


def gsheets_create(
    user_id: str,
    title: str,
) -> Optional[DriveFile]:
    """Create a new Google Spreadsheet.

    Args:
        user_id: Telegram chat_id used to look up the workspace token.
        title:   Title for the new spreadsheet.

    Returns:
        A DriveFile with the spreadsheet's ID and URL, or None on failure.
    """
    from datetime import timezone

    token = get_valid_token(user_id)
    if token is None:
        log.info("gsheets_create: no valid workspace token for user_id=%r", user_id)
        return None

    body = {
        "properties": {"title": title},
    }

    data = _call_sheets_api(
        "",
        token.access_token,
        method="POST",
        json_body=body,
    )
    if data is None:
        return None

    sheet_id = data.get("spreadsheetId", "")
    url = data.get("spreadsheetUrl", "")
    sheet_title = data.get("properties", {}).get("title", title)

    if not sheet_id:
        log.warning("gsheets_create: API response missing spreadsheetId")
        return None

    from datetime import datetime, timezone as tz_module
    return DriveFile(
        id=sheet_id,
        name=sheet_title,
        mime_type=_MIME_SHEET,
        modified_time=datetime.now(tz=tz_module.utc),
        url=url,
    )
