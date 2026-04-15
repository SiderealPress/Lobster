"""
BIS-299: Add Contacts with Provenance

Takes a list of contacts [{name, title, company, source_url, org_kissinger_id}]
and writes them into Kissinger CRM:

  1. createEntity — kind=person, tags=["supply-chain","prospect-enrichment"],
     meta=[
       {key:"provenance", value:"wallace"},
       {key:"title",      value:<title>},
       {key:"source_url", value:<source_url>},
       {key:"enriched_at", value:<ISO-8601 timestamp>},
     ]
  2. createEdge — source=<new_entity_id>, target=<org_kissinger_id>,
     relation="works_at"

Returns a list of {contact, entity_id, edge_created, error?} results.

Usage:
    python add_contacts_provenance.py \\
        --contacts '[{"name":"Jane Smith","title":"VP Supply Chain",...}]' \\
        [--dry-run]

Exits 0 always (per-contact errors are reported inline, not as exit codes).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import requests

KISSINGER_ENDPOINT = os.environ.get(
    "KISSINGER_ENDPOINT", "http://localhost:8080/graphql"
)
KISSINGER_API_TOKEN = os.environ.get("KISSINGER_API_TOKEN", "")

_CREATE_ENTITY_MUTATION = """
mutation CreateEntity($input: CreateEntityInput!) {
  createEntity(input: $input) {
    id
    name
    kind
    tags
  }
}
"""

_CREATE_EDGE_MUTATION = """
mutation CreateEdge($input: CreateEdgeInput!) {
  createEdge(input: $input) {
    id
    source
    target
    relation
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


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_contacts_provenance(
    contacts: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    endpoint: str = KISSINGER_ENDPOINT,
    token: str = KISSINGER_API_TOKEN,
) -> list[dict[str, Any]]:
    """
    Write contacts into Kissinger with provenance metadata.

    Each contact dict must have:
      - name (str)
      - title (str)
      - company (str)
      - source_url (str)
      - org_kissinger_id (str) — Kissinger ID of the parent org entity

    Args:
        contacts: List of contact dicts (see above).
        dry_run: If True, log what would happen but do not write anything.
        endpoint: GraphQL endpoint.
        token: Bearer token.

    Returns:
        List of result dicts:
          {
            "contact": <original contact dict>,
            "entity_id": <new entity ID or None on dry_run/error>,
            "edge_created": True/False,
            "dry_run": True/False,
            "error": <error string or None>,
          }
    """
    results: list[dict[str, Any]] = []
    enriched_at = _now_iso()

    for contact in contacts:
        name = (contact.get("name") or "").strip()
        title = (contact.get("title") or "").strip()
        source_url = (contact.get("source_url") or "").strip()
        org_id = (contact.get("org_kissinger_id") or "").strip()

        if not name:
            results.append(
                {
                    "contact": contact,
                    "entity_id": None,
                    "edge_created": False,
                    "dry_run": dry_run,
                    "error": "Missing required field: name",
                }
            )
            continue

        meta = [
            {"key": "provenance", "value": "wallace"},
            {"key": "enriched_at", "value": enriched_at},
        ]
        if title:
            meta.append({"key": "title", "value": title})
        if source_url:
            meta.append({"key": "source_url", "value": source_url})

        entity_input = {
            "kind": "person",
            "name": name,
            "tags": ["supply-chain", "prospect-enrichment"],
            "meta": meta,
        }

        if dry_run:
            # Log intended writes without side effects
            print(
                f"[dry-run] Would createEntity: {json.dumps(entity_input)}",
                file=sys.stderr,
            )
            if org_id:
                print(
                    f"[dry-run] Would createEdge: source=<new_id> "
                    f"target={org_id} relation=works_at",
                    file=sys.stderr,
                )
            results.append(
                {
                    "contact": contact,
                    "entity_id": None,
                    "edge_created": False,
                    "dry_run": True,
                    "error": None,
                }
            )
            continue

        # --- Create entity ---
        entity_id: str | None = None
        try:
            data = _graphql(
                _CREATE_ENTITY_MUTATION,
                {"input": entity_input},
                endpoint,
                token,
            )
            entity_id = data["createEntity"]["id"]
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "contact": contact,
                    "entity_id": None,
                    "edge_created": False,
                    "dry_run": False,
                    "error": f"createEntity failed: {exc}",
                }
            )
            continue

        # --- Create edge to org (if org_id provided) ---
        edge_created = False
        edge_error: str | None = None
        if org_id and entity_id:
            edge_input = {
                "source": entity_id,
                "target": org_id,
                "relation": "works_at",
            }
            try:
                _graphql(
                    _CREATE_EDGE_MUTATION,
                    {"input": edge_input},
                    endpoint,
                    token,
                )
                edge_created = True
            except Exception as exc:  # noqa: BLE001
                edge_error = f"createEdge failed: {exc}"

        results.append(
            {
                "contact": contact,
                "entity_id": entity_id,
                "edge_created": edge_created,
                "dry_run": False,
                "error": edge_error,
            }
        )

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add contacts to Kissinger CRM with provenance"
    )
    parser.add_argument(
        "--contacts",
        required=True,
        help="JSON array of contact dicts (must include org_kissinger_id)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen without writing to CRM",
    )
    parser.add_argument("--endpoint", default=KISSINGER_ENDPOINT)
    parser.add_argument("--token", default=KISSINGER_API_TOKEN)
    args = parser.parse_args()

    try:
        contacts = json.loads(args.contacts)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid contacts JSON — {exc}", file=sys.stderr)
        sys.exit(1)

    results = add_contacts_provenance(
        contacts,
        dry_run=args.dry_run,
        endpoint=args.endpoint,
        token=args.token,
    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
