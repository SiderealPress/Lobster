#!/usr/bin/env python3
"""
classify-seed-prospects.py

Classifies all Kissinger entities tagged 'prospect' with the canonical ontology
tags and meta fields defined in lobster-shop/prospect-enrichment/ontology/.

Adds:
  - vertical:* tags
  - size:* tags
  - supply_chain:* tags
  - stage:research (if no stage: tag present)
  - Meta: hq_location, revenue_estimate, employee_count, erp_system,
           key_challenge, economic_buyer_title, pipeline_stage,
           last_enriched_at, icp_score, _prov_* fields

Idempotent: safe to re-run. Skips tags/meta already present.
Use --dry-run to preview without writing.

Usage:
  python3 scripts/classify-seed-prospects.py --dry-run
  python3 scripts/classify-seed-prospects.py
"""

import os
import argparse
import json
import urllib.request
import sys
from datetime import datetime, timezone

KISSINGER_URL = "http://localhost:8080/graphql"
SCRIPT_VERSION = "1.0.0"
SCRIPT_NAME = "classify-seed-prospects.py"

# ---------------------------------------------------------------------------
# Canonical classification data for the seed prospects.
#
# Externalized to a private JSON file — this is proprietary business data,
# not appropriate for the public repo. Configure the path via
# CLASSIFY_SEED_PROSPECTS_DATA_PATH; defaults to the standard user-config
# data location.
# ---------------------------------------------------------------------------
_CLASSIFICATIONS_PATH = os.environ.get(
    "CLASSIFY_SEED_PROSPECTS_DATA_PATH",
    os.path.expanduser(
        "~/lobster-user-config/data/classify-seed-prospects-classifications.json"
    ),
)


def _load_company_classifications() -> dict:
    if not os.path.isfile(_CLASSIFICATIONS_PATH):
        print(
            f"[ERROR] Classification data file not found: {_CLASSIFICATIONS_PATH}\n"
            "        Set CLASSIFY_SEED_PROSPECTS_DATA_PATH to point at your "
            "private classification dataset.",
            file=sys.stderr,
        )
        return {}
    with open(_CLASSIFICATIONS_PATH) as _f:
        return json.load(_f)


COMPANY_CLASSIFICATIONS = _load_company_classifications()


def gql(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against Kissinger."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        KISSINGER_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_all_prospects() -> list[dict]:
    """Paginate through Kissinger and return all entities tagged 'prospect'."""
    all_prospects = []
    cursor = None
    while True:
        if cursor:
            q = (
                '{ entities(first: 200, kind: "org", after: "%s") '
                "{ nodes { id kind name tags } pageInfo { hasNextPage endCursor } } }"
                % cursor
            )
        else:
            q = (
                '{ entities(first: 200, kind: "org") '
                "{ nodes { id kind name tags } pageInfo { hasNextPage endCursor } } }"
            )
        result = gql(q)
        if "errors" in result:
            print(f"ERROR paginating entities: {result['errors']}", file=sys.stderr)
            sys.exit(1)
        nodes = result["data"]["entities"]["nodes"]
        all_prospects.extend(
            n for n in nodes if "prospect" in (n.get("tags") or [])
        )
        page_info = result["data"]["entities"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return all_prospects


def get_entity_full(entity_id: str) -> dict:
    """Fetch full entity including meta fields."""
    q = (
        '{ entity(id: "%s") { id name kind tags notes meta { key value } } }'
        % entity_id
    )
    result = gql(q)
    if "errors" in result:
        print(f"ERROR fetching entity {entity_id}: {result['errors']}", file=sys.stderr)
        return {}
    return result["data"]["entity"]


def compute_tags_to_add(entity: dict, classification: dict) -> list[str]:
    """Return list of tags that should be added but aren't present yet."""
    current_tags = set(entity.get("tags") or [])
    desired_tags = []

    # Vertical tags
    desired_tags.extend(classification["verticals"])

    # Size tag
    desired_tags.append(classification["size"])

    # Supply chain tag
    desired_tags.append(classification["supply_chain"])

    # Stage tag — add stage:research only if no stage: tag already present
    has_stage = any(t.startswith("stage:") for t in current_tags)
    if not has_stage:
        desired_tags.append("stage:research")

    # Filter to only tags not already present
    return [t for t in desired_tags if t not in current_tags]


def compute_meta_to_add(entity: dict, classification: dict) -> list[dict]:
    """Return list of meta {key, value} entries to add (skip existing keys)."""
    existing_keys = {m["key"] for m in (entity.get("meta") or [])}
    now_iso = datetime.now(timezone.utc).isoformat()

    desired_meta = {
        "hq_location": classification.get("hq_location", ""),
        "revenue_estimate": classification.get("revenue_estimate", ""),
        "employee_count": classification.get("employee_count", ""),
        "erp_system": classification.get("erp_system", ""),
        "key_challenge": classification.get("key_challenge", ""),
        "economic_buyer_title": classification.get("economic_buyer_title", ""),
        "pipeline_stage": "research",
        "icp_score": classification.get("icp_score", ""),
        "last_enriched_at": now_iso,
        "source": "prospects-v2",
        "_prov_imported_by": "classify-seed-prospects.py",
        "_prov_source": "ontology-v1",
        "_prov_imported_at": now_iso,
        "_prov_source_file": SCRIPT_NAME,
        "_prov_script_version": SCRIPT_VERSION,
    }

    # Only add keys that don't already exist
    return [
        {"key": k, "value": v}
        for k, v in desired_meta.items()
        if k not in existing_keys and v
    ]


def update_entity(entity_id: str, new_tags: list[str], new_meta: list[dict]) -> bool:
    """Apply tag and meta updates to a Kissinger entity."""
    if not new_tags and not new_meta:
        return True  # Nothing to do

    # We need to fetch current full state and merge
    entity = get_entity_full(entity_id)
    current_tags = list(entity.get("tags") or [])
    merged_tags = current_tags + new_tags

    mutation = """
mutation UpdateEntity($id: String!, $input: UpdateEntityInput!) {
  updateEntity(id: $id, input: $input) {
    id name tags meta { key value }
  }
}
"""
    variables = {
        "id": entity_id,
        "input": {
            "tags": merged_tags,
            "meta": new_meta if new_meta else None,
        },
    }
    # Remove None values from input
    variables["input"] = {k: v for k, v in variables["input"].items() if v is not None}

    result = gql(mutation, variables)
    if "errors" in result:
        print(f"  ERROR updating {entity_id}: {result['errors']}", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Classify seed prospects with canonical ontology tags and meta fields"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to Kissinger",
    )
    parser.add_argument(
        "--company",
        type=str,
        default=None,
        help="Only process a specific company by name (substring match)",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("Seed Prospect Classification")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE WRITE'}")
    print("=" * 70)
    print()

    # Fetch all prospects
    print("Fetching all prospect entities from Kissinger...")
    prospects = get_all_prospects()
    print(f"Found {len(prospects)} prospect entities")
    print()

    stats = {
        "total": len(prospects),
        "matched": 0,
        "unmatched": 0,
        "no_changes": 0,
        "updated": 0,
        "errors": 0,
        "tags_added": 0,
        "meta_added": 0,
    }

    for entity in prospects:
        name = entity["name"]

        # Optional filter
        if args.company and args.company.lower() not in name.lower():
            continue

        # Look up classification data
        classification = COMPANY_CLASSIFICATIONS.get(name)
        if not classification:
            print(f"  [UNMATCHED] {name} — no classification data, skipping")
            stats["unmatched"] += 1
            continue

        stats["matched"] += 1

        # Fetch full entity for meta
        full_entity = get_entity_full(entity["id"])

        # Compute deltas
        new_tags = compute_tags_to_add(full_entity, classification)
        new_meta = compute_meta_to_add(full_entity, classification)

        if not new_tags and not new_meta:
            print(f"  [OK]      {name} — already classified, no changes needed")
            stats["no_changes"] += 1
            continue

        print(f"  [UPDATE]  {name} ({entity['id'][:8]}...)")
        if new_tags:
            print(f"            + tags: {new_tags}")
        if new_meta:
            meta_keys = [m["key"] for m in new_meta]
            print(f"            + meta: {meta_keys}")

        if not args.dry_run:
            success = update_entity(entity["id"], new_tags, new_meta)
            if success:
                stats["updated"] += 1
                stats["tags_added"] += len(new_tags)
                stats["meta_added"] += len(new_meta)
                print(f"            -> Written OK")
            else:
                stats["errors"] += 1
                print(f"            -> ERROR (see above)")
        else:
            stats["updated"] += 1  # Count as "would update" in dry run
            stats["tags_added"] += len(new_tags)
            stats["meta_added"] += len(new_meta)

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"  Total prospects found:  {stats['total']}")
    print(f"  Matched (have data):    {stats['matched']}")
    print(f"  Unmatched (no data):    {stats['unmatched']}")
    print(f"  Already classified:     {stats['no_changes']}")
    print(f"  {'Would update' if args.dry_run else 'Updated'}:            {stats['updated']}")
    print(f"  Tags {'to add' if args.dry_run else 'added'}:              {stats['tags_added']}")
    print(f"  Meta fields {'to add' if args.dry_run else 'added'}:       {stats['meta_added']}")
    if not args.dry_run:
        print(f"  Errors:                 {stats['errors']}")
    if args.dry_run:
        print()
        print("  DRY RUN — no changes written. Re-run without --dry-run to apply.")
    print()


if __name__ == "__main__":
    main()
