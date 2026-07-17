"""
Fireflies.ai GraphQL API client.

Provides a clean, typed Python layer over the Fireflies.ai public GraphQL
API so Lobster can list and retrieve call transcripts — mirrors the design
of ``integrations.granola.client`` (same HTTP/retry conventions, same
multi-account shape) but adapted to Fireflies' GraphQL query surface.

All HTTP calls go through ``_call_api``, the single point of contact with
the network. Rate limits (429) are retried with exponential backoff. Auth
failures (401/403, or a GraphQL ``errors`` array whose message looks
auth-related) raise ``FirefliesAuthError``.

Design principles (consistent with other integrations):
- Immutable value objects (frozen dataclasses)
- Pure parsing helpers isolated from I/O
- Side effects (network calls) kept at the boundaries
- No credentials ever appear in logs or exception messages
- Timezone-aware datetimes throughout (UTC)

Fireflies public API docs:
    https://docs.fireflies.ai/

Environment variables:
    FIREFLIES_API_KEY            — primary account's API key (required)
    FIREFLIES_API_KEY_<NAME>     — additional team members' keys, e.g.
                                    FIREFLIES_API_KEY_ALICE, FIREFLIES_API_KEY_BOB.
                                    Any suffix is picked up automatically —
                                    no code change needed to add a new
                                    teammate's account. See
                                    build_account_configs_from_env().
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fireflies GraphQL API constants
# ---------------------------------------------------------------------------

_FIREFLIES_ENDPOINT: str = "https://api.fireflies.ai/graphql"
_HTTP_TIMEOUT: int = 20

# Fireflies caps `limit` at 50 transcripts per page (per API docs).
_MAX_PAGE_SIZE: int = 50

# Rate limiting backoff (mirrors integrations.granola.client).
_RATE_LIMIT_BACKOFF_BASE: float = 1.0
_RATE_LIMIT_MAX_RETRIES: int = 4


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirefliesAttendee:
    """Immutable meeting attendee record."""

    name: str
    email: str


@dataclass(frozen=True)
class FirefliesSentence:
    """A single transcript utterance."""

    index: int
    speaker_name: str
    speaker_id: str
    text: str
    start_time: Optional[float]
    end_time: Optional[float]


@dataclass(frozen=True)
class FirefliesSummary:
    """
    AI-generated summary attached to a transcript.

    ``action_items`` is the field the sales-CRM use case cares about most —
    Fireflies returns it as a Markdown-formatted bullet list of next steps.
    """

    overview: str = ""
    action_items: str = ""
    keywords: list[str] = field(default_factory=list)
    outline: str = ""
    shorthand_bullet: str = ""
    bullet_gist: str = ""
    gist: str = ""
    short_summary: str = ""


# Account name constant for the primary (required) API key.
ACCOUNT_PRIMARY: str = "primary"


@dataclass(frozen=True)
class FirefliesTranscript:
    """
    Immutable representation of a single Fireflies call transcript.

    The ``fireflies_account`` field identifies which configured Fireflies
    account this transcript came from ('primary', or a teammate's account
    name such as 'alice'). Defaults to 'primary'.
    """

    id: str
    title: str
    date: Optional[datetime] = None
    duration: Optional[float] = None
    transcript_url: str = ""
    meeting_link: str = ""
    host_email: str = ""
    organizer_email: str = ""
    participants: list[str] = field(default_factory=list)
    meeting_attendees: list[FirefliesAttendee] = field(default_factory=list)
    summary: FirefliesSummary = field(default_factory=FirefliesSummary)
    sentences: list[FirefliesSentence] = field(default_factory=list)
    fireflies_account: str = ACCOUNT_PRIMARY


@dataclass(frozen=True)
class TranscriptListPage:
    """A single page of results from list_transcripts()."""

    transcripts: list[FirefliesTranscript]
    has_more: bool
    skip: int = 0
    limit: int = _MAX_PAGE_SIZE


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FirefliesAPIError(RuntimeError):
    """Non-auth error from the Fireflies API (HTTP-level or GraphQL `errors`)."""

    def __init__(self, status_code: int, summary: str = "") -> None:
        self.status_code = status_code
        msg = f"Fireflies API error {status_code}" if status_code else "Fireflies API error"
        if summary:
            msg += f": {summary}"
        super().__init__(msg)


class FirefliesAuthError(FirefliesAPIError):
    """Authentication / authorisation failure (401/403, or an auth-shaped GraphQL error)."""

    def __init__(self) -> None:
        super().__init__(401, "authentication failed — check FIREFLIES_API_KEY")


class FirefliesNotFoundError(FirefliesAPIError):
    """Transcript not found (the `transcript` query returned null)."""

    def __init__(self, transcript_id: str) -> None:
        self.transcript_id = transcript_id
        super().__init__(404, f"transcript {transcript_id!r} not found")


class FirefliesUnknownAccountError(KeyError):
    """
    Raised when a transcript's fireflies_account name has no registered API key.

    Mirrors GranolaUnknownAccountError: signals a configuration gap (a new
    account name appeared without a matching key in config.env) rather than
    silently falling back to the primary account's key.
    """

    def __init__(self, account_name: str) -> None:
        self.account_name = account_name
        super().__init__(
            f"No API key registered for Fireflies account {account_name!r}. "
            f"Add FIREFLIES_API_KEY_{account_name.upper()} to ~/lobster-config/config.env."
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_api_key() -> str:
    """Load FIREFLIES_API_KEY from environment. Raises ValueError if missing."""
    key = os.environ.get("FIREFLIES_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "FIREFLIES_API_KEY environment variable is not set. "
            "Set it in ~/lobster-config/config.env."
        )
    return key


def _parse_dt(value: Optional[Any]) -> Optional[datetime]:
    """
    Parse a Fireflies date value → UTC datetime, or None if blank/None.

    Fireflies returns ISO 8601 strings for `date` in transcript responses,
    but some deprecated fields use epoch milliseconds (float). Handle both.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        log.debug("Could not parse Fireflies datetime: %r", value)
        return None


def _parse_attendee(raw: dict[str, Any]) -> FirefliesAttendee:
    return FirefliesAttendee(
        name=raw.get("name") or "",
        email=raw.get("email") or "",
    )


def _parse_sentence(raw: dict[str, Any]) -> FirefliesSentence:
    return FirefliesSentence(
        index=raw.get("index") or 0,
        speaker_name=raw.get("speaker_name") or "",
        speaker_id=str(raw.get("speaker_id") or ""),
        text=raw.get("text") or "",
        start_time=raw.get("start_time"),
        end_time=raw.get("end_time"),
    )


def _parse_summary(raw: Optional[dict[str, Any]]) -> FirefliesSummary:
    if not raw:
        return FirefliesSummary()
    return FirefliesSummary(
        overview=raw.get("overview") or "",
        action_items=raw.get("action_items") or "",
        keywords=list(raw.get("keywords") or []),
        outline=raw.get("outline") or "",
        shorthand_bullet=raw.get("shorthand_bullet") or "",
        bullet_gist=raw.get("bullet_gist") or "",
        gist=raw.get("gist") or "",
        short_summary=raw.get("short_summary") or "",
    )


def _parse_transcript(
    raw: dict[str, Any], fireflies_account: str = ACCOUNT_PRIMARY
) -> FirefliesTranscript:
    """Convert a raw API transcript dict → FirefliesTranscript dataclass."""
    return FirefliesTranscript(
        id=raw["id"],
        title=raw.get("title") or "Untitled",
        date=_parse_dt(raw.get("date")),
        duration=raw.get("duration"),
        transcript_url=raw.get("transcript_url") or "",
        meeting_link=raw.get("meeting_link") or "",
        host_email=raw.get("host_email") or "",
        organizer_email=raw.get("organizer_email") or "",
        participants=list(raw.get("participants") or []),
        meeting_attendees=[_parse_attendee(a) for a in raw.get("meeting_attendees") or []],
        summary=_parse_summary(raw.get("summary")),
        sentences=[_parse_sentence(s) for s in raw.get("sentences") or []],
        fireflies_account=fireflies_account,
    )


def _looks_like_auth_error(message: str) -> bool:
    """Heuristic: does a GraphQL error message describe an auth failure?"""
    lowered = message.lower()
    return any(token in lowered for token in ("unauthor", "auth", "api key", "api_key"))


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

_LIST_TRANSCRIPTS_QUERY = """
query Transcripts($limit: Int, $skip: Int, $fromDate: DateTime) {
  transcripts(limit: $limit, skip: $skip, fromDate: $fromDate) {
    id
    title
    date
  }
}
"""

_GET_TRANSCRIPT_QUERY = """
query Transcript($transcriptId: String!) {
  transcript(id: $transcriptId) {
    id
    title
    date
    duration
    transcript_url
    meeting_link
    host_email
    organizer_email
    participants
    meeting_attendees {
      name
      email
    }
    summary {
      overview
      action_items
      keywords
      outline
      shorthand_bullet
      bullet_gist
      gist
      short_summary
    }
    sentences {
      index
      speaker_name
      speaker_id
      text
      start_time
      end_time
    }
  }
}
"""


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _call_api(
    query: str,
    variables: dict[str, Any],
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """
    Make one authenticated GraphQL request to the Fireflies API.

    Handles 429 rate-limit with exponential backoff (up to
    _RATE_LIMIT_MAX_RETRIES). Raises FirefliesAuthError on 401/403 (or a
    GraphQL `errors` array that looks auth-related), FirefliesAPIError on
    other non-2xx or GraphQL error responses.

    Returns the `data` object from the GraphQL response.
    """
    if api_key is None:
        api_key = _get_api_key()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables}

    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = requests.request(
                "POST",
                _FIREFLIES_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("Fireflies API request failed (network): %s", exc)
            raise FirefliesAPIError(0, "network error") from exc

        if resp.status_code == 429:
            if attempt < _RATE_LIMIT_MAX_RETRIES:
                wait = _RATE_LIMIT_BACKOFF_BASE * (2 ** attempt)
                log.warning("Fireflies rate limit hit, waiting %.1fs (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            raise FirefliesAPIError(429, "rate limit exceeded after retries")

        if resp.status_code in (401, 403):
            raise FirefliesAuthError()

        if not resp.ok:
            raise FirefliesAPIError(resp.status_code, resp.text[:200])

        body = resp.json()
        errors = body.get("errors")
        if errors:
            message = "; ".join(str(e.get("message", "")) for e in errors)
            if _looks_like_auth_error(message):
                raise FirefliesAuthError()
            raise FirefliesAPIError(0, message[:200])

        return body.get("data") or {}

    # Should never reach here
    raise FirefliesAPIError(0, "unexpected retry exhaustion")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_transcripts(
    since: Optional[datetime] = None,
    skip: int = 0,
    limit: int = _MAX_PAGE_SIZE,
    api_key: Optional[str] = None,
    fireflies_account: str = ACCOUNT_PRIMARY,
) -> TranscriptListPage:
    """
    List call transcripts, newest-first (summary fields only — id/title/date).

    Args:
        since:             If provided, only return transcripts on/after this
                            datetime (Fireflies `fromDate` filter).
        skip:               Pagination offset.
        limit:              Max transcripts per page. Clamped to Fireflies'
                             documented maximum of 50.
        api_key:            Override FIREFLIES_API_KEY env var.
        fireflies_account:  Account identifier to embed in returned transcripts.

    Returns:
        TranscriptListPage with transcripts, has_more flag, skip and limit used.
        has_more is a heuristic: True iff the page returned exactly `limit`
        items (Fireflies does not return a total count or cursor).
    """
    effective_limit = min(limit, _MAX_PAGE_SIZE)
    variables: dict[str, Any] = {"limit": effective_limit, "skip": skip}
    if since is not None:
        variables["fromDate"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    data = _call_api(_LIST_TRANSCRIPTS_QUERY, variables, api_key=api_key)
    raw_list = data.get("transcripts") or []
    transcripts = [_parse_transcript(t, fireflies_account=fireflies_account) for t in raw_list]

    has_more = effective_limit > 0 and len(raw_list) == effective_limit

    return TranscriptListPage(
        transcripts=transcripts,
        has_more=has_more,
        skip=skip,
        limit=effective_limit,
    )


def get_transcript(
    transcript_id: str,
    api_key: Optional[str] = None,
    fireflies_account: str = ACCOUNT_PRIMARY,
) -> FirefliesTranscript:
    """
    Retrieve a single transcript by ID, including summary and sentences.

    Args:
        transcript_id:      The transcript's `id` field.
        api_key:            Override FIREFLIES_API_KEY env var.
        fireflies_account:  Account identifier to embed in returned transcript.

    Raises:
        FirefliesNotFoundError: if the transcript does not exist.
    """
    data = _call_api(_GET_TRANSCRIPT_QUERY, {"transcriptId": transcript_id}, api_key=api_key)
    raw = data.get("transcript")
    if raw is None:
        raise FirefliesNotFoundError(transcript_id)
    return _parse_transcript(raw, fireflies_account=fireflies_account)


def iter_all_transcripts(
    since: Optional[datetime] = None,
    api_key: Optional[str] = None,
    fireflies_account: str = ACCOUNT_PRIMARY,
    limit: int = _MAX_PAGE_SIZE,
) -> list[FirefliesTranscript]:
    """
    Fetch ALL transcript summaries (following pagination via skip/limit).

    Returns a flat list of FirefliesTranscript objects (summary fields only —
    callers needing full detail should call get_transcript() per ID).
    """
    all_transcripts: list[FirefliesTranscript] = []
    skip = 0

    while True:
        page = list_transcripts(
            since=since, skip=skip, limit=limit, api_key=api_key, fireflies_account=fireflies_account
        )
        all_transcripts.extend(page.transcripts)
        log.debug("Fetched page: %d transcripts, has_more=%s", len(page.transcripts), page.has_more)

        if not page.has_more:
            break
        skip += page.limit

    log.info("iter_all_transcripts: fetched %d total transcripts", len(all_transcripts))
    return all_transcripts


# ---------------------------------------------------------------------------
# Multi-account support
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirefliesAccountConfig:
    """
    Immutable descriptor for a single Fireflies account used in multi-account polling.

    Attributes:
        name:    Account identifier ('primary', or a teammate's name e.g. 'alice').
        api_key: Bearer token for this account.
    """

    name: str
    api_key: str


# Matches FIREFLIES_API_KEY_<NAME> but not the bare FIREFLIES_API_KEY itself.
_NAMED_ACCOUNT_KEY_RE = re.compile(r"^FIREFLIES_API_KEY_(.+)$")


def build_account_configs_from_env(env: Optional[dict[str, str]] = None) -> list[FirefliesAccountConfig]:
    """
    Discover configured Fireflies accounts from environment variables.

    Rules:
    - FIREFLIES_API_KEY is required (primary account). Returns empty list if absent.
    - Any FIREFLIES_API_KEY_<NAME> variable is treated as an additional account,
      named after the lowercased suffix (e.g. FIREFLIES_API_KEY_ALICE → 'alice').
      This is discovered dynamically by scanning env — unlike Granola's
      build_account_configs_from_env(), which only recognises a single
      hardcoded '_2' suffix and silently ignores any other named key. Adding a
      new teammate's Fireflies account here requires no code change, only a
      new env var in config.env.
    - Primary account is always first in the returned list; named accounts
      follow in alphabetical order (deterministic, independent of dict
      iteration order).

    Args:
        env: Dict of environment variables. Defaults to os.environ.

    Returns:
        List of FirefliesAccountConfig, primary account first.
    """
    if env is None:
        env = dict(os.environ)

    primary_key = env.get("FIREFLIES_API_KEY", "").strip()
    if not primary_key:
        return []

    configs: list[FirefliesAccountConfig] = [
        FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key=primary_key),
    ]

    named: list[tuple[str, str]] = []
    for key, value in env.items():
        match = _NAMED_ACCOUNT_KEY_RE.match(key)
        if not match:
            continue
        stripped_value = (value or "").strip()
        if not stripped_value:
            continue
        named.append((match.group(1).lower(), stripped_value))

    for name, api_key in sorted(named, key=lambda pair: pair[0]):
        configs.append(FirefliesAccountConfig(name=name, api_key=api_key))

    return configs


def iter_all_transcripts_for_account(
    account: FirefliesAccountConfig,
    since: Optional[datetime] = None,
) -> list[FirefliesTranscript]:
    """
    Fetch ALL transcript summaries for a specific account, with account attribution.

    Identical to iter_all_transcripts() but takes a FirefliesAccountConfig so
    the api_key and account name are bundled together.
    """
    log.info("Fetching transcripts for Fireflies account '%s'", account.name)
    return iter_all_transcripts(
        since=since,
        api_key=account.api_key,
        fireflies_account=account.name,
    )
