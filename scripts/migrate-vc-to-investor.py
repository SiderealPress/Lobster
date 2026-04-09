#!/usr/bin/env python3
"""
migrate-vc-to-investor.py

ARCHITECTURAL DECISION (BIS-325):
  Kissinger's EntityKind is a closed Rust enum: person, place, property, project, org, skill.
  - 'investor_firm' and 'investor_person' do NOT exist as native kinds.
  - updateEntity does NOT expose a kind field (kind is immutable via GraphQL).
  - Decision: use kind=org + tag=vc for investor firms, kind=person + tag=vc for investor people.
  - This is consistent with the existing classifyOrg() pattern in eloso-bisque.
  - No Kissinger recompile required; zero data loss; idempotent.

What this script does:
  1. Reads kissinger_vc_firms.json
  2. For each VC firm (kind=org): creates or updates in Kissinger with tag=vc and full meta
  3. For each VC person (kind=person): creates or updates with tags=vc,investor and org link via works_at
  4. Links people to their firm via works_at edge
  5. Full provenance meta: source, migrated_at, pipeline_run_id
  6. Dry-run mode: --dry-run logs all changes without writing
  7. Idempotent: safe to re-run (matches by name)

Usage:
  python3 migrate-vc-to-investor.py --dry-run
  python3 migrate-vc-to-investor.py
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KISSINGER_URL = os.environ.get("KISSINGER_API_URL", "http://localhost:8080/graphql")
VC_FIRMS_PATH = os.path.expanduser(
    "~/lobster-workspace/lobsterdrop/kissinger_vc_firms.json"
)
PIPELINE_RUN_ID = str(uuid.uuid4())[:8]
MIGRATED_AT = datetime.now(timezone.utc).isoformat()
SOURCE = "kissinger_vc_firms.json"


# ---------------------------------------------------------------------------
# GraphQL helpers
# ---------------------------------------------------------------------------

def gql(query: str, variables: dict = None, dry_run: bool = False, is_mutation: bool = False) -> dict:
    """Execute a GraphQL query or mutation."""
    if dry_run and is_mutation:
        return {}

    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        KISSINGER_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data.get("data", {})
    except urllib.error.URLError as e:
        print(f"[ERROR] Kissinger unreachable: {e}", file=sys.stderr)
        sys.exit(1)


def sanitize_search_query(name: str) -> str:
    """Escape special chars for Kissinger FTS search (CozoDB FTS syntax)."""
    # Replace hyphens and other FTS special chars with spaces
    # CozoDB FTS doesn't support raw hyphens in queries
    import re
    # Remove anything that's not alphanumeric or space
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", " ", name)
    # Collapse whitespace and take first meaningful word(s)
    words = cleaned.split()
    # Use up to first 3 words for search
    return " ".join(words[:3])


def find_entity_by_name(name: str, kind: str) -> Optional[dict]:
    """Search Kissinger for an entity by name and kind. Returns first match or None."""
    query = """
    query Search($q: String!, $limit: Int) {
      search(query: $q, limit: $limit) {
        __typename
        ... on EntitySearchHitGql {
          id
          kind
          name
          tags
          score
        }
      }
    }
    """
    search_q = sanitize_search_query(name)
    if not search_q.strip():
        return None
    data = gql(query, {"q": search_q, "limit": 50})
    hits = data.get("search", [])
    for hit in hits:
        if (
            hit.get("__typename") == "EntitySearchHitGql"
            and hit.get("kind") == kind
            and hit.get("name", "").lower() == name.lower()
        ):
            return hit
    return None


def get_entity_detail(entity_id: str) -> Optional[dict]:
    """Fetch full entity detail including meta."""
    query = """
    query EntityDetail($id: String!) {
      entity(id: $id) {
        id kind name tags notes meta { key value } archived
      }
    }
    """
    data = gql(query, {"id": entity_id})
    return data.get("entity")


def create_entity(kind: str, name: str, tags: list, notes: str, meta: list, dry_run: bool) -> Optional[dict]:
    """Create a new entity. Returns the created entity or None in dry-run."""
    mutation = """
    mutation CreateEntity($input: CreateEntityInput!) {
      createEntity(input: $input) {
        id kind name tags
      }
    }
    """
    input_data = {
        "kind": kind,
        "name": name,
        "tags": tags,
        "notes": notes,
        "meta": meta,
    }
    if dry_run:
        print(f"  [DRY-RUN] Would CREATE {kind}: '{name}' tags={tags}")
        return {"id": f"dry-run-{name[:8]}", "name": name, "kind": kind}

    data = gql(mutation, {"input": input_data}, is_mutation=True)
    return data.get("createEntity")


def update_entity(entity_id: str, tags: list, notes: str, meta: list, dry_run: bool) -> Optional[dict]:
    """Update existing entity tags, notes, meta."""
    mutation = """
    mutation UpdateEntity($id: String!, $input: UpdateEntityInput!) {
      updateEntity(id: $id, input: $input) {
        id kind name tags
      }
    }
    """
    input_data = {
        "tags": tags,
        "notes": notes,
        "meta": meta,
    }
    if dry_run:
        print(f"  [DRY-RUN] Would UPDATE entity {entity_id[:8]}... tags={tags}")
        return {"id": entity_id}

    data = gql(mutation, {"id": entity_id, "input": input_data}, is_mutation=True)
    return data.get("updateEntity")


def edge_exists(source_id: str, target_id: str, relation: str) -> bool:
    """Check if a directed edge already exists."""
    query = """
    query EdgesFrom($entityId: String!, $first: Int) {
      edgesFrom(entityId: $entityId, first: $first) {
        edges { node { source target relation } }
      }
    }
    """
    data = gql(query, {"entityId": source_id, "first": 200})
    edges = [e["node"] for e in data.get("edgesFrom", {}).get("edges", [])]
    return any(
        e["target"] == target_id and e["relation"] == relation
        for e in edges
    )


def create_edge(source_id: str, target_id: str, relation: str, notes: str = "", dry_run: bool = False):
    """Create an edge between two entities."""
    mutation = """
    mutation CreateEdge($input: CreateEdgeInput!) {
      createEdge(input: $input) {
        source target relation
      }
    }
    """
    input_data = {
        "source": source_id,
        "target": target_id,
        "relation": relation,
        "notes": notes,
    }
    if dry_run:
        print(f"  [DRY-RUN] Would CREATE edge {source_id[:8]}...--{relation}-->{target_id[:8]}...")
        return

    gql(mutation, {"input": input_data}, is_mutation=True)


# ---------------------------------------------------------------------------
# Provenance meta builder
# ---------------------------------------------------------------------------

def provenance_meta(extra: dict = None) -> list:
    """Build provenance meta entries."""
    entries = [
        {"key": "source", "value": SOURCE},
        {"key": "migrated_at", "value": MIGRATED_AT},
        {"key": "pipeline_run_id", "value": PIPELINE_RUN_ID},
    ]
    if extra:
        for k, v in extra.items():
            if v:
                entries.append({"key": k, "value": str(v)})
    return entries


def merge_meta(existing_meta: list, new_meta: list) -> list:
    """Merge new meta into existing, preserving existing keys not in new_meta."""
    existing_keys = {m["key"] for m in new_meta}
    merged = list(new_meta)
    for m in existing_meta:
        if m["key"] not in existing_keys:
            merged.append(m)
    return merged


# ---------------------------------------------------------------------------
# Main migration logic
# ---------------------------------------------------------------------------

def migrate_firm(firm: dict, dry_run: bool) -> Optional[str]:
    """Migrate a VC firm. Returns entity ID (real or dry-run placeholder)."""
    name = firm["name"]
    tags = list(set(firm.get("tags", []) + ["vc"]))

    # Build meta from firm data
    extra_meta = {
        "stage": firm.get("stage", ""),
        "check_size": firm.get("check_size", ""),
        "location": firm.get("location", ""),
        "sector_fit": firm.get("sector_fit", ""),
        "priority": firm.get("priority", ""),
        "website": firm.get("website", ""),
        "pipeline_stage": "Research",  # default pipeline stage
        "thesis": firm.get("notes", "")[:500] if firm.get("notes") else "",
    }
    meta = provenance_meta(extra_meta)
    notes = firm.get("notes", "")

    existing = find_entity_by_name(name, "org")

    if existing:
        entity_id = existing["id"]
        detail = get_entity_detail(entity_id)
        existing_tags = detail.get("tags", []) if detail else []
        existing_meta = detail.get("meta", []) if detail else []
        existing_notes = detail.get("notes", "") if detail else ""

        # Merge tags (add vc and any new tags)
        merged_tags = list(set(existing_tags + tags))
        merged_meta = merge_meta(existing_meta, meta)
        merged_notes = existing_notes or notes

        print(f"  UPDATE firm '{name}' (id={entity_id[:8]}...)")
        update_entity(entity_id, merged_tags, merged_notes, merged_meta, dry_run)
        return entity_id
    else:
        print(f"  CREATE firm '{name}'")
        created = create_entity("org", name, tags, notes, meta, dry_run)
        return created["id"] if created else None


def migrate_person(person: dict, firm_id: Optional[str], dry_run: bool) -> Optional[str]:
    """Migrate a VC person. Returns entity ID."""
    name = person["name"]
    org_name = person.get("org", "")
    tags = list(set(person.get("tags", []) + ["vc", "investor"]))

    extra_meta = {
        "title": person.get("title", ""),
        "org": org_name,
        "priority": person.get("priority", ""),
        "linkedin_url": person.get("linkedin", ""),
        "incentive": "Dealflow, returns, LP relationships",
    }
    meta = provenance_meta(extra_meta)
    notes = person.get("notes", "")

    existing = find_entity_by_name(name, "person")

    if existing:
        entity_id = existing["id"]
        detail = get_entity_detail(entity_id)
        existing_tags = detail.get("tags", []) if detail else []
        existing_meta = detail.get("meta", []) if detail else []
        existing_notes = detail.get("notes", "") if detail else ""

        merged_tags = list(set(existing_tags + tags))
        merged_meta = merge_meta(existing_meta, meta)
        merged_notes = existing_notes or notes

        print(f"  UPDATE person '{name}' (id={entity_id[:8]}...)")
        update_entity(entity_id, merged_tags, merged_notes, merged_meta, dry_run)
        return entity_id
    else:
        print(f"  CREATE person '{name}'")
        created = create_entity("person", name, tags, notes, meta, dry_run)
        return created["id"] if created else None


def link_person_to_firm(person_id: str, firm_id: str, title: str, dry_run: bool):
    """Create works_at edge from person to firm if it doesn't exist."""
    if dry_run:
        print(f"  [DRY-RUN] Would LINK person {person_id[:8]}... --works_at--> firm {firm_id[:8]}...")
        return

    if edge_exists(person_id, firm_id, "works_at"):
        print(f"  SKIP edge (already exists): {person_id[:8]}... --works_at--> {firm_id[:8]}...")
        return

    create_edge(person_id, firm_id, "works_at", notes=title, dry_run=dry_run)
    print(f"  LINKED person {person_id[:8]}... --works_at--> firm {firm_id[:8]}...")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Migrate VC firms to Kissinger investor entities")
    parser.add_argument("--dry-run", action="store_true", help="Log changes without writing")
    args = parser.parse_args()

    dry_run = args.dry_run

    if dry_run:
        print("=== DRY RUN MODE — no changes will be written ===\n")

    print(f"Pipeline run ID: {PIPELINE_RUN_ID}")
    print(f"Loading VC firms from: {VC_FIRMS_PATH}\n")

    with open(VC_FIRMS_PATH) as f:
        data = json.load(f)

    orgs = [x for x in data if x.get("kind") == "org"]
    people = [x for x in data if x.get("kind") == "person"]

    print(f"Found {len(orgs)} VC firms and {len(people)} VC people.\n")

    # Step 1: Migrate firms
    print("=== STEP 1: Migrating VC Firms ===")
    firm_name_to_id: dict[str, str] = {}

    firms_created = 0
    firms_updated = 0

    for firm in orgs:
        name = firm["name"]
        # Quick check: does it exist?
        existing = find_entity_by_name(name, "org")
        is_new = existing is None

        firm_id = migrate_firm(firm, dry_run)
        if firm_id:
            firm_name_to_id[name] = firm_id
            if is_new:
                firms_created += 1
            else:
                firms_updated += 1

    print(f"\nFirms: {firms_created} created, {firms_updated} updated.\n")

    # Step 2: Migrate people
    print("=== STEP 2: Migrating VC People ===")
    people_created = 0
    people_updated = 0
    edges_created = 0

    for person in people:
        name = person["name"]
        org_name = person.get("org", "")
        title = person.get("title", "")

        existing = find_entity_by_name(name, "person")
        is_new = existing is None

        firm_id = firm_name_to_id.get(org_name)
        person_id = migrate_person(person, firm_id, dry_run)

        if person_id and firm_id:
            link_person_to_firm(person_id, firm_id, title, dry_run)
            edges_created += 1

        if person_id:
            if is_new:
                people_created += 1
            else:
                people_updated += 1

    print(f"\nPeople: {people_created} created, {people_updated} updated.")
    print(f"Edges: {edges_created} works_at links created/verified.")

    # Step 3: Verification query
    print("\n=== STEP 3: Verification ===")
    verify_query = """
    query VCOrgs($first: Int) {
      entities(kind: "org", first: $first) {
        edges { node { id name tags } }
      }
    }
    """
    data_verify = gql(verify_query, {"first": 10000})
    all_orgs = [e["node"] for e in data_verify.get("entities", {}).get("edges", [])]
    vc_orgs = [e for e in all_orgs if "vc" in e.get("tags", [])]
    print(f"Kissinger now has {len(vc_orgs)} VC-tagged org entities.")

    if not dry_run:
        print(f"\nMigration complete.")
        print(f"  Firms created: {firms_created}")
        print(f"  Firms updated: {firms_updated}")
        print(f"  People created: {people_created}")
        print(f"  People updated: {people_updated}")
        print(f"  Edges: {edges_created}")
        print(f"  Total VC orgs in Kissinger: {len(vc_orgs)}")
    else:
        print("\nDry run complete — no changes written.")


if __name__ == "__main__":
    main()
