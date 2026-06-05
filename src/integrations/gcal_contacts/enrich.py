"""
Wave 1d (skeleton): Google Calendar → Kissinger knows-graph enrichment.

Meeting attendees are strong social signals: if the owner shared a calendar
event with someone, there is likely a verified real-time communication channel.
Meeting attendees → Knows edges (strength=0.9, inferred=false).

Shares OAuth credentials with the Google Calendar integration already in
Lobster (integrations.google_calendar.oauth / token_store). No new OAuth
flow is needed — the same GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET and the
same per-user token in ~/messages/config/gcal-tokens/{user_id}.json are used.

CURRENT STATUS: Skeleton — reads events and attendees from the Google Calendar
API but edge creation is stubbed out pending:
1. OAuth token for the user being present (authenticate via 'connect my Google
   Calendar' in Lobster chat)
2. The owner_entity_id being resolved (find via `kissinger entity search Owner Name`)

Usage:
    from integrations.gcal_contacts.enrich import enrich_from_calendar
    result = enrich_from_calendar(
        user_id="6645894734",
        owner_entity_id="abc123...",
        since_days=90,
        dry_run=True,
    )

CLI:
    python3 -m lobster.integrations.gcal_contacts.enrich \\
        --user-id USER_ID \\
        --owner-entity-id ENTITY_ID \\
        [--since-days 90] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_DB = Path.home() / ".kissinger" / "graph.db"

# Attendees = higher confidence than email (you deliberately met)
KNOWS_STRENGTH = 0.9
_GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"
_HTTP_TIMEOUT = 15


@dataclass
class GcalEnrichResult:
    """Result of a Google Calendar enrichment run."""
    events_scanned: int = 0
    attendees_found: int = 0
    attendees_matched: int = 0
    knows_created: int = 0
    knows_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _normalize_email(email: str) -> str:
    return email.lower().strip()


def _name_normalize(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


# ---------------------------------------------------------------------------
# Google Calendar API helpers (reuse gcal token from google_calendar integration)
# ---------------------------------------------------------------------------

def _get_valid_gcal_token(user_id: str) -> Optional[Any]:
    """Get a valid Google Calendar OAuth token for the user."""
    try:
        from integrations.google_calendar.token_store import get_valid_token
        return get_valid_token(user_id)
    except Exception as e:
        log.error("Failed to load gcal token: %s", e)
        return None


def _call_gcal_api(method: str, url: str, token: str, **kwargs: Any) -> Any:
    """Make an authenticated call to the Google Calendar API."""
    import requests
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    kwargs.setdefault("timeout", _HTTP_TIMEOUT)
    response = requests.request(method, url, headers=headers, **kwargs)
    if not response.ok:
        raise RuntimeError(f"Calendar API error {response.status_code}")
    return response.json()


def _fetch_events_with_attendees(
    user_id: str,
    since_days: int,
) -> list[dict]:
    """Fetch calendar events with attendee lists via the Google Calendar API.

    Uses the same OAuth token as the main calendar integration.
    Returns raw event dicts from the API (attendees field included).
    """
    token = _get_valid_gcal_token(user_id)
    if token is None:
        log.info("No valid gcal token for user_id=%r — is Google Calendar connected?", user_id)
        return []

    since_dt = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=since_days)
    )
    time_min = since_dt.isoformat()

    events_url = f"{_GCAL_API_BASE}/calendars/primary/events"
    params = {
        "timeMin": time_min,
        "maxResults": 500,
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    all_events = []
    page_token = None

    while True:
        if page_token:
            params["pageToken"] = page_token

        try:
            data = _call_gcal_api("GET", events_url, token.access_token, params=params)
        except RuntimeError as e:
            log.warning("gcal_contacts: API call failed: %s", e)
            break

        for event in data.get("items", []):
            # Only include events with attendees (i.e. shared meetings)
            if event.get("attendees"):
                all_events.append(event)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    log.info("gcal_contacts: fetched %d events with attendees for user_id=%r", len(all_events), user_id)
    return all_events


def _extract_attendees(events: list[dict], owner_email: Optional[str] = None) -> list[dict]:
    """Extract unique attendee contacts from a list of calendar events.

    Returns list of {"name": str, "email": str} dicts, excluding the owner.
    """
    seen: dict[str, dict] = {}  # email → contact

    for event in events:
        for attendee in event.get("attendees", []):
            email = _normalize_email(attendee.get("email") or "")
            if not email:
                continue
            # Skip the owner themselves
            if owner_email and email == _normalize_email(owner_email):
                continue
            # Skip calendar resource rooms (Google's meeting room accounts)
            if attendee.get("resource", False):
                continue

            if email not in seen:
                seen[email] = {
                    "name": attendee.get("displayName") or "",
                    "email": email,
                }

    return list(seen.values())


# ---------------------------------------------------------------------------
# Kissinger helpers
# ---------------------------------------------------------------------------

def _load_kissinger_entities(db_path: Path) -> list[dict]:
    """Load all person entities from Kissinger."""
    import pycozo
    db = pycozo.client.Client("sqlite", str(db_path), dataframe=False)
    try:
        result = db.run(
            "?[id, name, meta] := *entity{id, kind, name, meta}, kind == \"person\"",
            {},
        )
        entities = []
        for row in result["rows"]:
            entity_id, name, meta_raw = row
            meta = (meta_raw if isinstance(meta_raw, dict)
                    else (json.loads(meta_raw) if isinstance(meta_raw, str) else {}))
            entities.append({
                "id": entity_id,
                "name": name,
                "meta": meta,
                "email": _normalize_email(meta.get("email") or ""),
                "name_norm": _name_normalize(name),
            })
        return entities
    finally:
        db.close()


def _match_attendee(
    attendee: dict,
    entities: list[dict],
    email_index: dict[str, dict],
    name_index: dict[str, dict],
) -> Optional[dict]:
    """Match a calendar attendee to a Kissinger entity."""
    email = attendee.get("email", "")
    if email and email in email_index:
        return email_index[email]

    name_norm = _name_normalize(attendee.get("name") or "")
    if name_norm and name_norm in name_index:
        return name_index[name_norm]

    # Fuzzy name
    if name_norm:
        tokens = set(name_norm.split())
        best_score = 0.0
        best = None
        for e in entities:
            e_tokens = set(e["name_norm"].split())
            if not tokens or not e_tokens:
                continue
            overlap = len(tokens & e_tokens) / max(len(tokens), len(e_tokens))
            if overlap > best_score:
                best_score = overlap
                best = e
        if best_score >= 0.8 and best is not None:
            return best

    return None


def _edge_exists(db, owner_id: str, target_id: str) -> bool:
    """Check if a knows edge already exists."""
    try:
        result = db.run(
            "?[s, t] := *edge{source: s, target: t, relation: r}, "
            "r == \"knows\", "
            "(s == $owner and t == $target) or (s == $target and t == $owner)",
            {"owner": owner_id, "target": target_id},
        )
        return len(result["rows"]) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich_from_calendar(
    user_id: str,
    owner_entity_id: str,
    owner_email: Optional[str] = None,
    since_days: int = 90,
    db_path: Path = DEFAULT_DB,
    dry_run: bool = False,
) -> GcalEnrichResult:
    """Enrich Kissinger knows-graph from Google Calendar meeting attendees.

    Uses the same OAuth token as the Google Calendar integration — no new
    OAuth setup needed once the user has authenticated.

    Args:
        user_id:           Lobster user ID (Telegram chat_id as str).
        owner_entity_id:   Kissinger entity ID for the calendar owner.
        owner_email:       Owner's email to exclude self from attendee list.
        since_days:        Days of calendar history to scan (default: 90).
        db_path:           Path to Kissinger graph.db.
        dry_run:           If True, report without writing.

    Returns:
        GcalEnrichResult with counts.

    NOTE: This is currently a skeleton. Edge creation logic is implemented
    but requires:
    1. Google Calendar OAuth token for the user
    2. owner_entity_id resolved to the correct Kissinger entity
    """
    result = GcalEnrichResult()

    # --- Fetch calendar events ---
    events = _fetch_events_with_attendees(user_id, since_days)
    result.events_scanned = len(events)

    if not events:
        log.info("gcal_contacts: no events found — calendar not connected or no shared meetings")
        return result

    # --- Extract unique attendees ---
    attendees = _extract_attendees(events, owner_email=owner_email)
    result.attendees_found = len(attendees)
    log.info("gcal_contacts: %d unique attendees across %d events", len(attendees), len(events))

    # --- Load Kissinger entities ---
    try:
        entities = _load_kissinger_entities(db_path)
    except Exception as e:
        result.errors.append(f"Kissinger load failed: {e}")
        return result

    email_index = {e["email"]: e for e in entities if e["email"]}
    name_index = {e["name_norm"]: e for e in entities if e["name_norm"]}

    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    import pycozo
    db = None if dry_run else pycozo.client.Client("sqlite", str(db_path), dataframe=False)

    try:
        for attendee in attendees:
            matched = _match_attendee(attendee, entities, email_index, name_index)
            if not matched:
                continue

            result.attendees_matched += 1
            target_id = matched["id"]

            if owner_entity_id == target_id:
                continue

            if dry_run:
                result.knows_created += 1
                continue

            if _edge_exists(db, owner_entity_id, target_id):
                result.knows_skipped += 1
                continue

            # Create Knows edge
            try:
                db.run(
                    "?[source, target, relation, value_frame, strength, notes, inferred, how_they_know, created_at, updated_at] <- "
                    "[[$source, $target, $relation, $vf, $st, $notes, $inf, $how, $ca, $ua]] "
                    ":put edge {source, target, relation => value_frame, strength, notes, inferred, how_they_know, created_at, updated_at}",
                    {
                        "source": owner_entity_id,
                        "target": target_id,
                        "relation": "knows",
                        "vf": "",
                        "st": KNOWS_STRENGTH,
                        "notes": "",
                        "inf": False,
                        "how": "calendar meeting",
                        "ca": now_ts,
                        "ua": now_ts,
                    },
                )
                result.knows_created += 1
            except Exception as e:
                result.errors.append(f"Create edge {owner_entity_id[:8]}->{target_id[:8]}: {e}")

    finally:
        if db is not None:
            db.close()

    return result


def main() -> None:
    """CLI entry point for Google Calendar → Kissinger enrichment."""
    parser = argparse.ArgumentParser(
        description="Enrich Kissinger knows-graph from Google Calendar meeting attendees"
    )
    parser.add_argument("--user-id", required=True, help="Lobster user ID (Telegram chat_id)")
    parser.add_argument("--owner-entity-id", required=True, help="Kissinger entity ID for calendar owner")
    parser.add_argument("--owner-email", default="", help="Owner email to exclude from attendees")
    parser.add_argument("--since-days", type=int, default=90, help="Days of calendar history (default: 90)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"Kissinger graph.db path")
    parser.add_argument("--dry-run", action="store_true", help="Report without making changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    result = enrich_from_calendar(
        user_id=args.user_id,
        owner_entity_id=args.owner_entity_id,
        owner_email=args.owner_email or None,
        since_days=args.since_days,
        db_path=Path(args.db),
        dry_run=args.dry_run,
    )

    print()
    print("=" * 60)
    print("Google Calendar → Kissinger Enrichment Report")
    print(f"  Dry run:                         {args.dry_run}")
    print(f"  Events scanned (with attendees): {result.events_scanned}")
    print(f"  Unique attendees found:          {result.attendees_found}")
    print(f"  Attendees matched to Kissinger:  {result.attendees_matched}")
    print(f"  Knows edges created:             {result.knows_created}")
    print(f"  Knows edges skipped (exists):    {result.knows_skipped}")
    print(f"  Errors:                          {len(result.errors)}")
    if result.errors:
        for e in result.errors[:10]:
            print(f"    {e}")

    if result.events_scanned == 0:
        print()
        print("NOTE: 0 events found. Google Calendar not yet connected?")
        print("To authenticate: send 'connect my Google Calendar' to Lobster.")


if __name__ == "__main__":
    main()
