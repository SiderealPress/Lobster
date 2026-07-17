#!/usr/bin/env python3
"""
bootstrap-supply-chain-graph.py

Bootstraps supply chain relationships for seed prospects by:
1. Creating supplier/customer entities in Kissinger if they don't exist
2. Writing known_suppliers / known_customers structured meta to seed entities
3. Writing known_customers / known_suppliers back-refs to supplier entities
4. Tagging supplier entities with customer_of:{seed_id}
5. Tagging customer entities with supplier_of:{seed_id}

All relationships are stored as meta fields because Kissinger's GraphQL API
only exposes 'works_at' as an edge relation. See ontology doc §5 for the
recommended future Rust PR to extend EdgeRelation.

Usage:
  python3 scripts/bootstrap-supply-chain-graph.py --dry-run
  python3 scripts/bootstrap-supply-chain-graph.py
  python3 scripts/bootstrap-supply-chain-graph.py --seed "Greenbrier"
"""

import os
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

KISSINGER_URL = "http://localhost:8080/graphql"
SCRIPT_VERSION = "1.0.0"
SCRIPT_NAME = "bootstrap-supply-chain-graph.py"

# ---------------------------------------------------------------------------
# Known supply chain relationships.
#
# Externalized to a private JSON file — this is proprietary business data,
# not appropriate for the public repo. Configure the path via
# BOOTSTRAP_SUPPLY_CHAIN_DATA_PATH; defaults to the standard user-config
# data location.
#
# Format:
#   seed_name: {
#       "suppliers": [
#           {
#               "name": str,
#               "relationship_type": str,  # e.g. "steel_supplier", "component_supplier"
#               "confidence": "high"|"medium"|"low",
#               "source": str,             # research basis
#               "tags": [str],             # tags for the supplier entity
#               "notes": str,              # optional notes on the supplier entity
#           },
#       ],
#       "customers": [
#           {same structure}
#       ]
#   }
# ---------------------------------------------------------------------------
_SUPPLY_CHAIN_DATA_PATH = os.environ.get(
    "BOOTSTRAP_SUPPLY_CHAIN_DATA_PATH",
    os.path.expanduser(
        "~/lobster-user-config/data/bootstrap-supply-chain-relationships.json"
    ),
)


def _load_supply_chain_relationships() -> dict:
    if not os.path.isfile(_SUPPLY_CHAIN_DATA_PATH):
        print(
            f"[ERROR] Supply chain data file not found: {_SUPPLY_CHAIN_DATA_PATH}\n"
            "        Set BOOTSTRAP_SUPPLY_CHAIN_DATA_PATH to point at your "
            "private relationship dataset.",
            file=sys.stderr,
        )
        return {}
    with open(_SUPPLY_CHAIN_DATA_PATH) as _f:
        return json.load(_f)


SUPPLY_CHAIN_RELATIONSHIPS = _load_supply_chain_relationships()


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


def find_entity_by_name(name: str) -> dict | None:
    """Search for an entity by exact name match across all orgs."""
    # Paginate through all entities to find by name
    cursor = None
    while True:
        if cursor:
            q = '{ entities(first: 200, kind: "org", after: "%s") { nodes { id name kind tags meta { key value } } pageInfo { hasNextPage endCursor } } }' % cursor
        else:
            q = '{ entities(first: 200, kind: "org") { nodes { id name kind tags meta { key value } } pageInfo { hasNextPage endCursor } } }'
        result = gql(q)
        if "errors" in result:
            return None
        nodes = result["data"]["entities"]["nodes"]
        for node in nodes:
            if node["name"] == name:
                return node
        page_info = result["data"]["entities"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return None


def get_entity_full(entity_id: str) -> dict | None:
    """Fetch full entity including meta."""
    q = '{ entity(id: "%s") { id name kind tags notes meta { key value } } }' % entity_id
    result = gql(q)
    if "errors" in result:
        return None
    return result["data"]["entity"]


def create_entity(name: str, tags: list[str], notes: str = "") -> dict | None:
    """Create a new org entity in Kissinger."""
    now_iso = datetime.now(timezone.utc).isoformat()
    mutation = """
mutation CreateEntity($input: CreateEntityInput!) {
  createEntity(input: $input) {
    id name kind tags
  }
}
"""
    variables = {
        "input": {
            "kind": "org",
            "name": name,
            "tags": tags + ["prospect"],
            "notes": notes,
            "meta": [
                {"key": "_prov_imported_by", "value": SCRIPT_NAME},
                {"key": "_prov_source", "value": "supply-chain-bootstrap-v1"},
                {"key": "_prov_imported_at", "value": now_iso},
                {"key": "_prov_source_file", "value": SCRIPT_NAME},
                {"key": "_prov_script_version", "value": SCRIPT_VERSION},
            ],
        }
    }
    result = gql(mutation, variables)
    if "errors" in result:
        return None
    return result["data"]["createEntity"]


def update_entity_meta_and_tags(
    entity_id: str,
    current_tags: list[str],
    new_tags: list[str],
    new_meta: list[dict],
) -> bool:
    """Merge new tags and append new meta to an entity."""
    merged_tags = list(set(current_tags + new_tags))
    mutation = """
mutation UpdateEntity($id: String!, $input: UpdateEntityInput!) {
  updateEntity(id: $id, input: $input) {
    id name tags meta { key value }
  }
}
"""
    input_data: dict = {"tags": merged_tags}
    if new_meta:
        input_data["meta"] = new_meta
    result = gql(mutation, {"id": entity_id, "input": input_data})
    if "errors" in result:
        return False
    return True


def build_known_suppliers_json(suppliers_with_ids: list[dict]) -> str:
    """Build JSON array for known_suppliers meta field."""
    entries = []
    for s in suppliers_with_ids:
        entry = {
            "name": s["name"],
            "kissinger_id": s.get("kissinger_id", ""),
            "relationship_type": s["relationship_type"],
            "confidence": s["confidence"],
            "source": s["source"],
        }
        entries.append(entry)
    return json.dumps(entries)


def build_known_customers_json(customers_with_ids: list[dict]) -> str:
    """Build JSON array for known_customers meta field."""
    entries = []
    for c in customers_with_ids:
        entry = {
            "name": c["name"],
            "kissinger_id": c.get("kissinger_id", ""),
            "relationship_type": c["relationship_type"],
            "confidence": c["confidence"],
            "source": c["source"],
        }
        entries.append(entry)
    return json.dumps(entries)


def get_all_prospects() -> list[dict]:
    """Fetch all prospect entities from Kissinger."""
    all_prospects = []
    cursor = None
    while True:
        if cursor:
            q = '{ entities(first: 200, kind: "org", after: "%s") { nodes { id kind name tags } pageInfo { hasNextPage endCursor } } }' % cursor
        else:
            q = '{ entities(first: 200, kind: "org") { nodes { id kind name tags } pageInfo { hasNextPage endCursor } } }'
        result = gql(q)
        if "errors" in result:
            break
        nodes = result["data"]["entities"]["nodes"]
        all_prospects.extend(n for n in nodes if "prospect" in (n.get("tags") or []))
        page_info = result["data"]["entities"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return all_prospects


def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap supply chain graph relationships for seed prospects"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to Kissinger",
    )
    parser.add_argument(
        "--seed",
        type=str,
        default=None,
        help="Only process a specific seed (substring match on name)",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("Supply Chain Graph Bootstrap")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE WRITE'}")
    print("=" * 70)
    print()

    # Build a lookup of prospect entities by name
    print("Fetching all prospect entities from Kissinger...")
    prospects = get_all_prospects()
    prospect_by_name = {p["name"]: p for p in prospects}
    print(f"Found {len(prospects)} prospect entities")
    print()

    stats = {
        "seeds_processed": 0,
        "entities_created": 0,
        "entities_found_existing": 0,
        "seed_meta_updated": 0,
        "supplier_meta_updated": 0,
        "errors": 0,
        "total_relationships": 0,
    }

    now_iso = datetime.now(timezone.utc).isoformat()

    for seed_name, relationships in SUPPLY_CHAIN_RELATIONSHIPS.items():
        # Optional filter
        if args.seed and args.seed.lower() not in seed_name.lower():
            continue

        seed_entity = prospect_by_name.get(seed_name)
        if not seed_entity:
            print(f"[SKIP] Seed '{seed_name}' not found in Kissinger prospects")
            continue

        seed_id = seed_entity["id"]
        print(f"\n{'='*60}")
        print(f"[SEED] {seed_name} ({seed_id[:8]}...)")
        print(f"{'='*60}")
        stats["seeds_processed"] += 1

        # Process suppliers
        suppliers_resolved = []
        for sup in relationships.get("suppliers", []):
            sup_name = sup["name"]
            print(f"\n  [SUPPLIER] {sup_name}")

            # Try to find existing entity
            existing = find_entity_by_name(sup_name)
            if existing:
                sup_id = existing["id"]
                print(f"    Found existing: {sup_id[:8]}...")
                stats["entities_found_existing"] += 1
            else:
                print(f"    Not found — {'would create' if args.dry_run else 'creating'}...")
                if not args.dry_run:
                    created = create_entity(
                        name=sup_name,
                        tags=sup["tags"],
                        notes=sup.get("notes", ""),
                    )
                    if not created:
                        print(f"    ERROR creating entity")
                        stats["errors"] += 1
                        continue
                    sup_id = created["id"]
                    print(f"    Created: {sup_id[:8]}...")
                    stats["entities_created"] += 1
                else:
                    sup_id = "[dry-run-id]"
                    stats["entities_created"] += 1  # would-create count

            suppliers_resolved.append({**sup, "kissinger_id": sup_id})

            # Add back-ref meta to supplier: known_customers entry + tags
            if not args.dry_run and sup_id != "[dry-run-id]":
                supplier_full = get_entity_full(sup_id)
                if supplier_full:
                    existing_meta_keys = {m["key"] for m in (supplier_full.get("meta") or [])}
                    existing_tags = supplier_full.get("tags") or []

                    # Build known_customers meta for the supplier
                    known_customers_entry = json.dumps([{
                        "name": seed_name,
                        "kissinger_id": seed_id,
                        "relationship_type": sup["relationship_type"],
                        "confidence": sup["confidence"],
                        "source": sup["source"],
                    }])

                    new_meta_for_supplier = []
                    # Use a scoped key so multiple seeds don't overwrite each other
                    customer_meta_key = f"known_customers_of_{seed_id[:8]}"
                    if customer_meta_key not in existing_meta_keys:
                        new_meta_for_supplier.append({
                            "key": customer_meta_key,
                            "value": known_customers_entry,
                        })
                    if "_prov_supply_chain_bootstrap" not in existing_meta_keys:
                        new_meta_for_supplier.append({
                            "key": "_prov_supply_chain_bootstrap",
                            "value": now_iso,
                        })

                    new_tags_for_supplier = []
                    relationship_tag = f"customer_of:{seed_id}"
                    if relationship_tag not in existing_tags:
                        new_tags_for_supplier.append(relationship_tag)
                    if "supplier" not in existing_tags:
                        new_tags_for_supplier.append("supplier")

                    if new_meta_for_supplier or new_tags_for_supplier:
                        ok = update_entity_meta_and_tags(
                            sup_id,
                            existing_tags,
                            new_tags_for_supplier,
                            new_meta_for_supplier,
                        )
                        if ok:
                            print(f"    -> Back-ref written to supplier entity")
                            stats["supplier_meta_updated"] += 1
                        else:
                            print(f"    -> ERROR writing back-ref")
                            stats["errors"] += 1
                    else:
                        print(f"    -> Back-ref already present, skipping")
            else:
                print(f"    -> [dry-run] Would write back-ref to supplier entity")

            stats["total_relationships"] += 1

        # Process customers
        customers_resolved = []
        for cust in relationships.get("customers", []):
            cust_name = cust["name"]
            print(f"\n  [CUSTOMER] {cust_name}")

            # Try to find existing entity
            existing = find_entity_by_name(cust_name)
            if existing:
                cust_id = existing["id"]
                print(f"    Found existing: {cust_id[:8]}...")
                stats["entities_found_existing"] += 1
            else:
                print(f"    Not found — {'would create' if args.dry_run else 'creating'}...")
                if not args.dry_run:
                    created = create_entity(
                        name=cust_name,
                        tags=cust["tags"],
                        notes=cust.get("notes", ""),
                    )
                    if not created:
                        print(f"    ERROR creating entity")
                        stats["errors"] += 1
                        continue
                    cust_id = created["id"]
                    print(f"    Created: {cust_id[:8]}...")
                    stats["entities_created"] += 1
                else:
                    cust_id = "[dry-run-id]"
                    stats["entities_created"] += 1

            customers_resolved.append({**cust, "kissinger_id": cust_id})

            # Add back-ref to customer entity
            if not args.dry_run and cust_id != "[dry-run-id]":
                customer_full = get_entity_full(cust_id)
                if customer_full:
                    existing_meta_keys = {m["key"] for m in (customer_full.get("meta") or [])}
                    existing_tags = customer_full.get("tags") or []

                    known_suppliers_entry = json.dumps([{
                        "name": seed_name,
                        "kissinger_id": seed_id,
                        "relationship_type": cust["relationship_type"],
                        "confidence": cust["confidence"],
                        "source": cust["source"],
                    }])

                    new_meta_for_customer = []
                    supplier_meta_key = f"known_suppliers_of_{seed_id[:8]}"
                    if supplier_meta_key not in existing_meta_keys:
                        new_meta_for_customer.append({
                            "key": supplier_meta_key,
                            "value": known_suppliers_entry,
                        })
                    if "_prov_supply_chain_bootstrap" not in existing_meta_keys:
                        new_meta_for_customer.append({
                            "key": "_prov_supply_chain_bootstrap",
                            "value": now_iso,
                        })

                    new_tags_for_customer = []
                    relationship_tag = f"supplier_of:{seed_id}"
                    if relationship_tag not in existing_tags:
                        new_tags_for_customer.append(relationship_tag)
                    if "customer" not in existing_tags:
                        new_tags_for_customer.append("customer")

                    if new_meta_for_customer or new_tags_for_customer:
                        ok = update_entity_meta_and_tags(
                            cust_id,
                            existing_tags,
                            new_tags_for_customer,
                            new_meta_for_customer,
                        )
                        if ok:
                            print(f"    -> Back-ref written to customer entity")
                            stats["supplier_meta_updated"] += 1
                        else:
                            print(f"    -> ERROR writing back-ref")
                            stats["errors"] += 1
                    else:
                        print(f"    -> Back-ref already present, skipping")
            else:
                print(f"    -> [dry-run] Would write back-ref to customer entity")

            stats["total_relationships"] += 1

        # Now update the seed entity with known_suppliers and known_customers
        print(f"\n  [META] Updating seed entity with supply chain meta...")
        seed_full = get_entity_full(seed_id) if not args.dry_run else {"meta": [], "tags": seed_entity.get("tags", [])}

        if seed_full:
            existing_seed_meta_keys = {m["key"] for m in (seed_full.get("meta") or [])}

            new_seed_meta = []
            if suppliers_resolved:
                known_suppliers_json = build_known_suppliers_json(suppliers_resolved)
                if "known_suppliers" not in existing_seed_meta_keys:
                    new_seed_meta.append({"key": "known_suppliers", "value": known_suppliers_json})
                    print(f"    + known_suppliers: {len(suppliers_resolved)} entries")
                else:
                    print(f"    known_suppliers already set, skipping")

            if customers_resolved:
                known_customers_json = build_known_customers_json(customers_resolved)
                if "known_customers" not in existing_seed_meta_keys:
                    new_seed_meta.append({"key": "known_customers", "value": known_customers_json})
                    print(f"    + known_customers: {len(customers_resolved)} entries")
                else:
                    print(f"    known_customers already set, skipping")

            # Add buys_from / supplies_to ID lists
            if suppliers_resolved:
                supplier_ids = ",".join(
                    s["kissinger_id"] for s in suppliers_resolved
                    if s.get("kissinger_id") and s["kissinger_id"] != "[dry-run-id]"
                )
                if supplier_ids and "buys_from" not in existing_seed_meta_keys:
                    new_seed_meta.append({"key": "buys_from", "value": supplier_ids})
                    print(f"    + buys_from: {supplier_ids[:60]}...")

            if customers_resolved:
                customer_ids = ",".join(
                    c["kissinger_id"] for c in customers_resolved
                    if c.get("kissinger_id") and c["kissinger_id"] != "[dry-run-id]"
                )
                if customer_ids and "supplies_to" not in existing_seed_meta_keys:
                    new_seed_meta.append({"key": "supplies_to", "value": customer_ids})
                    print(f"    + supplies_to: {customer_ids[:60]}...")

            if "_prov_supply_chain_bootstrap" not in existing_seed_meta_keys:
                new_seed_meta.append({"key": "_prov_supply_chain_bootstrap", "value": now_iso})

            if new_seed_meta and not args.dry_run:
                seed_tags = seed_full.get("tags") or []
                ok = update_entity_meta_and_tags(seed_id, seed_tags, [], new_seed_meta)
                if ok:
                    print(f"    -> Seed meta written OK")
                    stats["seed_meta_updated"] += 1
                else:
                    print(f"    -> ERROR writing seed meta")
                    stats["errors"] += 1
            elif args.dry_run and new_seed_meta:
                print(f"    -> [dry-run] Would write {len(new_seed_meta)} meta fields to seed")
                stats["seed_meta_updated"] += 1
            else:
                print(f"    -> No new seed meta needed")

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"  Seeds processed:           {stats['seeds_processed']}")
    print(f"  Total relationships:       {stats['total_relationships']}")
    print(f"  New entities {'would create' if args.dry_run else 'created'}:    {stats['entities_created']}")
    print(f"  Existing entities found:   {stats['entities_found_existing']}")
    print(f"  Seed meta {'would update' if args.dry_run else 'updated'}:      {stats['seed_meta_updated']}")
    print(f"  Supplier/customer meta {'would update' if args.dry_run else 'updated'}: {stats['supplier_meta_updated']}")
    if not args.dry_run:
        print(f"  Errors:                    {stats['errors']}")
    if args.dry_run:
        print()
        print("  DRY RUN — no changes written. Re-run without --dry-run to apply.")
    print()


if __name__ == "__main__":
    main()
