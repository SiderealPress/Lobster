"""
BIS-298: Deduplicate Against Existing CRM

Given a list of candidate contacts [{name, title, company, source_url}],
checks each against the Kissinger CRM using:
  1. Full-text search() — finds near-name matches across all entities
  2. contactsAtOrg() — finds persons already connected to the org entity

Returns a classification:
  {
    "new": [...],         # Not found in CRM — safe to add
    "duplicates": [...],  # Exact or very close name match — skip
    "fuzzy_matches": [...] # Possible match — review before adding
  }

Each entry in "duplicates" and "fuzzy_matches" is augmented with a
"crm_match" field containing the matched Kissinger entity summary.

Fuzzy matching: normalises names (strip accents, lowercase, collapse spaces),
then uses SequenceMatcher similarity. Thresholds:
  >= 0.92  -> duplicate
  >= 0.72  -> fuzzy_match
  <  0.72  -> new

Usage:
    python dedup_crm_contacts.py --contacts '[{"name":"Jane Smith",...}]' \\
        [--org-id <kissinger-org-id>]

Exits 0 and prints JSON on success.
Exits 1 on hard errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import re
from difflib import SequenceMatcher
from typing import Any

import requests

KISSINGER_ENDPOINT = os.environ.get(
    "KISSINGER_ENDPOINT", "http://localhost:8080/graphql"
)
KISSINGER_API_TOKEN = os.environ.get("KISSINGER_API_TOKEN", "")

_DUPLICATE_THRESHOLD = 0.92
_FUZZY_THRESHOLD = 0.72

_SEARCH_QUERY = """
query SearchEntities($q: String!, $limit: Int) {
  search(query: $q, limit: $limit) {
    id
    kind
    name
    score
  }
}
"""

_CONTACTS_AT_ORG_QUERY = """
query ContactsAtOrg($orgId: String!, $first: Int) {
  contactsAtOrg(orgId: $orgId, first: $first) {
    edges {
      node {
        entityId
        entityName
        relation
      }
    }
  }
}
"""


def _graphql(
    query: str,
    variables: dict[str, Any],
    endpoint: str = KISSINGER_ENDPOINT,
    token: str = KISSINGER_API_TOKEN,
) -> dict[str, Any]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(
        endpoint,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def _normalize(name: str) -> str:
    """
    Normalise a personal name for fuzzy comparison.

    Steps:
      1. NFKD decompose (strip accents)
      2. Remove non-ASCII
      3. Lowercase
      4. Collapse whitespace
      5. Strip punctuation
    """
    nfd = unicodedata.normalize("NFKD", name)
    ascii_only = nfd.encode("ascii", "ignore").decode("ascii")
    lower = ascii_only.lower()
    no_punct = re.sub(r"[^\w\s]", " ", lower)
    return re.sub(r"\s+", " ", no_punct).strip()


def _similarity(a: str, b: str) -> float:
    """SequenceMatcher similarity on normalised names."""
    na, nb = _normalize(a), _normalize(b)
    return SequenceMatcher(None, na, nb).ratio()


def _search_crm(
    name: str, endpoint: str, token: str
) -> list[dict[str, Any]]:
    """Full-text search Kissinger for a name; return person-kind hits only."""
    try:
        data = _graphql(
            _SEARCH_QUERY,
            {"q": name, "limit": 10},
            endpoint,
            token,
        )
        return [
            h for h in data.get("search", []) if h.get("kind") == "person"
        ]
    except Exception:  # noqa: BLE001
        return []


def _contacts_at_org(
    org_id: str, endpoint: str, token: str
) -> list[dict[str, Any]]:
    """Fetch contacts already linked to an org in Kissinger."""
    try:
        data = _graphql(
            _CONTACTS_AT_ORG_QUERY,
            {"orgId": org_id, "first": 200},
            endpoint,
            token,
        )
        return [
            edge["node"]
            for edge in data.get("contactsAtOrg", {}).get("edges", [])
        ]
    except Exception:  # noqa: BLE001
        return []


def dedup_crm_contacts(
    candidates: list[dict[str, Any]],
    org_id: str | None = None,
    endpoint: str = KISSINGER_ENDPOINT,
    token: str = KISSINGER_API_TOKEN,
) -> dict[str, list[dict[str, Any]]]:
    """
    Deduplicate ``candidates`` against Kissinger CRM.

    Args:
        candidates: List of {name, title, company, source_url, ...}
        org_id: Kissinger entity ID of the org (optional; enables contactsAtOrg check)
        endpoint: GraphQL endpoint
        token: Bearer token

    Returns:
        {"new": [...], "duplicates": [...], "fuzzy_matches": [...]}
    """
    # Pre-fetch all contacts at org (if org_id provided)
    org_contacts: list[dict[str, Any]] = []
    if org_id:
        org_contacts = _contacts_at_org(org_id, endpoint, token)

    result: dict[str, list[dict[str, Any]]] = {
        "new": [],
        "duplicates": [],
        "fuzzy_matches": [],
    }

    for candidate in candidates:
        name = candidate.get("name", "").strip()
        if not name:
            # No name — treat as new (enrichment will note this)
            result["new"].append(candidate)
            continue

        best_score = 0.0
        best_match: dict[str, Any] | None = None

        # --- Check contactsAtOrg first (fastest path) ---
        for oc in org_contacts:
            crm_name = oc.get("entityName", "")
            score = _similarity(name, crm_name)
            if score > best_score:
                best_score = score
                best_match = {
                    "id": oc.get("entityId", ""),
                    "name": crm_name,
                    "kind": "person",
                    "source": "contactsAtOrg",
                }

        # --- Full-text search (broader net) ---
        search_hits = _search_crm(name, endpoint, token)
        for hit in search_hits:
            score = _similarity(name, hit.get("name", ""))
            if score > best_score:
                best_score = score
                best_match = {
                    "id": hit.get("id", ""),
                    "name": hit.get("name", ""),
                    "kind": hit.get("kind", ""),
                    "source": "search",
                }

        # --- Classify ---
        enriched = dict(candidate)
        if best_score >= _DUPLICATE_THRESHOLD and best_match:
            enriched["crm_match"] = best_match
            enriched["similarity"] = round(best_score, 3)
            result["duplicates"].append(enriched)
        elif best_score >= _FUZZY_THRESHOLD and best_match:
            enriched["crm_match"] = best_match
            enriched["similarity"] = round(best_score, 3)
            result["fuzzy_matches"].append(enriched)
        else:
            result["new"].append(enriched)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate candidate contacts against Kissinger CRM"
    )
    parser.add_argument(
        "--contacts",
        required=True,
        help="JSON array of contact dicts",
    )
    parser.add_argument(
        "--org-id",
        default=None,
        help="Kissinger org entity ID (enables contactsAtOrg check)",
    )
    parser.add_argument("--endpoint", default=KISSINGER_ENDPOINT)
    parser.add_argument("--token", default=KISSINGER_API_TOKEN)
    args = parser.parse_args()

    try:
        candidates = json.loads(args.contacts)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid contacts JSON — {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        output = dedup_crm_contacts(
            candidates,
            org_id=args.org_id,
            endpoint=args.endpoint,
            token=args.token,
        )
        print(json.dumps(output, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
