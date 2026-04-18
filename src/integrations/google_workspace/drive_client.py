"""
Google Drive API client — list and search operations.

Provides ``gdrive_list`` and ``gdrive_search`` to enumerate files in a Drive
folder or search by Drive query syntax.

Design principles (consistent with gmail, calendar, docs clients):
- Immutable value objects (frozen dataclasses)
- Pure helpers isolated from I/O
- Side effects (network calls, token refresh) at the boundaries
- No credentials or token values ever appear in logs
- Returns empty list (not None, not raises) on auth failure or API error

Google Drive REST API v3 docs:
    https://developers.google.com/drive/api/reference/rest/v3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from integrations.google_workspace.token_store import get_valid_token

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API constants
# ---------------------------------------------------------------------------

_DRIVE_API_BASE: str = "https://www.googleapis.com/drive/v3"
_HTTP_TIMEOUT: int = 15

# Fields requested from the Drive API for each file.
_FILE_FIELDS: str = "id,name,mimeType,modifiedTime,webViewLink"

# MIME type displayed for Google Docs, Sheets, Folders etc.
_MIME_FOLDER: str = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriveFile:
    """Immutable representation of a Google Drive file or folder.

    Attributes:
        id:            Drive file ID.
        name:          File or folder name.
        mime_type:     Google MIME type string (e.g. ``application/vnd.google-apps.document``).
        modified_time: Last modification time (UTC, timezone-aware).
        url:           HTTPS URL to open the file in a browser.
    """

    id: str
    name: str
    mime_type: str
    modified_time: datetime
    url: str


# ---------------------------------------------------------------------------
# Helpers (pure functions)
# ---------------------------------------------------------------------------


def _parse_drive_file(item: dict) -> Optional[DriveFile]:
    """Parse a single Drive API file item into a DriveFile.

    Returns None if any required field is missing or malformed.
    """
    file_id = item.get("id", "").strip()
    name = item.get("name", "").strip()
    mime_type = item.get("mimeType", "").strip()
    url = item.get("webViewLink", "").strip()
    modified_time_str = item.get("modifiedTime", "")

    if not file_id or not name:
        return None

    try:
        modified_time = datetime.fromisoformat(modified_time_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        modified_time = datetime.now(tz=timezone.utc)

    return DriveFile(
        id=file_id,
        name=name,
        mime_type=mime_type,
        modified_time=modified_time,
        url=url,
    )


# ---------------------------------------------------------------------------
# Internal I/O helpers
# ---------------------------------------------------------------------------


def _call_drive_api(
    path: str,
    access_token: str,
    params: Optional[dict] = None,
) -> Optional[dict]:
    """Make an authenticated GET call to the Google Drive API.

    Args:
        path:         URL path after the API base (e.g. ``"/files"``).
        access_token: Bearer token for Google API auth.
        params:       Optional query parameters.

    Returns:
        Parsed JSON response dict, or None on any error.
    """
    url = f"{_DRIVE_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = requests.get(url, headers=headers, params=params or {}, timeout=_HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        log.warning("Drive API network error (GET %s): %s", path, exc)
        return None

    if resp.status_code == 401:
        log.warning("Drive API: 401 Unauthorized — token may be expired (path=%s)", path)
        return None

    if not resp.ok:
        log.warning("Drive API returned %d for GET %s", resp.status_code, path)
        return None

    try:
        return resp.json()
    except ValueError as exc:
        log.warning("Drive API returned non-JSON for GET %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def gdrive_list(
    user_id: str,
    folder_id: str = "root",
    max_results: int = 20,
) -> list[DriveFile]:
    """List files in a Google Drive folder.

    Args:
        user_id:     Telegram chat_id used to look up the workspace token.
        folder_id:   Drive folder ID to list. Defaults to ``"root"`` (My Drive).
        max_results: Maximum number of files to return (1–100).

    Returns:
        List of DriveFile objects, sorted by the Drive API default order
        (most recently modified first). Returns ``[]`` on any error.
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("gdrive_list: no valid workspace token for user_id=%r", user_id)
        return []

    params = {
        "q": f"'{folder_id}' in parents and trashed=false",
        "pageSize": min(max(1, max_results), 100),
        "fields": f"files({_FILE_FIELDS})",
        "orderBy": "modifiedTime desc",
    }

    data = _call_drive_api("/files", token.access_token, params=params)
    if data is None:
        return []

    files = []
    for item in data.get("files", []):
        parsed = _parse_drive_file(item)
        if parsed is not None:
            files.append(parsed)

    return files


def gdrive_search(
    user_id: str,
    query: str,
    max_results: int = 10,
) -> list[DriveFile]:
    """Search Google Drive using Drive query syntax.

    The ``query`` parameter follows the Drive API query syntax. Examples:
    - ``"name contains 'budget'"``
    - ``"mimeType='application/vnd.google-apps.document'"``
    - ``"name contains 'report' and mimeType='application/vnd.google-apps.spreadsheet'"``

    See: https://developers.google.com/drive/api/guides/search-files

    Args:
        user_id:     Telegram chat_id used to look up the workspace token.
        query:       Drive API query string.
        max_results: Maximum number of files to return (1–100).

    Returns:
        List of DriveFile objects. Returns ``[]`` on any error or no results.
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("gdrive_search: no valid workspace token for user_id=%r", user_id)
        return []

    # Always exclude trashed files
    full_query = f"({query}) and trashed=false"

    params = {
        "q": full_query,
        "pageSize": min(max(1, max_results), 100),
        "fields": f"files({_FILE_FIELDS})",
        "orderBy": "modifiedTime desc",
    }

    data = _call_drive_api("/files", token.access_token, params=params)
    if data is None:
        return []

    files = []
    for item in data.get("files", []):
        parsed = _parse_drive_file(item)
        if parsed is not None:
            files.append(parsed)

    return files
