"""
Trigify Daily Sync — Phases 1 & 2.

Pulls LinkedIn post signals from Trigify, matches them against Kissinger,
writes contact events, discovers new prospects, and surfaces warm intro paths.

Usage (from ~/lobster):
    uv run python src/integrations/trigify/daily_sync.py

The script is also invoked by the scheduled job
~/lobster-workspace/scheduled-jobs/tasks/trigify-daily-sync.md.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import time

import httpx

# ---------------------------------------------------------------------------
# Ensure src/ is on path when invoked directly
# ---------------------------------------------------------------------------
_SRC = Path(__file__).resolve().parent.parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from integrations.trigify.oauth import TrigifyOAuthError, refresh_access_token
from integrations.trigify.token_store import TrigifyTokenData, load_token, save_token

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [trigify-sync] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TRIGIFY_API_BASE: str = "https://api.trigify.io/v1"
_HTTP_TIMEOUT: int = 30

KISSINGER_API_URL: str = os.environ.get(
    "KISSINGER_API_URL", "http://localhost:8080/graphql"
)
KISSINGER_API_TOKEN: str = os.environ.get("KISSINGER_API_TOKEN", "")

# Default searches to create if none exist yet
DEFAULT_SEARCHES: list[dict[str, str]] = [
    {"name": "supply chain planning", "query": "supply chain planning", "platform": "linkedin-posts"},
    {"name": "demand planning accuracy", "query": "demand planning accuracy", "platform": "linkedin-posts"},
    {"name": "Palantir Foundry", "query": "Palantir Foundry", "platform": "linkedin-posts"},
    {"name": "S&OP challenges", "query": "S&OP challenges", "platform": "linkedin-posts"},
    {"name": "rail car manufacturing", "query": "rail car manufacturing", "platform": "linkedin-posts"},
]

# ---------------------------------------------------------------------------
# Title exclusion — COO / Chief Operating Officer must never become a prospect
# ---------------------------------------------------------------------------
import re as _re

_EXCLUDED_TITLE_PATTERNS = [
    _re.compile(r'\bCOO\b', _re.IGNORECASE),
    _re.compile(r'Chief Operating Officer', _re.IGNORECASE),
    _re.compile(r'Chief Operations Officer', _re.IGNORECASE),
]


def is_excluded_title(title: str) -> bool:
    """Return True if the title matches a permanently-excluded role (e.g. COO)."""
    if not title:
        return False
    return any(p.search(title) for p in _EXCLUDED_TITLE_PATTERNS)


# Sectors that qualify an unmatched result as a prospect worth creating
TARGET_SECTOR_KEYWORDS: frozenset[str] = frozenset([
    "manufacturing", "supply chain", "defense", "aerospace", "industrial",
    "rail", "heavy equipment", "logistics", "operations", "procurement",
    "s&op", "demand planning", "materials",
])

# Admin Telegram chat ID for the digest (read from env)
ADMIN_CHAT_ID: str = os.environ.get(
    "LOBSTER_ADMIN_CHAT_ID",
    os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")[0],
)


# ===========================================================================
# Token management
# ===========================================================================

def ensure_valid_token() -> str:
    """Load and (if necessary) refresh the Trigify access token.

    Returns:
        A valid access token string.

    Raises:
        SystemExit: If no token is stored or refresh fails.
    """
    token = load_token()
    if token is None:
        log.error("No Trigify token found. Run the OAuth callback server first.")
        sys.exit(1)

    if token.is_valid():
        return token.access_token

    if token.refresh_token is None:
        log.error("Token expired and no refresh_token stored. Re-run OAuth.")
        sys.exit(1)

    log.info("Access token expired — refreshing…")
    try:
        new_token = refresh_access_token(token.refresh_token)
    except TrigifyOAuthError as exc:
        log.error("Token refresh failed: %s", exc)
        sys.exit(1)

    save_token(new_token)
    log.info("Token refreshed, new expiry: %s", new_token.expires_at.isoformat())
    return new_token.access_token


# ===========================================================================
# Trigify API helpers
# ===========================================================================

def _trigify_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


_RETRY_DELAYS: tuple[int, ...] = (5, 15, 30)   # seconds between retries


def _trigify_get_with_retry(url: str, headers: dict[str, str], **kwargs: Any) -> Optional[httpx.Response]:
    """GET with exponential-ish backoff on timeout/5xx errors."""
    delays = list(_RETRY_DELAYS)
    for attempt, delay in enumerate([-1] + delays):
        if delay >= 0:
            log.info("Retrying GET %s after %ds (attempt %d)…", url, delay, attempt + 1)
            time.sleep(delay)
        try:
            resp = httpx.get(url, headers=headers, **kwargs)
            if resp.status_code < 500:
                return resp
            log.warning("Trigify GET %s returned %s — will retry", url, resp.status_code)
        except httpx.TimeoutException as exc:
            log.warning("Trigify GET %s timed out: %s", url, exc)
        except httpx.RequestError as exc:
            log.warning("Trigify GET %s network error: %s", url, exc)
    return None


def _trigify_post_with_retry(url: str, headers: dict[str, str], **kwargs: Any) -> Optional[httpx.Response]:
    """POST with exponential-ish backoff on timeout/5xx errors."""
    delays = list(_RETRY_DELAYS)
    for attempt, delay in enumerate([-1] + delays):
        if delay >= 0:
            log.info("Retrying POST %s after %ds (attempt %d)…", url, delay, attempt + 1)
            time.sleep(delay)
        try:
            resp = httpx.post(url, headers=headers, **kwargs)
            if resp.status_code < 500:
                return resp
            log.warning("Trigify POST %s returned %s — will retry", url, resp.status_code)
        except httpx.TimeoutException as exc:
            log.warning("Trigify POST %s timed out: %s", url, exc)
        except httpx.RequestError as exc:
            log.warning("Trigify POST %s network error: %s", url, exc)
    return None


def trigify_list_searches(access_token: str) -> list[dict[str, Any]]:
    """Return all saved Trigify searches for the current user."""
    url = f"{TRIGIFY_API_BASE}/searches"
    try:
        resp = _trigify_get_with_retry(url, _trigify_headers(access_token), timeout=_HTTP_TIMEOUT)
        if resp is None:
            log.warning("All retries exhausted listing Trigify searches")
            return []
        resp.raise_for_status()
        data = resp.json()
        # Trigify may return {"data": [...]} or a bare list
        if isinstance(data, dict):
            return data.get("data", data.get("searches", []))
        return data if isinstance(data, list) else []
    except httpx.HTTPStatusError as exc:
        log.warning("Failed to list Trigify searches (%s): %s", exc.response.status_code, exc.response.text[:200])
        return []
    except httpx.RequestError as exc:
        log.warning("Network error listing Trigify searches: %s", exc)
        return []


def trigify_create_search(
    access_token: str,
    name: str,
    query: str,
    platform: str,
) -> Optional[dict[str, Any]]:
    """Create a new Trigify keyword search.

    Args:
        access_token: Valid Trigify access token.
        name:         Human-readable label for the search.
        query:        Keyword/phrase to monitor.
        platform:     e.g. "linkedin-posts".

    Returns:
        The created search dict, or None on failure.
    """
    url = f"{TRIGIFY_API_BASE}/searches"
    # Trigify API requires `query` to be an object with `keywords` (array) and
    # `monitoring_type`.  The top-level `platform` field is no longer accepted.
    payload = {
        "name": name,
        "query": {
            "keywords": [query],
            "monitoring_type": platform,
        },
    }
    try:
        resp = _trigify_post_with_retry(
            url,
            _trigify_headers(access_token),
            json=payload,
            timeout=_HTTP_TIMEOUT,
        )
        if resp is None:
            log.warning("All retries exhausted creating Trigify search '%s'", name)
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data
    except httpx.HTTPStatusError as exc:
        log.warning(
            "Failed to create Trigify search '%s' (%s): %s",
            name,
            exc.response.status_code,
            exc.response.text[:200],
        )
        return None
    except httpx.RequestError as exc:
        log.warning("Network error creating Trigify search '%s': %s", name, exc)
        return None


def trigify_get_results(
    access_token: str,
    search_id: str,
    since: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Fetch results for a saved search.

    Args:
        access_token: Valid Trigify access token.
        search_id:    ID of the saved search.
        since:        Only return results newer than this datetime (UTC).

    Returns:
        List of result dicts, each representing a LinkedIn post / person signal.
    """
    url = f"{TRIGIFY_API_BASE}/searches/{search_id}/results"
    params: dict[str, str] = {}
    if since:
        params["since"] = since.isoformat()

    try:
        resp = _trigify_get_with_retry(
            url,
            _trigify_headers(access_token),
            params=params,
            timeout=_HTTP_TIMEOUT,
        )
        if resp is None:
            log.warning("All retries exhausted fetching results for search %s", search_id)
            return []
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data.get("data", data.get("results", []))
        return data if isinstance(data, list) else []
    except httpx.HTTPStatusError as exc:
        log.warning(
            "Failed to fetch results for search %s (%s): %s",
            search_id,
            exc.response.status_code,
            exc.response.text[:200],
        )
        return []
    except httpx.RequestError as exc:
        log.warning("Network error fetching results for search %s: %s", search_id, exc)
        return []


def ensure_default_searches(
    access_token: str,
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create any missing default searches and return the full updated list.

    Args:
        access_token: Valid access token.
        existing:     Searches already returned by trigify_list_searches().

    Returns:
        Updated list including any newly created searches.
    """
    existing_names = {s.get("name", "").lower() for s in existing}
    result = list(existing)

    for spec in DEFAULT_SEARCHES:
        if spec["name"].lower() in existing_names:
            log.debug("Search '%s' already exists — skipping", spec["name"])
            continue
        log.info("Creating default search: '%s'", spec["name"])
        created = trigify_create_search(
            access_token,
            name=spec["name"],
            query=spec["query"],
            platform=spec["platform"],
        )
        if created:
            result.append(created)
            log.info("Created search id=%s", created.get("id", "?"))
        else:
            log.warning("Could not create search '%s'", spec["name"])

    return result


# ===========================================================================
# Kissinger GraphQL helpers
# ===========================================================================

def _kissinger_headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if KISSINGER_API_TOKEN:
        h["Authorization"] = f"Bearer {KISSINGER_API_TOKEN}"
    return h


def _kissinger_gql(query: str, variables: dict[str, Any] = {}) -> Optional[dict[str, Any]]:
    """Execute a Kissinger GraphQL query/mutation.

    Returns:
        The ``data`` dict from the response, or None on error.
    """
    try:
        resp = httpx.post(
            KISSINGER_API_URL,
            headers=_kissinger_headers(),
            json={"query": query, "variables": variables},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            log.warning("Kissinger GraphQL errors: %s", body["errors"])
        return body.get("data")
    except httpx.HTTPStatusError as exc:
        log.warning("Kissinger request failed (%s): %s", exc.response.status_code, exc.response.text[:200])
        return None
    except httpx.RequestError as exc:
        log.warning("Kissinger network error: %s", exc)
        return None


_ENTITY_BY_META_QUERY = """
query FindByLinkedIn($kind: String, $first: Int, $after: String) {
  entities(kind: $kind, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        tags
        updatedAt
      }
    }
  }
}
"""

# EntitySummaryGql (returned by the entities list) no longer has a `meta`
# field.  Full entity data (including meta) requires a separate entity(id)
# query.  For the linkedin_url map we therefore do a lazy per-entity fetch
# only when we have a name match candidate.

_ENTITY_BY_ID_QUERY = """
query GetEntity($id: String!) {
  entity(id: $id) {
    id
    name
    tags
    meta { key value }
    updatedAt
  }
}
"""


def _fetch_full_entity(entity_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single full entity (with meta) from Kissinger."""
    data = _kissinger_gql(_ENTITY_BY_ID_QUERY, {"id": entity_id})
    if data:
        return data.get("entity")
    return None


def _extract_linkedin_url_map(entities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build a {linkedin_url -> entity} map for fast lookup.

    Note: entities from the summary list no longer carry ``meta``.  This map
    will be populated lazily via _fetch_full_entity() calls inside
    match_result_to_entity() when a name match is found.
    """
    result: dict[str, dict[str, Any]] = {}
    for entity in entities:
        for m in entity.get("meta", []):
            if m["key"] == "linkedin_url" and m["value"]:
                result[m["value"].rstrip("/").lower()] = entity
    return result


def fetch_all_kissinger_people() -> list[dict[str, Any]]:
    """Fetch all person entities from Kissinger (paginated).

    Returns:
        Flat list of person entity dicts (id, name, tags, meta).
    """
    people: list[dict[str, Any]] = []
    after: Optional[str] = None
    page = 0

    while True:
        page += 1
        variables: dict[str, Any] = {"kind": "person", "first": 500}
        if after:
            variables["after"] = after

        data = _kissinger_gql(_ENTITY_BY_META_QUERY, variables)
        if not data:
            log.warning("Kissinger fetch failed on page %d", page)
            break

        conn = data.get("entities", {})
        edges = conn.get("edges", [])
        people.extend(e["node"] for e in edges)
        log.debug("Fetched %d people (page %d)", len(edges), page)

        page_info = conn.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")

    log.info("Fetched %d total people from Kissinger", len(people))
    return people


def match_result_to_entity(
    result: dict[str, Any],
    linkedin_map: dict[str, dict[str, Any]],
    name_map: dict[str, list[dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    """Try to find a Kissinger entity matching a Trigify result.

    Strategy:
    1. Exact match on linkedin_url (via pre-built map or lazy fetch).
    2. Fuzzy match: same first+last name AND same company (case-insensitive).

    Args:
        result:       A Trigify search result dict. Trigify nests author data
                      under an ``author`` sub-dict; flat fields are also checked
                      for backward compatibility.
        linkedin_map: {normalized_linkedin_url -> entity} from Kissinger.
                      May be sparse — populated lazily as full entities are
                      fetched.
        name_map:     {normalized_name -> [entities]} from Kissinger.

    Returns:
        The matched entity dict or None.
    """
    # Normalize Trigify result fields — newer API nests under author/content
    author = result.get("author") or {}
    content = result.get("content") or {}
    linkedin_url = (
        author.get("profile_url")
        or result.get("linkedin_url")
        or result.get("profile_url")
        or ""
    ).rstrip("/").lower()

    # 1. LinkedIn URL match (pre-built map first, lazy-fetch on name candidates)
    if linkedin_url and linkedin_url in linkedin_map:
        return linkedin_map[linkedin_url]

    # 2. Fuzzy name + company match
    result_name = (
        author.get("name")
        or result.get("name")
        or result.get("author_name")
        or ""
    ).strip().lower()
    result_company = (result.get("company") or result.get("author_company") or "").strip().lower()

    if result_name:
        candidates = name_map.get(result_name, [])
        if result_company:
            for entity in candidates:
                # meta is absent on summary entities; fetch full entity lazily
                if not entity.get("meta"):
                    full = _fetch_full_entity(entity["id"])
                    if full:
                        entity.update(full)
                        # Update linkedin_map while we have the full data
                        for m in full.get("meta", []):
                            if m["key"] == "linkedin_url" and m["value"]:
                                linkedin_map[m["value"].rstrip("/").lower()] = entity
                meta_company = ""
                for m in entity.get("meta", []):
                    if m["key"] == "company":
                        meta_company = m["value"].strip().lower()
                        break
                if meta_company and (meta_company in result_company or result_company in meta_company):
                    return entity
        if len(candidates) == 1:
            # Single match on name alone is good enough
            return candidates[0]

    # 3. LinkedIn URL match via freshly-fetched map (populated during step 2)
    if linkedin_url and linkedin_url in linkedin_map:
        return linkedin_map[linkedin_url]

    return None


_CREATE_ENTITY_MUTATION = """
mutation CreatePerson($input: CreateEntityInput!) {
  createEntity(input: $input) {
    id
    name
    tags
    meta { key value }
  }
}
"""

_UPDATE_ENTITY_MUTATION = """
mutation UpdateEntity($id: String!, $input: UpdateEntityInput!) {
  updateEntity(id: $id, input: $input) {
    id
    name
    tags
    meta { key value }
  }
}
"""

_LOG_CONTACT_EVENT_MUTATION = """
mutation LogTrigifySignal($input: CreateContactEventInput!) {
  createContactEvent(input: $input) {
    id
    entityId
    eventType
    summary
    occurredAt
  }
}
"""


def log_trigify_signal(
    entity_id: str,
    keyword: str,
    post_text: str,
    occurred_at: str,
) -> bool:
    """Write a trigify_signal contact event to Kissinger.

    Args:
        entity_id:   Kissinger entity ID.
        keyword:     The search keyword that surfaced this post.
        post_text:   Excerpt from the LinkedIn post.
        occurred_at: ISO datetime string of when the post was made.

    Returns:
        True if the event was written successfully.
    """
    excerpt = post_text[:200] + "…" if len(post_text) > 200 else post_text
    summary = f'Posted about "{keyword}": {excerpt}'

    data = _kissinger_gql(
        _LOG_CONTACT_EVENT_MUTATION,
        {
            "input": {
                "entityId": entity_id,
                "eventType": "NOTE",   # Kissinger enum — NOTE is the catch-all
                "summary": summary,
                "occurredAt": occurred_at,
                "createdBy": "trigify-sync",
            }
        },
    )
    return bool(data and data.get("createContactEvent"))


def update_entity_tags_and_meta(
    entity_id: str,
    current_tags: list[str],
    add_tags: list[str],
    meta_updates: list[dict[str, str]],
) -> bool:
    """Add tags and merge-update meta keys on an entity.

    Fetches the entity's existing meta first and merges: keys in ``meta_updates``
    are set (overwriting any previous value), all other existing keys are preserved.
    This prevents promotion / signal writes from wiping pre-existing fields like
    title, company, linkedin_url, last_signal_date, etc.

    Args:
        entity_id:    Kissinger entity ID.
        current_tags: The entity's existing tag list.
        add_tags:     Tags to add (duplicates ignored).
        meta_updates: List of {key, value} dicts to set (merged, not replaced).

    Returns:
        True on success.
    """
    # Fetch current meta to preserve existing keys (title, company, linkedin_url, etc.)
    existing_meta: list[dict[str, str]] = []
    full = _fetch_full_entity(entity_id)
    if full:
        existing_meta = full.get("meta", [])

    # Merge: start with existing, overwrite keys present in meta_updates
    update_keys = {m["key"] for m in meta_updates}
    kept = [m for m in existing_meta if m["key"] not in update_keys]
    merged_meta = kept + list(meta_updates)

    merged_tags = list(dict.fromkeys(current_tags + [t for t in add_tags if t not in current_tags]))
    data = _kissinger_gql(
        _UPDATE_ENTITY_MUTATION,
        {
            "id": entity_id,
            "input": {
                "tags": merged_tags,
                "meta": merged_meta,
            },
        },
    )
    return bool(data and data.get("updateEntity"))


def create_prospect_entity(result: dict[str, Any]) -> Optional[str]:
    """Create a new Kissinger person entity from a Trigify result.

    Args:
        result: A Trigify search result dict with author data.

    Returns:
        The new entity ID, or None on failure.
    """
    # Trigify API nests author data under an "author" sub-dict
    author = result.get("author") or {}
    name = (author.get("name") or result.get("name") or result.get("author_name") or "Unknown").strip()
    title = author.get("title") or result.get("title") or result.get("author_title") or ""
    company = author.get("company") or result.get("company") or result.get("author_company") or ""
    linkedin_url = author.get("profile_url") or result.get("linkedin_url") or result.get("profile_url") or ""

    meta: list[dict[str, str]] = []
    if title:
        meta.append({"key": "title", "value": title})
    if company:
        meta.append({"key": "company", "value": company})
    if linkedin_url:
        meta.append({"key": "linkedin_url", "value": linkedin_url})

    data = _kissinger_gql(
        _CREATE_ENTITY_MUTATION,
        {
            "input": {
                "kind": "person",
                "name": name,
                "tags": ["trigify-discovered", "prospect"],
                "meta": meta,
            }
        },
    )
    if data and data.get("createEntity"):
        return data["createEntity"]["id"]
    return None


# ===========================================================================
# Phase 2: Warm intro path surfacing
# ===========================================================================

_INTRO_PATH_QUERY = """
query IntroPath($targetPersonId: String!, $sourcePersonIds: [String!]!, $maxHops: Int) {
  introPath(targetPersonId: $targetPersonId, sourcePersonIds: $sourcePersonIds, maxHops: $maxHops) {
    found
    hops
    steps {
      personId
      name
      title
      organization
      relationToNext
    }
  }
}
"""

_TAGGED_ENTITIES_QUERY = """
query SignalEntities($kind: String, $first: Int, $after: String) {
  entities(kind: $kind, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        tags
        updatedAt
      }
    }
  }
}
"""


def fetch_recent_signal_entities(days: int = 7) -> list[dict[str, Any]]:
    """Return person entities tagged 'signal:post-engagement' updated in last N days.

    Args:
        days: Look-back window.

    Returns:
        List of entity dicts.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    result: list[dict[str, Any]] = []
    after: Optional[str] = None

    while True:
        variables: dict[str, Any] = {"kind": "person", "first": 500}
        if after:
            variables["after"] = after

        data = _kissinger_gql(_TAGGED_ENTITIES_QUERY, variables)
        if not data:
            break

        conn = data.get("entities", {})
        for edge in conn.get("edges", []):
            node = edge["node"]
            if "signal:post-engagement" not in node.get("tags", []):
                continue
            updated_str = node.get("updatedAt", "")
            try:
                updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                if updated < cutoff:
                    continue
            except (ValueError, AttributeError):
                pass
            result.append(node)

        page_info = conn.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")

    log.info("Found %d recently-signalled entities", len(result))
    return result


def fetch_intro_paths(
    target_ids: list[str],
    source_ids: list[str],
    max_hops: int = 3,
) -> dict[str, Optional[dict[str, Any]]]:
    """Fetch intro paths from source nodes to each target.

    Args:
        target_ids:  Entity IDs to route to.
        source_ids:  Starting entity IDs (usually the admin's entity + close contacts).
        max_hops:    Maximum BFS depth.

    Returns:
        {target_id -> introPath result dict (or None if not found)}.
    """
    paths: dict[str, Optional[dict[str, Any]]] = {}

    for target_id in target_ids:
        data = _kissinger_gql(
            _INTRO_PATH_QUERY,
            {
                "targetPersonId": target_id,
                "sourcePersonIds": source_ids,
                "maxHops": max_hops,
            },
        )
        if data and data.get("introPath"):
            intro = data["introPath"]
            paths[target_id] = intro if intro.get("found") else None
        else:
            paths[target_id] = None

    return paths


# ===========================================================================
# Queue replenishment
# ===========================================================================

_FETCH_ALL_PEOPLE_WITH_TAGS_QUERY = """
query FetchAllPeople($kind: String, $first: Int, $after: String) {
  entities(kind: $kind, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        tags
        updatedAt
      }
    }
  }
}
"""


def _count_active_queue() -> int:
    """Count prospect-contact entities that are not skipped and not yet sent.

    Returns:
        Count of truly active queue entries.
    """
    # Fetch all prospect-contact tagged entities (summary level, no meta)
    candidates: list[dict[str, Any]] = []
    after: Optional[str] = None
    while True:
        variables: dict[str, Any] = {"kind": "person", "first": 500}
        if after:
            variables["after"] = after
        data = _kissinger_gql(_FETCH_ALL_PEOPLE_WITH_TAGS_QUERY, variables)
        if not data:
            break
        conn = data.get("entities", {})
        for edge in conn.get("edges", []):
            node = edge["node"]
            if "prospect-contact" in node.get("tags", []):
                candidates.append(node)
        page_info = conn.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")

    # For each, fetch full entity to check skipped/sent meta
    active = 0
    for entity in candidates:
        full = _fetch_full_entity(entity["id"])
        if not full:
            continue
        meta = {m["key"]: m["value"] for m in full.get("meta", [])}
        if meta.get("outreach_skipped") == "true":
            continue
        if meta.get("outreach_sent_at"):
            continue
        active += 1
    return active


def _fetch_replenishment_candidates(limit: int) -> list[dict[str, Any]]:
    """Fetch trigify-discovered prospects eligible for queue promotion.

    Eligibility:
    - Tagged ``trigify-discovered`` and ``prospect``
    - NOT tagged ``prospect-contact``
    - NOT meta ``outreach_skipped = true``
    - NOT excluded title (COO etc.)

    Sorted by: entities with ``last_signal_date`` first (warm signals),
    then by recency of ``last_signal_date`` descending.

    Args:
        limit: Maximum number of candidates to return.

    Returns:
        List of full entity dicts, ready to promote.
    """
    # Collect summary-level candidates
    summary_pool: list[dict[str, Any]] = []
    after: Optional[str] = None
    while True:
        variables: dict[str, Any] = {"kind": "person", "first": 500}
        if after:
            variables["after"] = after
        data = _kissinger_gql(_FETCH_ALL_PEOPLE_WITH_TAGS_QUERY, variables)
        if not data:
            break
        conn = data.get("entities", {})
        for edge in conn.get("edges", []):
            node = edge["node"]
            tags = node.get("tags", [])
            if "trigify-discovered" in tags and "prospect" in tags and "prospect-contact" not in tags:
                summary_pool.append(node)
        page_info = conn.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")

    log.info("Replenishment: %d summary-level candidates found", len(summary_pool))

    # Fetch full entities to filter and score
    eligible: list[tuple[str, dict[str, Any]]] = []  # (last_signal_date_iso_or_empty, entity)
    for summary in summary_pool:
        full = _fetch_full_entity(summary["id"])
        if not full:
            continue
        meta = {m["key"]: m["value"] for m in full.get("meta", [])}

        # Skip if already skipped
        if meta.get("outreach_skipped") == "true":
            continue
        if meta.get("signal_dismissed") == "true":
            continue

        # Skip excluded titles
        title = meta.get("title", "")
        if is_excluded_title(title):
            log.debug("Replenishment: skipping excluded title '%s' (%s)", full.get("name"), title)
            continue

        last_signal = meta.get("last_signal_date", "")
        eligible.append((last_signal, full))

    log.info("Replenishment: %d eligible candidates after filtering", len(eligible))

    # Sort: entities with a last_signal_date come first (warm), then by recency desc
    def _sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        date_str = item[0]
        has_signal = 1 if date_str else 0
        return (-has_signal, "" if not date_str else ("" if date_str > "z" else date_str))

    eligible.sort(key=lambda item: (-int(bool(item[0])), "" if not item[0] else item[0]), reverse=False)
    # Better sort: warm signals first, then by date descending
    eligible.sort(key=lambda item: (0 if item[0] else 1, "" if not item[0] else item[0]), reverse=False)
    # Final deterministic sort: (no_signal_flag ASC, date DESC)
    eligible.sort(key=lambda item: (0 if item[0] else 1, item[0] or ""), reverse=False)
    # Stable: put entries with a date first, sorted newest-first
    with_date = sorted(
        [(d, e) for d, e in eligible if d],
        key=lambda x: x[0],
        reverse=True,
    )
    without_date = [(d, e) for d, e in eligible if not d]
    sorted_eligible = with_date + without_date

    return [entity for _, entity in sorted_eligible[:limit]]


def replenish_outreach_queue(target_size: int = 100) -> dict[str, Any]:
    """Ensure the active outreach queue has at least ``target_size`` contacts.

    Promotes ``trigify-discovered`` prospects to ``prospect-contact`` status
    until the active queue reaches the target.  Applies title exclusion filters
    before promoting.

    Args:
        target_size: Minimum number of active (not skipped, not sent) queue
                     entries to maintain. Defaults to 100.

    Returns:
        dict with keys:
            ``current``       — active queue size before promotion
            ``promoted``      — number of entities promoted this run
            ``available_pool``— remaining eligible candidates not promoted
    """
    log.info("=== Queue replenishment (target=%d) ===", target_size)

    current = _count_active_queue()
    log.info("Active queue size: %d", current)

    deficit = max(0, target_size - current)
    if deficit == 0:
        log.info("Queue already at or above target (%d >= %d)", current, target_size)
        return {"current": current, "promoted": 0, "available_pool": 0}

    log.info("Deficit: %d — fetching candidates to promote", deficit)

    # Fetch a slightly larger pool than needed to account for any race conditions
    fetch_limit = deficit + 20
    candidates = _fetch_replenishment_candidates(fetch_limit)
    available_pool = max(0, len(candidates) - deficit)

    promoted_count = 0
    to_promote = candidates[:deficit]

    for entity in to_promote:
        entity_id = entity["id"]
        current_tags = entity.get("tags", [])
        ok = update_entity_tags_and_meta(
            entity_id=entity_id,
            current_tags=current_tags,
            add_tags=["prospect-contact"],
            meta_updates=[
                {"key": "queue_added_at", "value": datetime.now(tz=timezone.utc).isoformat()},
                {"key": "queue_added_by", "value": "auto-replenishment"},
            ],
        )
        if ok:
            promoted_count += 1
            log.info(
                "Promoted %s (%s) -> prospect-contact",
                entity.get("name", entity_id),
                entity_id,
            )
        else:
            log.warning("Failed to promote entity %s", entity_id)

    log.info(
        "Queue replenishment complete: promoted %d, pool has ~%d remaining",
        promoted_count,
        available_pool,
    )
    return {
        "current": current,
        "promoted": promoted_count,
        "available_pool": available_pool,
    }


# ===========================================================================
# Main sync logic
# ===========================================================================

def _is_target_sector(result: dict[str, Any]) -> bool:
    """Return True if a Trigify result appears to be from a target sector."""
    author = result.get("author") or {}
    content = result.get("content") or {}
    content_text = content.get("text", "") if isinstance(content, dict) else (content or "")
    haystack = " ".join([
        author.get("title", ""),
        author.get("company", ""),
        result.get("title", ""),
        result.get("author_title", ""),
        result.get("company", ""),
        result.get("author_company", ""),
        content_text,
        result.get("post_text", ""),
        result.get("text", ""),
        result.get("snippet", ""),
    ]).lower()
    return any(kw in haystack for kw in TARGET_SECTOR_KEYWORDS)


def _result_post_text(result: dict[str, Any]) -> str:
    """Extract the post text from a Trigify result (field name varies by API version)."""
    # Newer Trigify API nests content under a "content" sub-dict
    content = result.get("content")
    if isinstance(content, dict):
        text = content.get("text") or content.get("snippet") or ""
        if text:
            return text
    return (
        result.get("post_text")
        or result.get("text")
        or (content if isinstance(content, str) else "")
        or result.get("snippet")
        or ""
    )


def _result_occurred_at(result: dict[str, Any]) -> str:
    """Extract post datetime from a Trigify result, defaulting to now."""
    raw = (
        result.get("published_at")   # primary field in current Trigify API
        or result.get("posted_at")
        or result.get("created_at")
        or result.get("timestamp")
        or ""
    )
    if raw:
        return raw
    return datetime.now(tz=timezone.utc).isoformat()


def _build_name_map(people: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build a {normalized_full_name -> [entities]} lookup."""
    result: dict[str, list[dict[str, Any]]] = {}
    for entity in people:
        key = entity.get("name", "").strip().lower()
        if key:
            result.setdefault(key, []).append(entity)
    return result


_SEARCH_QUERY = """
query SearchEntities($query: String!) {
  search(query: $query) {
    ... on EntitySearchHitGql {
      id
      name
      kind
      score
    }
  }
}
"""


def _get_source_entity_ids() -> list[str]:
    """Resolve the admin's Kissinger entity IDs to use as BFS sources.

    Uses full-text search on the admin name, then fetches each candidate's
    full entity to check the email meta field.  Fallback: returns [] if the
    admin entity cannot be found.

    Note: freshestClaim is broken in the current Kissinger DB schema
    (missing prov_claim.discovered_in_run_id column) so we avoid it here.
    """
    email = os.environ.get("ADMIN_EMAIL", "admin@eloso.ai")  # noname
    # Derive a search name from email (e.g. "admin@eloso.ai" -> "admin")
    admin_name = email.split("@")[0].lower()

    search_data = _kissinger_gql(_SEARCH_QUERY, {"query": admin_name})
    if not search_data:
        return []

    result_ids: list[str] = []
    for hit in search_data.get("search", []):
        if not hit or hit.get("kind") != "person":
            continue
        full = _fetch_full_entity(hit["id"])
        if not full:
            continue
        for m in full.get("meta", []):
            if m["key"] == "email" and email.lower() in (m["value"] or "").lower():
                result_ids.append(hit["id"])
                break
    return result_ids


def run_sync() -> dict[str, Any]:
    """Execute the full Trigify daily sync.

    Returns:
        Summary dict with stats and the Telegram digest text.
    """
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    log.info("=== Trigify Daily Sync — %s ===", today_str)

    # ------------------------------------------------------------------
    # Step 1: Ensure token is valid
    # ------------------------------------------------------------------
    access_token = ensure_valid_token()
    log.info("Token valid — proceeding.")

    # ------------------------------------------------------------------
    # Step 2: List / create searches
    # ------------------------------------------------------------------
    searches = trigify_list_searches(access_token)
    log.info("Found %d existing searches", len(searches))
    searches = ensure_default_searches(access_token, searches)

    # ------------------------------------------------------------------
    # Step 3: Fetch last 24h results from all searches
    # ------------------------------------------------------------------
    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    all_results: list[dict[str, Any]] = []
    results_by_keyword: dict[str, list[dict[str, Any]]] = {}

    for search in searches:
        search_id = str(search.get("id", ""))
        keyword = search.get("name") or search.get("query") or search_id
        if not search_id:
            continue

        results = trigify_get_results(access_token, search_id, since=since)
        log.info("Search '%s' returned %d results", keyword, len(results))

        for r in results:
            r["_keyword"] = keyword  # annotate with source keyword
        results_by_keyword[keyword] = results
        all_results.extend(results)

    log.info("Total raw results: %d", len(all_results))

    # ------------------------------------------------------------------
    # Step 4: Load all Kissinger people (for matching)
    # ------------------------------------------------------------------
    all_people = fetch_all_kissinger_people()
    linkedin_map = _extract_linkedin_url_map(all_people)
    name_map = _build_name_map(all_people)

    # ------------------------------------------------------------------
    # Step 5 & 6: Match results and write signals
    # ------------------------------------------------------------------
    warm_signals: list[dict[str, Any]] = []        # matched + written
    new_prospects_created: list[dict[str, Any]] = []  # newly created entities
    signal_entity_ids: list[str] = []

    now_iso = datetime.now(tz=timezone.utc).isoformat()

    for result in all_results:
        keyword = result.get("_keyword", "unknown")
        post_text = _result_post_text(result)
        occurred_at = _result_occurred_at(result)
        # Trigify API nests author data under an "author" sub-dict
        author = result.get("author") or {}
        content = result.get("content") or {}
        person_name = (
            author.get("name")
            or result.get("name")
            or result.get("author_name")
            or "Unknown"
        ).strip()
        title = author.get("title") or result.get("title") or result.get("author_title") or ""
        company = author.get("company") or result.get("company") or result.get("author_company") or ""

        # Extract post URL from wherever Trigify stores it
        post_url = (
            (content.get("url") or content.get("post_url") or content.get("permalink") if isinstance(content, dict) else "")
            or result.get("url")
            or result.get("post_url")
            or result.get("permalink")
            or result.get("post_permalink")
            or ""
        )

        entity = match_result_to_entity(result, linkedin_map, name_map)

        if entity:
            entity_id = entity["id"]
            log.info("Matched '%s' -> Kissinger entity %s", person_name, entity_id)

            # Write contact event
            written = log_trigify_signal(entity_id, keyword, post_text, occurred_at)
            if written:
                # Tag and update last_signal_date + last_signal_keyword + last_signal_url
                meta_updates = [
                    {"key": "last_signal_date", "value": now_iso},
                    {"key": "last_signal_keyword", "value": keyword},
                ]
                if post_url:
                    meta_updates.append({"key": "last_signal_url", "value": post_url})
                update_entity_tags_and_meta(
                    entity_id,
                    current_tags=entity.get("tags", []),
                    add_tags=["signal:post-engagement"],
                    meta_updates=meta_updates,
                )
                signal_entity_ids.append(entity_id)
                warm_signals.append({
                    "name": entity.get("name", person_name),
                    "title": title,
                    "company": company,
                    "keyword": keyword,
                    "post_excerpt": post_text[:120],
                    "entity_id": entity_id,
                    "in_graph": True,
                })
        else:
            # Phase 1 Step 7: create new prospect for target-sector unmatched results
            if _is_target_sector(result):
                # Permanently skip COO / Chief Operating Officer titles
                if is_excluded_title(title):
                    log.info("Skipping COO/excluded title prospect: '%s' (%s)", person_name, title)
                    continue
                log.info("Creating new prospect for unmatched result: '%s'", person_name)
                new_id = create_prospect_entity(result)
                if new_id:
                    # Log the signal event on the new entity too
                    log_trigify_signal(new_id, keyword, post_text, occurred_at)
                    new_meta_updates = [
                        {"key": "last_signal_date", "value": now_iso},
                        {"key": "last_signal_keyword", "value": keyword},
                    ]
                    if post_url:
                        new_meta_updates.append({"key": "last_signal_url", "value": post_url})
                    update_entity_tags_and_meta(
                        new_id,
                        current_tags=["trigify-discovered", "prospect"],
                        add_tags=["signal:post-engagement"],
                        meta_updates=new_meta_updates,
                    )
                    new_prospects_created.append({
                        "name": person_name,
                        "title": title,
                        "company": company,
                        "keyword": keyword,
                        "entity_id": new_id,
                    })

    log.info(
        "Signals written: %d warm matches, %d new prospects",
        len(warm_signals),
        len(new_prospects_created),
    )

    # ------------------------------------------------------------------
    # Phase 2: Warm intro paths for recently-signalled entities
    # ------------------------------------------------------------------
    intro_path_lines: list[str] = []

    signal_entities = fetch_recent_signal_entities(days=7)
    if signal_entities:
        source_ids = _get_source_entity_ids()
        if not source_ids:
            log.info("No source entity IDs found — skipping intro path BFS")
        else:
            target_ids = [e["id"] for e in signal_entities]
            log.info(
                "Running intro path BFS: %d targets, %d sources",
                len(target_ids),
                len(source_ids),
            )
            paths = fetch_intro_paths(target_ids, source_ids)

            # Build a name lookup for signal entities
            id_to_entity = {e["id"]: e for e in signal_entities}

            for target_id, path in paths.items():
                if not path or not path.get("found"):
                    continue
                entity = id_to_entity.get(target_id, {})
                entity_name = entity.get("name", target_id)
                entity_company = ""
                # meta may be absent on summary entities; try lazy full fetch
                if not entity.get("meta"):
                    full = _fetch_full_entity(entity["id"])
                    if full:
                        entity.update(full)
                for m in entity.get("meta", []):
                    if m["key"] == "company":
                        entity_company = m["value"]
                        break

                steps = path.get("steps", [])
                step_names = [s.get("name", "?") for s in steps]
                path_str = " -> ".join(step_names) if step_names else "direct"

                company_str = f" ({entity_company})" if entity_company else ""
                intro_path_lines.append(
                    f"- Path to {entity_name}{company_str}: {path_str}"
                )
                log.info("Found intro path to %s: %s", entity_name, path_str)

    # ------------------------------------------------------------------
    # Step 8: Build Telegram digest
    # ------------------------------------------------------------------
    digest_lines: list[str] = [f"Trigify Daily Signal — {today_str}", ""]

    if warm_signals:
        digest_lines.append(f"{len(warm_signals)} warm signal{'s' if len(warm_signals) != 1 else ''} today:")
        for sig in warm_signals:
            in_graph = "Yes" if sig["in_graph"] else "New"
            title_str = f" ({sig['title']} at {sig['company']})" if sig.get("title") or sig.get("company") else ""
            digest_lines.append(
                f"- {sig['name']}{title_str} posted about \"{sig['keyword']}\"\n"
                f"  In graph: {in_graph}"
            )
    else:
        digest_lines.append("No warm signals matched today.")

    digest_lines.append("")

    if new_prospects_created:
        digest_lines.append(f"{len(new_prospects_created)} new prospect{'s' if len(new_prospects_created) != 1 else ''} discovered:")
        for p in new_prospects_created:
            company_str = f" @ {p['company']}" if p.get("company") else ""
            digest_lines.append(f"- {p['name']}{company_str} (via \"{p['keyword']}\")")
        digest_lines.append("")

    if intro_path_lines:
        digest_lines.append("Warm intro paths:")
        digest_lines.extend(intro_path_lines)
        digest_lines.append("")

    # ------------------------------------------------------------------
    # Step 9: Queue replenishment — ensure >= 100 active contacts
    # ------------------------------------------------------------------
    replenish_result = replenish_outreach_queue(target_size=100)
    log.info(
        "Queue replenishment: %d active, promoted %d, pool has %d remaining",
        replenish_result["current"],
        replenish_result["promoted"],
        replenish_result["available_pool"],
    )

    # Add replenishment line to digest
    digest_lines.append("")
    if replenish_result["promoted"] > 0:
        digest_lines.append(
            f"Queue: {replenish_result['current']} active before sync, "
            f"promoted {replenish_result['promoted']} new contacts "
            f"({replenish_result['available_pool']} remaining in pool)."
        )
    else:
        digest_lines.append(
            f"Queue: {replenish_result['current']} active contacts — at or above target (no promotion needed)."
        )

    digest = "\n".join(digest_lines).strip()

    summary = {
        "date": today_str,
        "total_results": len(all_results),
        "warm_signals": len(warm_signals),
        "new_prospects": len(new_prospects_created),
        "intro_paths_found": len(intro_path_lines),
        "queue_before": replenish_result["current"],
        "queue_promoted": replenish_result["promoted"],
        "queue_pool_remaining": replenish_result["available_pool"],
        "digest": digest,
    }

    log.info("Sync complete: %s", summary)
    return summary


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Trigify daily sync — fetch signals, update Kissinger, send digest."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and match but do not write to Kissinger or send digest.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON summary to this file path.",
    )
    args = parser.parse_args()

    if args.dry_run:
        log.info("DRY RUN mode — no writes will be made")
        # Token check only
        ensure_valid_token()
        log.info("Token OK. Exiting dry run (no fetch in dry mode).")
        return

    summary = run_sync()

    print()
    print(summary["digest"])
    print()
    print(f"Stats: {summary['warm_signals']} signals, {summary['new_prospects']} new prospects, "
          f"{summary['intro_paths_found']} intro paths")

    if args.output:
        Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info("Summary written to %s", args.output)


if __name__ == "__main__":
    main()
