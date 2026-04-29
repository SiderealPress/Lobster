"""
Granola API client — Slice 0.

Provides a clean, typed Python layer over the Granola public REST API
so Lobster can list and retrieve meeting notes.

All HTTP calls go through ``_call_api``, which is the single point of
contact with the network. Rate limits (429) are handled with exponential
backoff. Auth failures (401/403) raise ``GranolaAuthError``.

Design principles (consistent with other integrations):
- Immutable value objects (frozen dataclasses)
- Pure helpers isolated from I/O
- Side effects (network calls) kept at the boundaries
- No credentials ever appear in logs or exception messages
- Timezone-aware datetimes throughout (UTC)

Granola public API docs:
    https://docs.granola.ai/introduction

Environment variables:
    GRANOLA_API_KEY  — Bearer token with grn_* prefix
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Granola REST API constants
# ---------------------------------------------------------------------------

_GRANOLA_API_BASE: str = "https://public-api.granola.ai"
_HTTP_TIMEOUT: int = 20

# Rate limiting: 5 req/s sustained, 25 burst. We stay well under.
_RATE_LIMIT_BACKOFF_BASE: float = 1.0
_RATE_LIMIT_MAX_RETRIES: int = 4


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GranolaOwner:
    """Immutable owner/user record."""

    name: str
    email: str


@dataclass(frozen=True)
class GranolaAttendee:
    """Immutable attendee record."""

    name: str
    email: str


@dataclass(frozen=True)
class GranolaCalendarEvent:
    """Immutable calendar metadata attached to a note."""

    event_title: str
    calendar_event_id: str
    scheduled_start_time: Optional[datetime]
    scheduled_end_time: Optional[datetime]
    invitees: list[GranolaAttendee] = field(default_factory=list)
    organiser: Optional[GranolaAttendee] = None


@dataclass(frozen=True)
class GranolaTranscriptSegment:
    """A single transcript utterance."""

    speaker: str
    text: str
    start_time: str  # ISO 8601 string
    end_time: str    # ISO 8601 string


# Account name constants (canonical values shared across pipelines)
ACCOUNT_DREW: str = "drew"  # noname
ACCOUNT_KELLY: str = "kelly"  # noname


@dataclass(frozen=True)
class GranolaNote:
    """
    Immutable representation of a single Granola meeting note.

    Fields reflect what the public API returns for GET /v1/notes/{id}
    with ?include=transcript.

    The ``granola_account`` field identifies which Granola account this note
    came from ('drew' or 'kelly'). Defaults to 'drew' for backward compat.  # noname
    """

    id: str
    title: str
    owner: GranolaOwner
    created_at: datetime
    updated_at: datetime
    summary_markdown: str = ""
    summary_text: str = ""
    attendees: list[GranolaAttendee] = field(default_factory=list)
    calendar_event: Optional[GranolaCalendarEvent] = None
    transcript: list[GranolaTranscriptSegment] = field(default_factory=list)
    granola_account: str = ACCOUNT_DREW  # noname


@dataclass(frozen=True)
class NoteListPage:
    """A single page of results from list_notes()."""

    notes: list[GranolaNote]
    has_more: bool
    cursor: Optional[str]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GranolaAPIError(RuntimeError):
    """Non-auth HTTP error from the Granola API."""

    def __init__(self, status_code: int, summary: str = "") -> None:
        self.status_code = status_code
        msg = f"Granola API error {status_code}"
        if summary:
            msg += f": {summary}"
        super().__init__(msg)


class GranolaAuthError(GranolaAPIError):
    """Authentication / authorisation failure (401 or 403)."""

    def __init__(self) -> None:
        super().__init__(401, "authentication failed — check GRANOLA_API_KEY")


class GranolaNotFoundError(GranolaAPIError):
    """Note not found (404 — note may still be processing / unsummarised)."""

    def __init__(self, note_id: str) -> None:
        self.note_id = note_id
        super().__init__(404, f"note {note_id!r} not found or not yet summarised")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_api_key() -> str:
    """Load GRANOLA_API_KEY from environment. Raises ValueError if missing."""
    key = os.environ.get("GRANOLA_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "GRANOLA_API_KEY environment variable is not set. "
            "Set it in ~/lobster-config/config.env."
        )
    return key


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 string → UTC datetime, or None if blank/None."""
    if not value:
        return None
    try:
        # Python 3.11+ handles 'Z' directly; handle older versions too
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        log.debug("Could not parse datetime: %r", value)
        return None


def _parse_attendee(raw: dict[str, Any]) -> GranolaAttendee:
    return GranolaAttendee(
        name=raw.get("name") or "",
        email=raw.get("email") or "",
    )


def _parse_calendar_event(raw: Optional[dict[str, Any]]) -> Optional[GranolaCalendarEvent]:
    if not raw:
        return None
    invitees = [_parse_attendee(a) for a in raw.get("invitees") or []]
    organiser_raw = raw.get("organiser")
    # organiser may be a plain email string or a dict {name, email}
    organiser: Optional[GranolaAttendee] = None
    if isinstance(organiser_raw, dict):
        organiser = _parse_attendee(organiser_raw)
    elif isinstance(organiser_raw, str) and organiser_raw:
        organiser = GranolaAttendee(name="", email=organiser_raw)
    return GranolaCalendarEvent(
        event_title=raw.get("event_title") or "",
        calendar_event_id=raw.get("calendar_event_id") or "",
        scheduled_start_time=_parse_dt(raw.get("scheduled_start_time")),
        scheduled_end_time=_parse_dt(raw.get("scheduled_end_time")),
        invitees=invitees,
        organiser=organiser,
    )


def _parse_transcript(raw_list: Optional[list[dict[str, Any]]]) -> list[GranolaTranscriptSegment]:
    if not raw_list:
        return []
    segments = []
    for item in raw_list:
        # speaker may be a plain string or a dict {"source": "speaker"} or {"name": "Alice"}
        speaker_raw = item.get("speaker")
        if isinstance(speaker_raw, dict):
            # Use "name" if present, else "source", else empty
            speaker = speaker_raw.get("name") or speaker_raw.get("source") or ""
        elif isinstance(speaker_raw, str):
            speaker = speaker_raw
        else:
            speaker = ""
        segments.append(GranolaTranscriptSegment(
            speaker=speaker,
            text=item.get("text") or "",
            start_time=item.get("start_time") or "",
            end_time=item.get("end_time") or "",
        ))
    return segments


def _parse_note(raw: dict[str, Any], granola_account: str = ACCOUNT_DREW) -> GranolaNote:  # noname
    """Convert a raw API note dict → GranolaNote dataclass."""
    owner_raw = raw.get("owner") or {}
    owner = GranolaOwner(
        name=owner_raw.get("name") or "",
        email=owner_raw.get("email") or "",
    )
    attendees = [_parse_attendee(a) for a in raw.get("attendees") or []]
    created_at = _parse_dt(raw.get("created_at")) or datetime.now(timezone.utc)
    updated_at = _parse_dt(raw.get("updated_at")) or datetime.now(timezone.utc)

    return GranolaNote(
        id=raw["id"],
        title=raw.get("title") or "Untitled",
        owner=owner,
        created_at=created_at,
        updated_at=updated_at,
        summary_markdown=raw.get("summary_markdown") or "",
        summary_text=raw.get("summary_text") or "",
        attendees=attendees,
        calendar_event=_parse_calendar_event(raw.get("calendar_event")),
        transcript=_parse_transcript(raw.get("transcript")),
        granola_account=granola_account,
    )


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _call_api(
    method: str,
    path: str,
    params: Optional[dict[str, str]] = None,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """
    Make one authenticated request to the Granola API.

    Handles 429 rate-limit with exponential backoff (up to _RATE_LIMIT_MAX_RETRIES).
    Raises GranolaAuthError on 401/403, GranolaNotFoundError on 404,
    GranolaAPIError on other non-2xx.
    """
    if api_key is None:
        api_key = _get_api_key()

    url = f"{_GRANOLA_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("Granola API request failed (network): %s", exc)
            raise GranolaAPIError(0, "network error") from exc

        if resp.status_code == 429:
            if attempt < _RATE_LIMIT_MAX_RETRIES:
                wait = _RATE_LIMIT_BACKOFF_BASE * (2 ** attempt)
                log.warning("Granola rate limit hit, waiting %.1fs (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            else:
                raise GranolaAPIError(429, "rate limit exceeded after retries")

        if resp.status_code in (401, 403):
            raise GranolaAuthError()

        if resp.status_code == 404:
            # Extract note ID from path if present (/v1/notes/<id>)
            note_id = path.rsplit("/", 1)[-1] if "/" in path else path
            raise GranolaNotFoundError(note_id)

        if not resp.ok:
            raise GranolaAPIError(resp.status_code, resp.text[:200])

        return resp.json()

    # Should never reach here
    raise GranolaAPIError(0, "unexpected retry exhaustion")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_notes(
    since: Optional[datetime] = None,
    cursor: Optional[str] = None,
    limit: int = 100,
    api_key: Optional[str] = None,
    granola_account: str = ACCOUNT_DREW,  # noname
) -> NoteListPage:
    """
    List meeting notes, newest-first.

    Args:
        since:           If provided, only return notes with created_at >= this datetime.
                         Used for incremental sync. Pass a timezone-aware datetime.
        cursor:          Pagination cursor from a previous NoteListPage response.
        limit:           Max notes per page (API max appears to be 100).
        api_key:         Override GRANOLA_API_KEY env var.
        granola_account: Account identifier to embed in returned GranolaNote objects.

    Returns:
        NoteListPage with notes, has_more flag, and next cursor.

    Notes returned by the API are only notes that have a generated AI summary
    and transcript. Processing or unsummarised notes are excluded.
    """
    params: dict[str, str] = {"limit": str(limit)}
    if since is not None:
        # API expects ISO 8601 in UTC
        params["created_after"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if cursor is not None:
        params["cursor"] = cursor

    data = _call_api("GET", "/v1/notes", params=params, api_key=api_key)

    raw_notes = data.get("notes") or []
    notes = [_parse_note(n, granola_account=granola_account) for n in raw_notes]

    return NoteListPage(
        notes=notes,
        has_more=bool(data.get("hasMore", False)),
        cursor=data.get("cursor"),
    )


def get_note(
    note_id: str,
    include_transcript: bool = True,
    api_key: Optional[str] = None,
    granola_account: str = ACCOUNT_DREW,  # noname
) -> GranolaNote:
    """
    Retrieve a single note by ID.

    Args:
        note_id:            The note's ``id`` field (e.g. ``not_xeEBpfpKDHxtv6``).
        include_transcript: If True, include full transcript in response.
        api_key:            Override GRANOLA_API_KEY env var.
        granola_account:    Account identifier to embed in returned GranolaNote.

    Raises:
        GranolaNotFoundError: if the note does not exist or hasn't been summarised yet.
    """
    params: dict[str, str] = {}
    if include_transcript:
        params["include"] = "transcript"

    data = _call_api("GET", f"/v1/notes/{note_id}", params=params, api_key=api_key)
    return _parse_note(data, granola_account=granola_account)


def iter_all_notes(
    since: Optional[datetime] = None,
    api_key: Optional[str] = None,
    granola_account: str = ACCOUNT_DREW,  # noname
) -> list[GranolaNote]:
    """
    Fetch ALL notes (following pagination), optionally filtered by created_after.

    This handles cursor pagination automatically. For large vaults this may
    make multiple API calls — stay within rate limits (5 req/s).

    Returns a flat list of GranolaNote objects, all pages combined.
    """
    all_notes: list[GranolaNote] = []
    cursor: Optional[str] = None

    while True:
        page = list_notes(since=since, cursor=cursor, api_key=api_key, granola_account=granola_account)
        all_notes.extend(page.notes)
        log.debug("Fetched page: %d notes, has_more=%s", len(page.notes), page.has_more)

        if not page.has_more or not page.cursor:
            break
        cursor = page.cursor

    log.info("iter_all_notes: fetched %d total notes", len(all_notes))
    return all_notes


# ---------------------------------------------------------------------------
# Multi-account support
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GranolaAccountConfig:
    """
    Immutable descriptor for a single Granola account used in multi-account polling.

    Attributes:
        name:    Account identifier ('drew' or 'kelly').  # noname
        api_key: Bearer token for this account.
    """

    name: str
    api_key: str


def build_account_configs_from_env(env: Optional[dict[str, str]] = None) -> list[GranolaAccountConfig]:
    """
    Discover configured Granola accounts from environment variables.

    Rules:
    - GRANOLA_API_KEY is required (primary enterprise account).
    - GRANOLA_API_KEY_KELLY is optional (secondary personal account).  # noname
    - Primary account is always first in the returned list.
    - Returns empty list if GRANOLA_API_KEY is absent.

    Args:
        env: Dict of environment variables. Defaults to os.environ.

    Returns:
        List of GranolaAccountConfig, primary account first.
    """
    if env is None:
        env = dict(os.environ)

    primary_key = env.get("GRANOLA_API_KEY", "").strip()  # noname
    if not primary_key:
        return []

    configs: list[GranolaAccountConfig] = [
        GranolaAccountConfig(name=ACCOUNT_DREW, api_key=primary_key),  # noname
    ]

    secondary_key = env.get("GRANOLA_API_KEY_KELLY", "").strip()  # noname
    if secondary_key:
        configs.append(GranolaAccountConfig(name=ACCOUNT_KELLY, api_key=secondary_key))  # noname

    return configs


def iter_all_notes_for_account(
    account: GranolaAccountConfig,
    since: Optional[datetime] = None,
) -> list[GranolaNote]:
    """
    Fetch ALL notes for a specific account, with account attribution.

    Identical to iter_all_notes() but takes a GranolaAccountConfig so the
    api_key and account name are bundled together.

    Args:
        account: Account configuration (name + api_key).
        since:   If provided, only return notes created after this datetime.

    Returns:
        List of GranolaNote with granola_account set to account.name.
    """
    log.info("Fetching notes for account '%s'", account.name)
    return iter_all_notes(
        since=since,
        api_key=account.api_key,
        granola_account=account.name,
    )
