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
import hashlib
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

from manifest_loader import load_manifest, available_sources_for_goal, confidence_from_score, hash_response, now_iso
from pipeline_hygiene import HygieneLayer
from run_manifest import create_run, complete_run, update_run
from find_supply_chain_contacts import find_supply_chain_contacts
from dedup_crm_contacts import dedup_crm_contacts

# Clay integration — person-level enrichment (work_history goal)
# Imported lazily inside the function to keep startup fast when Clay is unavailable
_CLAY_AVAILABLE: bool | None = None  # None = unchecked, True/False = checked

# GraphQL mutations for writing enriched meta back to Kissinger entities
_UPDATE_ENTITY_META_MUTATION = """
mutation UpdateEntityMeta($id: String!, $meta: [MetaInput!]!) {
  updateEntityMeta(id: $id, meta: $meta) { id meta { key value } }
}
"""

# NOTE: An async webhook-table enrichment path also exists in ClayClient.enrich_via_webhook().
# This supports batch enrichment for plans that don't include direct REST lookup.
# Set CLAY_WEBHOOK_URL in config.env and call enrich_via_webhook() to use it.
# That path is not wired here (requires a callback receiver); direct API is used first.

KISSINGER_ENDPOINT = os.environ.get("KISSINGER_ENDPOINT", "http://localhost:8080/graphql")
KISSINGER_API_TOKEN = os.environ.get("KISSINGER_API_TOKEN", "")

_MANIFEST_PATH = Path(__file__).parent.parent / "sources" / "manifest.json"
_CLAY_SOURCE_URL = "https://api.clay.com/v3/sources/people"


def _build_clay_provenance_meta(
    run_id: str,
    goal: str,
    raw_response: str,
    goal_score: float,
) -> dict[str, str]:
    """
    Build the standard provenance meta dict for a Clay enrichment write.

    Uses the multi-source suffix scheme from provenance/ontology.md so Clay's
    provenance keys don't collide with those from Apollo or other sources on the
    same entity.  The unsuffixed keys are also set so they reflect the most
    recent enrichment overall.
    """
    confidence = confidence_from_score(goal_score)
    raw_hash = hash_response(raw_response)
    ts = now_iso()
    return {
        # Source-specific (multi-source suffix scheme)
        "provenance.source.clay": "clay",
        "provenance.enriched_at.clay": ts,
        "provenance.goal.clay": goal,
        "provenance.confidence.clay": confidence,
        "provenance.raw_response_hash.clay": raw_hash,
        # Generic keys — always reflect most-recent enrichment
        "provenance.source": "clay",
        "provenance.source_url": _CLAY_SOURCE_URL,
        "provenance.enriched_at": ts,
        "provenance.enriched_by": "wallace",
        "provenance.pipeline_run_id": run_id,
        "provenance.confidence": confidence,
        "provenance.goal": goal,
        "provenance.raw_response_hash": raw_hash,
    }

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


def _enrich_person_via_clay(
    *,
    entity: dict[str, Any],
    entity_meta: dict[str, str],
    entity_name: str,
    run_id: str,
    source: dict[str, Any],
    dry_run: bool,
    endpoint: str,
    token: str,
    errors: list[str],
) -> None:
    """
    Enrich an existing Kissinger person entity using Clay's waterfall lookup.

    Clay sits after Apollo/ZoomInfo/PDL in the work_history waterfall — its
    confidence score of 0.75 reflects that it aggregates secondary sources.
    When it IS the best available source (those above are key-gated), it runs
    first and fills gaps (email, LinkedIn URL, title, phone, location, etc.).

    Lookup strategy (in priority order):
      1. By email  — if the entity already has an email, use it to find LinkedIn/title
      2. By LinkedIn URL — if the entity has a LinkedIn URL, use it to find email
      3. By name + org — fallback for contacts with neither

    Epistemic rules:
      - Clay data NEVER overwrites existing fields from higher-confidence sources.
      - Conflicts with existing fields are logged and the existing value is kept.
      - Tags the entity with 'clay-enriched'.
      - Provenance follows the multi-source suffix scheme from ontology.md.

    NOTE: enrich_via_webhook() in ClayClient supports async batch enrichment
    for plans without direct REST access (set CLAY_WEBHOOK_URL in config.env).
    That path is not wired here — implement a callback receiver first.
    """
    # Lazy import — only load Clay when the key is present
    try:
        _src_dir = Path(__file__).parent.parent.parent.parent / "src"
        sys.path.insert(0, str(_src_dir))
        from integrations.clay.client import ClayClient, ClayError, CLAY_TAG
    except ImportError as exc:
        errors.append(f"Clay import failed: {exc}")
        print(f"[enrich_contact] Clay import failed: {exc}", file=sys.stderr)
        return

    try:
        clay_client = ClayClient()
    except Exception as exc:  # noqa: BLE001 — ClayError is not in scope yet if import failed
        errors.append(f"Clay client init failed: {exc}")
        print(f"[enrich_contact] Clay client init failed: {exc}", file=sys.stderr)
        return

    entity_id = entity["id"]
    existing_email    = entity_meta.get("email", "").strip()
    existing_linkedin = entity_meta.get("linkedin_url", "").strip()
    existing_org      = (
        entity_meta.get("org")
        or entity_meta.get("company")
        or entity_meta.get("employer", "")
    ).strip()

    # Idempotency: skip if Clay enriched this entity within its freshness window
    # (21 days per manifest).  We read provenance from the entity meta array
    # (enrich_contact keeps meta as a dict, but idempotency_check needs a list).
    entity_meta_list = [{"key": k, "value": v} for k, v in entity_meta.items()]
    _pipeline_dir = Path(__file__).parent.parent / "pipeline"
    sys.path.insert(0, str(_pipeline_dir.parent))
    try:
        from pipeline.idempotency_check import is_fresh
        freshness = is_fresh(
            entity_meta=entity_meta_list,
            source_id="clay",
            data_freshness_days=source.get("data_freshness_days", 21),
        )
        if freshness.skip:
            print(
                f"[enrich_contact] Clay: skipping {entity_name} — "
                f"already enriched {freshness.reason}",
                file=sys.stderr,
            )
            return
    except ImportError:
        pass  # idempotency_check unavailable — proceed without freshness guard

    # Attempt Clay lookup using the best available identifier
    clay_person = None
    lookup_method = "none"

    try:
        if existing_email:
            print(
                f"[enrich_contact] Clay: looking up '{entity_name}' by email...",
                file=sys.stderr,
            )
            clay_person = clay_client.lookup_by_email(existing_email)
            lookup_method = f"email:{existing_email}"

        if clay_person is None and existing_linkedin:
            print(
                f"[enrich_contact] Clay: looking up '{entity_name}' by LinkedIn URL...",
                file=sys.stderr,
            )
            clay_person = clay_client.lookup_by_linkedin(existing_linkedin)
            lookup_method = "linkedin"

        if clay_person is None and entity_name and existing_org:
            print(
                f"[enrich_contact] Clay: looking up '{entity_name}' by name+org ({existing_org})...",
                file=sys.stderr,
            )
            clay_person = clay_client.lookup_by_name(entity_name, company=existing_org)
            lookup_method = f"name:{entity_name}+org:{existing_org}"

    except ClayError as exc:
        # Standard plan returns 404/402 — fail gracefully, don't abort the run
        if exc.status_code in (402, 404):
            print(
                f"[enrich_contact] Clay: no data for '{entity_name}' "
                f"(status={exc.status_code}, likely standard plan) — skipping",
                file=sys.stderr,
            )
            return
        errors.append(f"Clay lookup failed for '{entity_name}': {exc}")
        print(f"[enrich_contact] Clay error: {exc}", file=sys.stderr)
        return
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Clay lookup error for '{entity_name}': {exc}")
        print(f"[enrich_contact] Clay unexpected error: {exc}", file=sys.stderr)
        return

    if clay_person is None:
        print(
            f"[enrich_contact] Clay: no data found for '{entity_name}' "
            f"(method={lookup_method}) — standard plan or not in Clay index",
            file=sys.stderr,
        )
        return

    # Log which sub-sources Clay used
    if clay_person.data_sources:
        print(
            f"[enrich_contact] Clay sub-sources for '{entity_name}': "
            f"{', '.join(clay_person.data_sources)}",
            file=sys.stderr,
        )

    # Extract fields Clay returned
    clay_fields = clay_person.to_meta_fields()
    if not clay_fields:
        print(
            f"[enrich_contact] Clay: returned person but no useful fields for '{entity_name}'",
            file=sys.stderr,
        )
        return

    # Conflict detection: never overwrite fields from higher-confidence sources
    # (Apollo confidence 0.80 > Clay 0.75).
    # Rule: if the entity already has a field, keep it regardless of source.
    fields_to_write: dict[str, str] = {}
    conflicts: list[str] = []
    for field, clay_val in clay_fields.items():
        if not clay_val:
            continue
        existing_val = entity_meta.get(field, "").strip()
        if not existing_val:
            fields_to_write[field] = clay_val  # gap fill — accept Clay
        elif existing_val.lower() != clay_val.lower():
            conflicts.append(
                f"  {entity_name}.{field}: existing={repr(existing_val)} "
                f"vs Clay={repr(clay_val)} — keeping existing"
            )

    if conflicts:
        for c in conflicts:
            print(f"[enrich_contact] Clay conflict (kept existing): {c}", file=sys.stderr)

    if not fields_to_write:
        print(
            f"[enrich_contact] Clay: no new fields for '{entity_name}' "
            f"(all already populated)",
            file=sys.stderr,
        )
        return

    # Build provenance meta using the multi-source suffix scheme
    goal_score = source.get("goal_scores", {}).get("work_history", 0.85)
    raw_response_str = json.dumps(clay_person.raw) if clay_person.raw else "{}"
    provenance_meta = _build_clay_provenance_meta(
        run_id=run_id,
        goal="work_history",
        raw_response=raw_response_str,
        goal_score=goal_score,
    )

    # Merge: existing meta + Clay gap-fills + provenance + tag
    new_meta = dict(entity_meta)
    new_meta.update(fields_to_write)
    new_meta.update(provenance_meta)

    existing_tags: list[str] = entity.get("tags", [])
    new_tags = list(existing_tags)
    if CLAY_TAG not in new_tags:
        new_tags.append(CLAY_TAG)

    if dry_run:
        print(
            f"[enrich_contact] Clay [dry-run]: would write "
            f"{len(fields_to_write)} field(s) for '{entity_name}': "
            f"{list(fields_to_write.keys())}",
            file=sys.stderr,
        )
        return

    meta_input = [{"key": k, "value": v} for k, v in new_meta.items()]
    try:
        _gql(
            _UPDATE_ENTITY_META_MUTATION,
            {"id": entity_id, "meta": meta_input},
            endpoint,
            token,
        )
        print(
            f"[enrich_contact] Clay: wrote {len(fields_to_write)} field(s) "
            f"for '{entity_name}': {list(fields_to_write.keys())}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        err = f"Clay: updateEntityMeta failed for '{entity_name}': {exc}"
        errors.append(err)
        print(f"[enrich_contact] {err}", file=sys.stderr)


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
    # Clay fits here: it enriches an *existing* contact by filling gap fields
    # (email, LinkedIn URL, title, phone, etc.) by running a waterfall across 100+
    # sub-sources.  Waterfall position: after Apollo/ZoomInfo/PDL (when those are
    # available) — Clay's confidence (0.75) is intentionally one step below Apollo
    # (0.80).  When Clay IS the best available source, it runs first.
    if kind == "person" and "work_history" in goals:
        wh_sources = available_sources_for_goal(source_manifest, "work_history")
        for source in wh_sources:
            sid = source["source_id"]

            if sid == "kissinger_graph":
                # Kissinger graph describes existing connections, not new contact data
                continue

            if sid == "clay":
                sources_attempted.append(sid)
                _enrich_person_via_clay(
                    entity=entity,
                    entity_meta=entity_meta,
                    entity_name=entity_name,
                    run_id=run_id,
                    source=source,
                    dry_run=dry_run,
                    endpoint=endpoint,
                    token=token,
                    errors=errors,
                )
                break  # Clay is the top available work_history source — stop here

            # Other paid sources (Apollo, PDL, LinkedIn SERP, etc.) — not yet
            # implemented.  Log the attempt so ops knows they're key-gated.
            sources_attempted.append(sid)
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
            # Select best available org_chart source that supports bulk contact discovery.
            # Clay enriches *individual* contacts but does NOT have a "find all employees
            # at company X" endpoint — skip Clay for org_chart discovery and fall through
            # to the next available source (google_serp_free, company_website, etc.).
            oc_source = None
            for s in oc_sources:
                if s["source_id"] in ("kissinger_graph", "clay"):
                    # kissinger_graph: reads existing graph, no new discovery
                    # clay: person-level enrichment only — no company employee list API
                    if s["source_id"] == "clay":
                        print(
                            "[enrich_contact] Skipping Clay for org_chart — Clay enriches "
                            "individual contacts (work_history goal), not company employee lists. "
                            "Clay will run in the work_history pass for person entities.",
                            file=sys.stderr,
                        )
                    continue
                oc_source = s
                break

            if oc_source is None:
                sources_skipped.extend(
                    [s["source_id"] for s in source_manifest["sources"]
                     if "org_chart" in s["goals"] and not s["available"]]
                )
                print(
                    "[enrich_contact] No suitable org_chart discovery source available — "
                    "Clay skipped (person-level only), others require API keys",
                    file=sys.stderr,
                )
            else:
                sid = oc_source["source_id"]
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
                    raw_contacts = []

                contacts_found += len(raw_contacts)

                if not raw_contacts:
                    print(
                        f"[enrich_contact] No contacts found for '{company_name}'",
                        file=sys.stderr,
                    )
                else:
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
                        dedup = {"new": [], "duplicates": [], "fuzzy_matches": []}

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
