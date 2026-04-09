#!/usr/bin/env python3
"""
bootstrap-supply-chain-graph.py — BIS-338

Bootstraps supply chain relationships for Eloso seed prospects by:
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

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

KISSINGER_URL = "http://localhost:8080/graphql"
SCRIPT_VERSION = "1.0.0"
SCRIPT_NAME = "bootstrap-supply-chain-graph.py"

# ---------------------------------------------------------------------------
# Known supply chain relationships
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
SUPPLY_CHAIN_RELATIONSHIPS = {
    "The Greenbrier Companies": {
        "suppliers": [
            {
                "name": "Nucor Corporation",
                "relationship_type": "steel_supplier",
                "confidence": "high",
                "source": "Public industry knowledge — Nucor is largest US steel supplier to railcar manufacturers; Greenbrier 10-K cites steel as primary input cost",
                "tags": ["supplier", "vertical:capital_goods"],
                "notes": "Largest domestic steel producer; primary supplier of hot-rolled coil and plate steel to North American railcar manufacturers including Greenbrier. ~$37B revenue.",
            },
            {
                "name": "Steel Technologies (METALS USA)",
                "relationship_type": "steel_service_center",
                "confidence": "medium",
                "source": "Industry knowledge — steel service centers are standard intermediate in railcar manufacturing supply chain",
                "tags": ["supplier", "vertical:capital_goods"],
                "notes": "Steel service center and processor; supplies processed steel to railcar manufacturers. Part of Metals USA group.",
            },
            {
                "name": "Wabtec Corporation",
                "relationship_type": "railcar_component_supplier",
                "confidence": "high",
                "source": "Public industry knowledge — Wabtec is dominant supplier of braking systems, couplers, and other railcar components to all major railcar OEMs",
                "tags": ["supplier", "vertical:rail", "vertical:capital_goods"],
                "notes": "Leading supplier of freight car components: brakes, couplers, draft gear, trucks. ~$9B revenue. Supplies all major railcar manufacturers.",
            },
            {
                "name": "Amsted Rail",
                "relationship_type": "railcar_components_supplier",
                "confidence": "high",
                "source": "Industry knowledge — Amsted Rail (private) supplies wheels, axles, bearings to railcar manufacturers",
                "tags": ["supplier", "vertical:rail"],
                "notes": "Private supplier of railcar wheels, axles, and roller bearings. Major supplier to North American railcar builders.",
            },
        ],
        "customers": [
            {
                "name": "BNSF Railway",
                "relationship_type": "railcar_customer",
                "confidence": "high",
                "source": "Public — BNSF is one of the largest purchasers of new railcars in North America; Greenbrier 10-K references Class I railroad customers",
                "tags": ["customer", "vertical:rail"],
                "notes": "Class I railroad, one of Greenbrier's major fleet purchaser customers. ~$23B revenue.",
            },
            {
                "name": "Union Pacific Railroad",
                "relationship_type": "railcar_customer",
                "confidence": "high",
                "source": "Public — Union Pacific regularly orders new railcars; Greenbrier is a major supplier to Class I railroads",
                "tags": ["customer", "vertical:rail"],
                "notes": "Class I railroad and major railcar fleet purchaser. ~$24B revenue.",
            },
            {
                "name": "GATX Corporation",
                "relationship_type": "railcar_leasing_customer",
                "confidence": "high",
                "source": "Industry knowledge — GATX is a major railcar lessor and purchaser from Greenbrier",
                "tags": ["customer", "vertical:rail", "prospect", "eloso"],
                "notes": "Railcar leasing company and fleet manager. Also a seed prospect. Purchases railcars from Greenbrier for its leasing fleet.",
            },
        ],
    },

    "FreightCar America": {
        "suppliers": [
            {
                "name": "Nucor Corporation",
                "relationship_type": "steel_supplier",
                "confidence": "high",
                "source": "Public — FreightCar America 10-K cites steel as primary input material; Nucor largest domestic supplier",
                "tags": ["supplier", "vertical:capital_goods"],
                "notes": "Primary steel supplier to FreightCar America for railcar plate and structural steel. Nucor is largest domestic EAF steelmaker.",
            },
            {
                "name": "Wabtec Corporation",
                "relationship_type": "railcar_component_supplier",
                "confidence": "high",
                "source": "Industry standard — Wabtec supplies braking and coupling systems to all NA railcar OEMs",
                "tags": ["supplier", "vertical:rail", "vertical:capital_goods"],
                "notes": "Supplies braking systems, couplers, draft gear to FreightCar America railcar production.",
            },
            {
                "name": "Amsted Rail",
                "relationship_type": "wheels_axles_supplier",
                "confidence": "high",
                "source": "Industry knowledge — Amsted Rail is dominant supplier of wheels/axles to NA railcar manufacturers",
                "tags": ["supplier", "vertical:rail"],
                "notes": "Supplies wheels, axles, and bearings to FreightCar America.",
            },
        ],
        "customers": [
            {
                "name": "BNSF Railway",
                "relationship_type": "railcar_customer",
                "confidence": "medium",
                "source": "Industry knowledge — Class I railroads are primary customers for gondola and open-top hoppers that FreightCar America specializes in",
                "tags": ["customer", "vertical:rail"],
                "notes": "Major Class I railroad customer for coal gondolas and bulk commodity railcars.",
            },
            {
                "name": "Norfolk Southern Railway",
                "relationship_type": "railcar_customer",
                "confidence": "medium",
                "source": "Industry knowledge — FreightCar America primarily serves eastern coal-producing region railroads",
                "tags": ["customer", "vertical:rail"],
                "notes": "Class I railroad; customer for open-top hoppers and gondola cars.",
            },
        ],
    },

    "Textron Systems": {
        "suppliers": [
            {
                "name": "Moog Inc.",
                "relationship_type": "precision_actuation_supplier",
                "confidence": "high",
                "source": "Industry knowledge — Moog is a leading supplier of precision actuation and flight control systems to defense drone manufacturers including Textron Bell",
                "tags": ["supplier", "vertical:defense", "vertical:aerospace"],
                "notes": "Supplies precision actuation, flight control, and electromechanical systems to Textron drone and aircraft programs. Also a seed prospect in Kissinger.",
            },
            {
                "name": "Parker Hannifin",
                "relationship_type": "fluid_power_motion_supplier",
                "confidence": "high",
                "source": "Industry knowledge — Parker Hannifin is a dominant supplier of motion control and fluid power systems to defense aerospace manufacturers",
                "tags": ["supplier", "vertical:defense", "vertical:aerospace", "vertical:capital_goods"],
                "notes": "Supplies hydraulic, pneumatic, and motion control systems for Textron's drone and armored vehicle programs. ~$20B revenue.",
            },
            {
                "name": "TransDigm Group",
                "relationship_type": "aerospace_components_supplier",
                "confidence": "medium",
                "source": "Industry knowledge — TransDigm is a major supplier of highly engineered aerospace components to defense manufacturers",
                "tags": ["supplier", "vertical:defense", "vertical:aerospace"],
                "notes": "Supplies highly engineered aerospace/defense components (actuators, sensors, ignition systems) to Textron defense programs. ~$7B revenue.",
            },
            {
                "name": "L3Harris Technologies",
                "relationship_type": "sensors_electronics_supplier",
                "confidence": "medium",
                "source": "Industry knowledge — L3Harris is a leading supplier of sensors, EO/IR systems, and communications to drone/UAS manufacturers",
                "tags": ["supplier", "vertical:defense"],
                "notes": "Supplies EO/IR sensors, communications systems, and electronic warfare components to Textron drone programs.",
            },
        ],
        "customers": [
            {
                "name": "U.S. Army (DoD)",
                "relationship_type": "primary_government_customer",
                "confidence": "high",
                "source": "Public — Textron Systems' Shadow and Aerosonde drones are US Army programs of record",
                "tags": ["customer"],
                "notes": "Primary customer for Shadow TUAS and Aerosonde drone systems under US Army contracts.",
            },
        ],
    },

    "Moog Inc.": {
        "suppliers": [
            {
                "name": "Parker Hannifin",
                "relationship_type": "components_competitor_supplier",
                "confidence": "medium",
                "source": "Industry knowledge — Parker and Moog compete but Parker also supplies some components to Moog's non-competing segments",
                "tags": ["supplier", "vertical:defense", "vertical:aerospace", "vertical:capital_goods"],
                "notes": "Complex relationship — competitor in motion control but also potential supplier of sub-components to Moog's industrial segment.",
            },
            {
                "name": "TE Connectivity",
                "relationship_type": "connectors_sensors_supplier",
                "confidence": "high",
                "source": "Industry knowledge — TE Connectivity is a dominant supplier of connectors and sensors to aerospace/defense manufacturers",
                "tags": ["supplier", "vertical:capital_goods"],
                "notes": "Supplies precision connectors, sensors, and data connectivity components to Moog's aerospace and defense product lines.",
            },
            {
                "name": "Curtiss-Wright Corporation",
                "relationship_type": "defense_components_supplier",
                "confidence": "medium",
                "source": "Industry knowledge — Curtiss-Wright supplies defense-grade electronics and components to precision motion control manufacturers",
                "tags": ["supplier", "vertical:defense"],
                "notes": "Supplies defense-hardened electronics and actuation components used in Moog's defense programs.",
            },
        ],
        "customers": [
            {
                "name": "Boeing",
                "relationship_type": "aerospace_oem_customer",
                "confidence": "high",
                "source": "Public — Moog supplies flight control actuation systems to Boeing commercial and defense programs",
                "tags": ["customer", "vertical:aerospace"],
                "notes": "Major customer for Moog's flight control actuation systems across commercial (737, 787) and defense (F/A-18) platforms.",
            },
            {
                "name": "Lockheed Martin",
                "relationship_type": "defense_oem_customer",
                "confidence": "high",
                "source": "Public — Moog supplies to F-35 and other Lockheed programs",
                "tags": ["customer", "vertical:defense", "vertical:aerospace"],
                "notes": "Key defense customer for Moog's precision actuation, flight control, and missile systems components.",
            },
            {
                "name": "Raytheon Technologies (RTX)",
                "relationship_type": "defense_oem_customer",
                "confidence": "high",
                "source": "Public — Moog supplies to multiple Raytheon/RTX missile and aircraft programs",
                "tags": ["customer", "vertical:defense", "vertical:aerospace"],
                "notes": "Customer for Moog's missile actuation and precision motion control systems across Raytheon/Collins programs.",
            },
        ],
    },

    "AeroVironment": {
        "suppliers": [
            {
                "name": "Vicor Corporation",
                "relationship_type": "power_electronics_supplier",
                "confidence": "medium",
                "source": "Industry knowledge — Vicor is a leading supplier of power modules for small UAS and defense electronics",
                "tags": ["supplier", "vertical:capital_goods"],
                "notes": "Supplies high-density power conversion modules for AeroVironment's small UAS platforms.",
            },
            {
                "name": "Teledyne FLIR",
                "relationship_type": "eo_ir_sensor_supplier",
                "confidence": "high",
                "source": "Industry knowledge — Teledyne FLIR is the dominant supplier of small EO/IR sensors used in tactical small UAS like Raven and Puma",
                "tags": ["supplier", "vertical:defense"],
                "notes": "Supplies lightweight EO/IR sensors for AeroVironment's small UAS payload systems (Raven, Puma, Shrike).",
            },
            {
                "name": "Maxon Group",
                "relationship_type": "precision_motor_supplier",
                "confidence": "medium",
                "source": "Industry knowledge — Maxon precision motors are widely used in small UAS propulsion and control systems",
                "tags": ["supplier", "vertical:capital_goods"],
                "notes": "Swiss manufacturer of precision DC and BLDC motors used in AeroVironment's small drone propulsion and gimbal systems.",
            },
        ],
        "customers": [
            {
                "name": "U.S. Army (DoD)",
                "relationship_type": "primary_government_customer",
                "confidence": "high",
                "source": "Public — AeroVironment is the sole-source supplier for Raven, Puma, and Wasp small UAS programs",
                "tags": ["customer"],
                "notes": "Primary customer. AeroVironment holds the Army's small UAS programs of record (Raven B, Puma AE, Wasp III).",
            },
        ],
    },

    "Anduril Industries": {
        "suppliers": [
            {
                "name": "NVIDIA Corporation",
                "relationship_type": "ai_compute_supplier",
                "confidence": "high",
                "source": "Public — Anduril's Lattice AI platform uses GPU-based compute; NVIDIA is the dominant supplier",
                "tags": ["supplier", "vertical:capital_goods"],
                "notes": "Supplies GPU compute (Jetson, A100/H100 class) for Anduril's Lattice AI platform and autonomous systems processing.",
            },
            {
                "name": "Teledyne FLIR",
                "relationship_type": "sensor_supplier",
                "confidence": "high",
                "source": "Industry knowledge — FLIR sensors are used in Anduril's Sentry tower and drone detection systems",
                "tags": ["supplier", "vertical:defense"],
                "notes": "Supplies thermal and EO/IR sensors for Anduril's Sentry autonomous surveillance towers and Ghost drone systems.",
            },
            {
                "name": "General Dynamics Mission Systems",
                "relationship_type": "secure_comms_supplier",
                "confidence": "medium",
                "source": "Industry knowledge — GDMS supplies Type 1 encryption and secure communications hardware to defense contractors",
                "tags": ["supplier", "vertical:defense"],
                "notes": "Potential supplier of NSA-certified cryptographic and secure communications modules for Anduril's defense platforms.",
            },
        ],
        "customers": [
            {
                "name": "U.S. Customs and Border Protection (CBP)",
                "relationship_type": "government_customer",
                "confidence": "high",
                "source": "Public — Anduril won the CBP Autonomous Surveillance Tower contract",
                "tags": ["customer"],
                "notes": "Customer for Anduril's Sentry autonomous surveillance towers deployed at US-Mexico border.",
            },
            {
                "name": "U.S. Special Operations Command (SOCOM)",
                "relationship_type": "government_customer",
                "confidence": "high",
                "source": "Public — Anduril has multiple SOCOM contracts for Ghost drone and Altius loitering munitions",
                "tags": ["customer"],
                "notes": "Key customer for Anduril's Ghost UAS and Altius loitering munition systems.",
            },
        ],
    },
}


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
            "tags": tags + ["eloso"],
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
        description="Bootstrap supply chain graph relationships for Eloso seed prospects"
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
    print("Eloso Supply Chain Graph Bootstrap — BIS-338")
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
