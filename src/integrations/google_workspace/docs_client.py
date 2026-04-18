"""
Google Docs API client — read and write operations.

Provides:
  - ``gdocs_read``   — fetch the full plain-text content of a Google Doc (Slice 2)
  - ``gdocs_create`` — create a new Google Doc (Slice 3)
  - ``gdocs_edit``   — apply batchUpdate requests to an existing Doc (Slice 3)

Design principles (consistent with gmail and calendar clients):
- Immutable value objects (frozen dataclasses)
- Pure helpers isolated from I/O
- Side effects (network calls, token refresh) at the boundaries
- No credentials or token values ever appear in logs
- Returns None (not raises) on auth failure or API error for graceful degradation

Google Docs REST API docs:
    https://developers.google.com/docs/api/reference/rest
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

from integrations.google_workspace.token_store import get_valid_token

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API constants
# ---------------------------------------------------------------------------

_DOCS_API_BASE: str = "https://docs.googleapis.com/v1/documents"
_HTTP_TIMEOUT: int = 15


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocFile:
    """Immutable representation of a Google Doc.

    Attributes:
        id:    The document's unique ID (from the Google Docs URL).
        title: The document title as set in Google Docs.
        url:   Full HTTPS URL to open the document in a browser.
    """

    id: str
    title: str
    url: str


# ---------------------------------------------------------------------------
# Helpers (pure functions)
# ---------------------------------------------------------------------------


def _doc_id_from_url(doc_id_or_url: str) -> str:
    """Extract the document ID from a full Google Docs URL, or return as-is.

    Accepts:
    - A raw document ID: ``1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms``
    - A full URL: ``https://docs.google.com/document/d/<ID>/edit``
    - A URL without the path suffix

    Returns the document ID string.
    """
    match = re.search(r"/document/d/([a-zA-Z0-9_-]+)", doc_id_or_url)
    if match:
        return match.group(1)
    return doc_id_or_url.strip()


def _extract_text_from_doc(doc_json: dict) -> str:
    """Extract plain text from a Google Docs API response.

    Traverses the structural elements (paragraphs, table cells, etc.) and
    concatenates their text content. Preserves paragraph breaks.

    Args:
        doc_json: Parsed JSON response from the Docs API ``GET /documents/{id}`` endpoint.

    Returns:
        Plain-text string with newlines between structural elements.
    """
    parts: list[str] = []

    def _extract_paragraph_text(paragraph: dict) -> str:
        text_parts = []
        for element in paragraph.get("elements", []):
            text_run = element.get("textRun", {})
            content = text_run.get("content", "")
            if content:
                text_parts.append(content)
        return "".join(text_parts)

    for body_element in doc_json.get("body", {}).get("content", []):
        if "paragraph" in body_element:
            text = _extract_paragraph_text(body_element["paragraph"])
            if text:
                parts.append(text)
        elif "table" in body_element:
            for row in body_element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    for cell_element in cell.get("content", []):
                        if "paragraph" in cell_element:
                            text = _extract_paragraph_text(cell_element["paragraph"])
                            if text:
                                parts.append(text)

    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Internal I/O helpers
# ---------------------------------------------------------------------------


def _call_docs_api(
    path: str,
    access_token: str,
    method: str = "GET",
    json_body: Optional[dict] = None,
) -> Optional[dict]:
    """Make an authenticated call to the Google Docs API.

    Args:
        path:         URL path after the API base (e.g. ``"/1abc123"``).
        access_token: Bearer token for Google API auth.
        method:       HTTP method (``"GET"`` or ``"POST"``).
        json_body:    Optional JSON body for POST/PATCH requests.

    Returns:
        Parsed JSON response dict, or None on any error (auth, network, API).
    """
    url = f"{_DOCS_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        elif method == "PATCH":
            resp = requests.patch(url, headers=headers, json=json_body, timeout=_HTTP_TIMEOUT)
        else:
            resp = requests.post(url, headers=headers, json=json_body, timeout=_HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        log.warning("Docs API network error (%s %s): %s", method, path, exc)
        return None

    if resp.status_code == 401:
        log.warning("Docs API: 401 Unauthorized — token may be expired (path=%s)", path)
        return None

    if not resp.ok:
        log.warning("Docs API returned %d for %s %s", resp.status_code, method, path)
        return None

    try:
        return resp.json()
    except ValueError as exc:
        log.warning("Docs API returned non-JSON for %s %s: %s", method, path, exc)
        return None


# ---------------------------------------------------------------------------
# Public API — read operations (Slice 2)
# ---------------------------------------------------------------------------


def gdocs_read(user_id: str, doc_id_or_url: str) -> Optional[str]:
    """Read the plain-text content of a Google Doc.

    Fetches the full document structure from the Google Docs API and extracts
    plain text from all paragraphs and table cells.

    Args:
        user_id:       Telegram chat_id used to look up the workspace token.
        doc_id_or_url: Google Docs document ID or full document URL.

    Returns:
        Plain-text string with the document content, or None if the document
        cannot be accessed (no token, doc not found, API error).
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("gdocs_read: no valid workspace token for user_id=%r", user_id)
        return None

    doc_id = _doc_id_from_url(doc_id_or_url)

    data = _call_docs_api(f"/{doc_id}", token.access_token)
    if data is None:
        return None

    return _extract_text_from_doc(data)


# ---------------------------------------------------------------------------
# Public API — write operations (Slice 3)
# ---------------------------------------------------------------------------


def gdocs_create(user_id: str, title: str) -> Optional[DocFile]:
    """Create a new, empty Google Doc.

    Args:
        user_id: Telegram chat_id used to look up the workspace token.
        title:   Title for the new document.

    Returns:
        A DocFile with the new document's ID, title, and URL,
        or None on any error (no token, API error).
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("gdocs_create: no valid workspace token for user_id=%r", user_id)
        return None

    data = _call_docs_api(
        "",
        token.access_token,
        method="POST",
        json_body={"title": title},
    )
    if data is None:
        return None

    doc_id = data.get("documentId", "")
    if not doc_id:
        log.warning("gdocs_create: API response missing documentId")
        return None

    doc_title = data.get("title", title)
    url = f"https://docs.google.com/document/d/{doc_id}/edit"
    return DocFile(id=doc_id, title=doc_title, url=url)


def gdocs_edit(
    user_id: str,
    doc_id_or_url: str,
    requests_body: list[dict],
) -> bool:
    """Apply batchUpdate requests to an existing Google Doc.

    Uses the Docs API ``batchUpdate`` endpoint to apply one or more structured
    edit requests (insert text, delete range, format text, etc.).

    See: https://developers.google.com/docs/api/reference/rest/v1/documents/batchUpdate

    Args:
        user_id:       Telegram chat_id used to look up the workspace token.
        doc_id_or_url: Document ID or full Google Docs URL.
        requests_body: List of request dicts following the Docs API request format.
                       Example: [{"insertText": {"location": {"index": 1}, "text": "Hello"}}]

    Returns:
        True on success, False on any error (no token, doc not found, API error).
    """
    token = get_valid_token(user_id)
    if token is None:
        log.info("gdocs_edit: no valid workspace token for user_id=%r", user_id)
        return False

    doc_id = _doc_id_from_url(doc_id_or_url)

    data = _call_docs_api(
        f"/{doc_id}:batchUpdate",
        token.access_token,
        method="POST",
        json_body={"requests": requests_body},
    )
    return data is not None
