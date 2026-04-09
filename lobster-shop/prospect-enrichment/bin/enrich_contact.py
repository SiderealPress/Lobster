"""
Enrich a single Kissinger contact.

Called by the eloso-bisque API route POST /api/contacts/[id]/enrich.
Fetches the contact's entity from Kissinger, discovers new data from
available sources, and writes enriched fields back with full provenance.

Writes status to ~/lobster-workspace/enrichment-runs/{run_id}.json so the
UI can poll for completion.

Usage:
    python3 enrich_contact.py \\
        --contact-id ent_abc123 \\
        --run-id <uuid> \\
        [--endpoint http://localhost:8080/graphql] \\
        [--token <bearer>] \\
        [--dry-run]

Exit codes:
    0 — completed (possibly with non-fatal errors)
    1 — fatal failure (contact not found, Kissinger unreachable)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

# Sibling imports
_BIN = Path(__file__).parent
sys.path.insert(0, str(_BIN))

from manifest_loader import load_manifest, available_sources_for_goal, now_iso
from pipeline_hygiene import HygieneLayer
from run_manifest import create_run, complete_run, update_run
from find_supply_chain_contacts import find_supply_chain_contacts
from dedup_crm_contacts import dedup_crm_contacts

KISSINGER_ENDPOINT = os.environ.get("KISSINGER_ENDPOINT", "http://localhost:8080/graphql")
KISSINGER_API_TOKEN = os.environ.get("KISSINGER_API_TOKEN", "")

_MANIFEST_PATH = Path(__file__).parent.parent / "sources" / "manifest.json"

_ENTITY_QUERY = """
query GetEntity($id: String!) {
  entity(id: $id) {
    id kind name tags notes archived
    meta { key value }
    createdAt updatedAt
  }
}
"""

_EDGES_FROM_QUERY = """
query EdgesFrom($entityId: String!, $first: Int) {
  edgesFrom(entityId: $entityId, first: $first) {
    edges { node { source target relation strength notes } }
  }
}
"""


def _gql(
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
    if "errors" in payload and payload["errors"]:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def enrich_contact(
    *,
    contact_id: str,
    run_id: str,
    endpoint: str = KISSINGER_ENDPOINT,
    token: str = KISSINGER_API_TOKEN,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Enrich a single contact identified by Kissinger entity ID.

    Flow:
      1. Load source manifest — skip unavailable sources
      2. Fetch entity from Kissinger
      3. Determine goals appropriate for this entity kind
      4. For each available source + goal combination:
         a. Fetch enrichment data from source
         b. Write via HygieneLayer (idempotency + provenance + rollback log)
      5. Write completed run manifest

    Returns the final run manifest dict.
    """
    # --- Create the run manifest immediately (status=running) ---
    manifest_dict = create_run(
        run_id,
        dry_run=dry_run,
        contact_id=contact_id,
        goals=["org_chart", "work_history"],
    )

    # --- Load source manifest ---
    try:
        source_manifest = load_manifest(_MANIFEST_PATH)
    except Exception as exc:  # noqa: BLE001
        err = f"Failed to load source manifest: {exc}"
        print(f"[enrich_contact] FATAL: {err}", file=sys.stderr)
        return complete_run(run_id, status="failed", errors=[err])

    # --- Fetch entity from Kissinger ---
    try:
        entity_data = _gql(_ENTITY_QUERY, {"id": contact_id}, endpoint, token)
    except Exception as exc:  # noqa: BLE001
        err = f"Failed to fetch entity {contact_id}: {exc}"
        print(f"[enrich_contact] FATAL: {err}", file=sys.stderr)
        return complete_run(run_id, status="failed", errors=[err])

    entity = entity_data.get("entity")
    if not entity:
        err = f"Entity {contact_id} not found in Kissinger"
        print(f"[enrich_contact] FATAL: {err}", file=sys.stderr)
        return complete_run(run_id, status="failed", errors=[err])

    # --- Fetch edges to find org associations ---
    try:
        edges_data = _gql(
            _EDGES_FROM_QUERY,
            {"entityId": contact_id, "first": 20},
            endpoint,
            token,
        )
        raw_edges = [e["node"] for e in edges_data.get("edgesFrom", {}).get("edges", [])]
    except Exception:  # noqa: BLE001
        raw_edges = []

    works_at_edges = [e for e in raw_edges if e["relation"] == "works_at"]
    org_id: str | None = works_at_edges[0]["target"] if works_at_edges else None

    # Determine entity kind to decide goal strategy
    kind = entity.get("kind", "person")
    entity_name = entity.get("name", "")
    entity_meta = {m["key"]: m["value"] for m in entity.get("meta", [])}
    entity_tags = entity.get("tags", [])

    # For a person: discover additional contacts at their org (org_chart goal)
    # and also try to enrich their own work history
    # For an org: discover org chart (find people working there)
    goals = ["org_chart"] if kind == "org" else ["work_history", "org_chart"]

    errors: list[str] = []
    contacts_found = 0
    contacts_added = 0
    duplicates_skipped = 0
    fuzzy_flagged = 0
    skipped_fresh = 0
    sources_attempted: list[str] = []
    sources_skipped: list[str] = []

    # --- Identify the company name to search for ---
    if kind == "org":
        company_name = entity_name
        search_org_id = contact_id
    else:
        # For a person, get their company name from meta or org edge
        company_name = (
            entity_meta.get("company")
            or entity_meta.get("employer")
            or (works_at_edges[0].get("notes", "") if works_at_edges else "")
        )
        # If no company name from meta, skip org_chart goal
        if not company_name:
            goals = [g for g in goals if g != "org_chart"]
        search_org_id = org_id

    # --- Work history enrichment for a person (enriches the entity itself) ---
    if kind == "person" and "work_history" in goals:
        wh_sources = available_sources_for_goal(source_manifest, "work_history")
        for source in wh_sources:
            sid = source["source_id"]
            sources_attempted.append(sid)

            # Currently only google_serp_free and kissinger_graph are available.
            # google_serp_free is better used for org_chart discovery; kissinger
            # already has this entity. We record the attempt but no new data is
            # fetched until paid sources are enabled.
            # Future: when Apollo/PDL/LinkedIn available, call their person endpoint here.
            print(
                f"[enrich_contact] work_history source '{sid}' — "
                "no person-level fetch implemented for this source yet (key-gated)",
                file=sys.stderr,
            )
            break  # Only attempt first available source; avoid duplicate log spam

    # --- Org chart enrichment: discover colleagues via web search ---
    if company_name and "org_chart" in goals:
        oc_sources = available_sources_for_goal(source_manifest, "org_chart")
        if not oc_sources:
            sources_skipped.extend(
                [s["source_id"] for s in source_manifest["sources"]
                 if "org_chart" in s["goals"] and not s["available"]]
            )
            print(
                "[enrich_contact] No available org_chart sources — "
                "all sources require API keys",
                file=sys.stderr,
            )
        else:
            for source in oc_sources[:1]:  # Use best available source
                sid = source["source_id"]

                if sid == "kissinger_graph":
                    # Kissinger graph doesn't discover new contacts — skip for org_chart
                    continue

                sources_attempted.append(sid)

                # Record all skipped sources
                for s in source_manifest["sources"]:
                    if "org_chart" in s["goals"] and not s["available"]:
                        if s["source_id"] not in sources_skipped:
                            sources_skipped.append(s["source_id"])

                try:
                    raw_contacts = find_supply_chain_contacts(
                        company_name, delay_secs=1.0
                    )
                except Exception as exc:  # noqa: BLE001
                    err = f"find_supply_chain_contacts failed for '{company_name}': {exc}"
                    errors.append(err)
                    print(f"[enrich_contact] {err}", file=sys.stderr)
                    continue

                contacts_found += len(raw_contacts)

                if not raw_contacts:
                    print(
                        f"[enrich_contact] No contacts found for '{company_name}'",
                        file=sys.stderr,
                    )
                    continue

                # Dedup against CRM
                try:
                    dedup = dedup_crm_contacts(
                        raw_contacts,
                        org_id=search_org_id,
                        endpoint=endpoint,
                        token=token,
                    )
                except Exception as exc:  # noqa: BLE001
                    err = f"dedup_crm_contacts failed: {exc}"
                    errors.append(err)
                    print(f"[enrich_contact] {err}", file=sys.stderr)
                    continue

                new_contacts = dedup["new"]
                duplicates_skipped += len(dedup["duplicates"])
                fuzzy_flagged += len(dedup["fuzzy_matches"])

                # Write new contacts via hygiene layer
                with HygieneLayer(
                    run_id=run_id,
                    source_id=sid,
                    goal="org_chart",
                    manifest=source_manifest,
                    dry_run=dry_run,
                    endpoint=endpoint,
                    token=token,
                ) as layer:
                    for contact in new_contacts:
                        contact_org_id = search_org_id or contact.get("org_kissinger_id")
                        result = layer.write_contact(
                            contact,
                            org_kissinger_id=contact_org_id,
                        )
                        if result["status"] == "written":
                            contacts_added += 1
                        elif result["status"] == "skipped":
                            skipped_fresh += 1
                        elif result["status"] == "error":
                            errors.append(
                                f"{contact.get('name','?')}: {result['reason']}"
                            )

    # --- Also enrich the entity itself if it's a prospect org ---
    if kind == "org" and "prospect" in entity_tags and org_id is None:
        # The entity IS the org — we just enriched its contacts above
        pass
    elif kind == "person" and "kissinger_graph" in [s["source_id"] for s in source_manifest["sources"] if s["available"]]:
        # Use Kissinger graph to enrich first-degree connections
        # (connections goal — reads edges, no new writes needed for basic enrichment)
        if "kissinger_graph" not in sources_attempted:
            sources_attempted.append("kissinger_graph")

    # --- Write final run manifest ---
    final_status = "failed" if (errors and contacts_added == 0 and contacts_found == 0) else "completed"
    result_manifest = complete_run(
        run_id,
        status=final_status,
        companies_scanned=1,
        contacts_found=contacts_found,
        contacts_added=contacts_added,
        duplicates_skipped=duplicates_skipped,
        fuzzy_flagged=fuzzy_flagged,
        skipped_fresh=skipped_fresh,
        sources_attempted=list(set(sources_attempted)),
        sources_skipped=list(set(sources_skipped)),
        errors=errors,
    )

    print(
        f"[enrich_contact] Done: "
        f"found={contacts_found} added={contacts_added} "
        f"dupes={duplicates_skipped} fuzzy={fuzzy_flagged} "
        f"fresh_skipped={skipped_fresh} errors={len(errors)}",
        file=sys.stderr,
    )
    return result_manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich a single Kissinger contact with provenance hygiene"
    )
    parser.add_argument("--contact-id", required=True, help="Kissinger entity ID")
    parser.add_argument("--run-id", required=True, help="UUID for this run (from API route)")
    parser.add_argument("--endpoint", default=KISSINGER_ENDPOINT)
    parser.add_argument("--token", default=KISSINGER_API_TOKEN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = enrich_contact(
        contact_id=args.contact_id,
        run_id=args.run_id,
        endpoint=args.endpoint,
        token=args.token,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))
    if result.get("status") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
