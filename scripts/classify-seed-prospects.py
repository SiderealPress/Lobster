#!/usr/bin/env python3
"""
classify-seed-prospects.py — BIS-335

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

import argparse
import json
import urllib.request
import sys
from datetime import datetime, timezone

KISSINGER_URL = "http://localhost:8080/graphql"
SCRIPT_VERSION = "1.0.0"
SCRIPT_NAME = "classify-seed-prospects.py"

# ---------------------------------------------------------------------------
# Canonical classification data for the 30 seed prospects
# Keyed by Kissinger entity name (exact match).
# ---------------------------------------------------------------------------
COMPANY_CLASSIFICATIONS = {
    "Textron Systems": {
        "verticals": ["vertical:defense", "vertical:aerospace"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Hunt Valley, MD, USA",
        "revenue_estimate": "$1.0B-$1.5B (systems div est.)",
        "employee_count": "~3,000-4,000",
        "erp_system": "Oracle",
        "key_challenge": "Multi-program defense backlog tracking; delivery-based revenue recognition (ASC 606); supplier traceability under DFARS",
        "economic_buyer_title": "VP Supply Chain / CSCO",
        "icp_score": "88",
    },
    "General Atomics Aeronautical Systems (GA-ASI)": {
        "verticals": ["vertical:defense", "vertical:aerospace"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Poway, CA, USA",
        "revenue_estimate": "$2B+ (est., private)",
        "employee_count": "~6,000",
        "erp_system": "SAP (General Atomics enterprise)",
        "key_challenge": "ITAR-restricted component sourcing; long-cycle drone production backlog; DoD delivery milestone tracking",
        "economic_buyer_title": "VP Supply Chain / Director of Operations",
        "icp_score": "85",
    },
    "Skydio": {
        "verticals": ["vertical:defense", "vertical:aerospace"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "San Mateo, CA, USA",
        "revenue_estimate": "$100M-$300M (est., private)",
        "employee_count": "~400-600",
        "erp_system": "NetSuite (est.)",
        "key_challenge": "Component shortages (semiconductors, imaging sensors); drone supply chain traceability for DoD compliance",
        "economic_buyer_title": "VP Operations / Head of Supply Chain",
        "icp_score": "78",
    },
    "Joby Aviation": {
        "verticals": ["vertical:aerospace", "vertical:ev"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Santa Cruz, CA, USA",
        "revenue_estimate": "$0 revenue (pre-revenue); ~$800M raised",
        "employee_count": "~1,200",
        "erp_system": "Unknown (pre-revenue startup)",
        "key_challenge": "Novel eVTOL supply chain buildout; FAA certification-driven component traceability; long-lead aerospace parts",
        "economic_buyer_title": "VP Operations / Chief Supply Chain Officer",
        "icp_score": "72",
    },
    "Panasonic Energy of North America (PENA)": {
        "verticals": ["vertical:ev", "vertical:capital_goods"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Sparks, NV, USA (Gigafactory Nevada)",
        "revenue_estimate": "$3B+ (est., Panasonic Energy segment)",
        "employee_count": "~4,000-5,000 (Sparks campus)",
        "erp_system": "SAP (Panasonic global)",
        "key_challenge": "Lithium/cobalt raw material sourcing volatility; battery cell supply chain traceability for IRA compliance; multi-shift gigafactory demand planning",
        "economic_buyer_title": "VP Supply Chain / Head of Procurement",
        "icp_score": "80",
    },
    "Cognex Corporation": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Natick, MA, USA",
        "revenue_estimate": "$800M-$1B",
        "employee_count": "~2,000",
        "erp_system": "SAP",
        "key_challenge": "Semiconductor component sourcing; hardware BOM complexity across vision system product lines; channel inventory management",
        "economic_buyer_title": "VP Operations / Director of Supply Chain",
        "icp_score": "68",
    },
    "Zebra Technologies": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Lincolnshire, IL, USA",
        "revenue_estimate": "$4.5B-$5B",
        "employee_count": "~10,000",
        "erp_system": "SAP",
        "key_challenge": "Hardware supply chain for scanners/printers amid chip shortages; demand planning across OEM + channel; backlog management for enterprise hardware",
        "economic_buyer_title": "CSCO / VP Supply Chain",
        "icp_score": "65",
    },
    "Teradyne (Robotics Division)": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "North Reading, MA, USA",
        "revenue_estimate": "$900M-$1.2B (robotics + industrial automation segment)",
        "employee_count": "~2,000-3,000 (robotics)",
        "erp_system": "Oracle",
        "key_challenge": "Robot component sourcing (servo motors, vision systems); backlog management for MiR/UR orders; collaborative robot BOM complexity",
        "economic_buyer_title": "VP Operations / VP Supply Chain",
        "icp_score": "70",
    },
    "AeroVironment": {
        "verticals": ["vertical:defense", "vertical:aerospace"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Arlington, VA, USA",
        "revenue_estimate": "$500M-$700M",
        "employee_count": "~1,000-1,500",
        "erp_system": "Costpoint (Deltek)",
        "key_challenge": "ITAR-controlled drone components; delivery-milestone revenue recognition; sole-source supplier concentration risk for small UAS",
        "economic_buyer_title": "VP Supply Chain / Director of Procurement",
        "icp_score": "82",
    },
    "Kratos Defense & Security Solutions": {
        "verticals": ["vertical:defense", "vertical:aerospace"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "San Diego, CA, USA",
        "revenue_estimate": "$850M-$1B",
        "employee_count": "~3,500-4,000",
        "erp_system": "Costpoint (Deltek)",
        "key_challenge": "Target drone and satellite production backlog; multi-program DFARS traceability; government contract milestone billing vs. delivery",
        "economic_buyer_title": "VP Supply Chain / Chief Operating Officer",
        "icp_score": "84",
    },
    "Shield AI": {
        "verticals": ["vertical:defense"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "San Diego, CA, USA",
        "revenue_estimate": "$200M-$400M (est., private; ~$2.7B valuation)",
        "employee_count": "~700-900",
        "erp_system": "Unknown (pre-scale startup)",
        "key_challenge": "Autonomous systems hardware supply chain; ITAR compliance for AI-driven defense platforms; scaling from prototype to production",
        "economic_buyer_title": "VP Operations / Head of Supply Chain",
        "icp_score": "76",
    },
    "Anduril Industries": {
        "verticals": ["vertical:defense"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Costa Mesa, CA, USA",
        "revenue_estimate": "$500M-$1B (est., private; ~$14B valuation)",
        "employee_count": "~3,000-4,000",
        "erp_system": "Custom / NetSuite (est.)",
        "key_challenge": "Lattice sensor/electronics supply chain at scale; ITAR-controlled components; rapid multi-platform production ramp for DoD contracts",
        "economic_buyer_title": "VP Supply Chain / Chief Operating Officer",
        "icp_score": "79",
    },
    "Mueller Water Products": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Atlanta, GA, USA",
        "revenue_estimate": "$300M-$450M",
        "employee_count": "~1,700-2,000",
        "erp_system": "SAP",
        "key_challenge": "Cast iron and brass sourcing volatility; lead time variability for municipal water infrastructure orders; backlog management for large utility contracts",
        "economic_buyer_title": "VP Supply Chain / VP Operations",
        "icp_score": "62",
    },
    "Watts Water (acquired Haws Corp)": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "North Andover, MA, USA",
        "revenue_estimate": "$200M-$400M (Haws segment est.)",
        "employee_count": "~1,500-2,500",
        "erp_system": "SAP",
        "key_challenge": "Emergency safety equipment sourcing; regulatory compliance for plumbing and water treatment products; multi-region manufacturing coordination",
        "economic_buyer_title": "VP Supply Chain / Director of Operations",
        "icp_score": "60",
    },
    "Wabash National": {
        "verticals": ["vertical:rail"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Lafayette, IN, USA",
        "revenue_estimate": "$2B-$2.5B",
        "employee_count": "~6,500-7,500",
        "erp_system": "SAP",
        "key_challenge": "Steel and aluminum sourcing for trailer manufacturing; cyclical demand planning across transportation equipment; backlog-to-production scheduling",
        "economic_buyer_title": "VP Supply Chain / CSCO",
        "icp_score": "75",
    },
    "GATX Corporation": {
        "verticals": ["vertical:rail"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Chicago, IL, USA",
        "revenue_estimate": "$1.4B-$1.6B",
        "employee_count": "~1,800-2,200",
        "erp_system": "Oracle",
        "key_challenge": "Tank car lifecycle management and maintenance parts sourcing; railcar fleet backlog; multi-country (US, Europe, India) maintenance network coordination",
        "economic_buyer_title": "VP Supply Chain / VP Fleet Management",
        "icp_score": "73",
    },
    "Crane NXT Co.": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Stamford, CT, USA",
        "revenue_estimate": "$600M-$800M",
        "employee_count": "~3,500-4,500",
        "erp_system": "SAP",
        "key_challenge": "Currency authentication technology components; security printing supply chain; hardware/software integrated product backlog management",
        "economic_buyer_title": "VP Supply Chain / VP Operations",
        "icp_score": "65",
    },
    "Circor International": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Burlington, MA, USA",
        "revenue_estimate": "$400M-$600M",
        "employee_count": "~3,000-4,000",
        "erp_system": "Oracle",
        "key_challenge": "Precision flow control components across aerospace/industrial; multi-plant sourcing coordination; backlog management for long-lead engineered products",
        "economic_buyer_title": "VP Supply Chain / Director of Procurement",
        "icp_score": "66",
    },
    "Thermon Group Holdings": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "San Marcos, TX, USA",
        "revenue_estimate": "$350M-$500M",
        "employee_count": "~1,600-2,000",
        "erp_system": "SAP",
        "key_challenge": "Industrial heating cable components; multi-country project-based revenue recognition; energy sector project backlog management",
        "economic_buyer_title": "VP Operations / Director of Supply Chain",
        "icp_score": "64",
    },
    "Trex Company": {
        "verticals": ["vertical:building_products"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Winchester, VA, USA",
        "revenue_estimate": "$900M-$1.1B",
        "employee_count": "~1,800-2,200",
        "erp_system": "SAP",
        "key_challenge": "Recycled plastic/wood fiber sourcing variability; seasonal demand swings for decking products; retailer channel inventory management",
        "economic_buyer_title": "VP Supply Chain / VP Manufacturing",
        "icp_score": "60",
    },
    "Insteel Industries": {
        "verticals": ["vertical:building_products"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Mount Airy, NC, USA",
        "revenue_estimate": "$600M-$800M",
        "employee_count": "~2,000-2,500",
        "erp_system": "SAP",
        "key_challenge": "Steel wire rod input cost volatility; infrastructure spending cycle demand planning; multi-plant production scheduling for prestressed concrete strand",
        "economic_buyer_title": "VP Operations / Director of Procurement",
        "icp_score": "61",
    },
    "Hexion Inc.": {
        "verticals": ["vertical:chemicals"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Columbus, OH, USA",
        "revenue_estimate": "$2B-$2.5B",
        "employee_count": "~3,500-4,500",
        "erp_system": "SAP",
        "key_challenge": "Phenol/formaldehyde feedstock sourcing; multi-site resin production planning; ESG reporting for specialty chemicals under IFRS/SEC rules",
        "economic_buyer_title": "VP Supply Chain / VP Procurement",
        "icp_score": "74",
    },
    "Cabot Corporation": {
        "verticals": ["vertical:chemicals"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Boston, MA, USA",
        "revenue_estimate": "$3.5B-$4B",
        "employee_count": "~2,800-3,200",
        "erp_system": "SAP",
        "key_challenge": "Carbon black and specialty fluids feedstock sourcing across 20+ countries; ESG/Scope 3 supply chain transparency; demand planning for specialty carbon products",
        "economic_buyer_title": "VP Supply Chain / Chief Procurement Officer",
        "icp_score": "75",
    },
    "Watts Water Technologies": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "North Andover, MA, USA",
        "revenue_estimate": "$1.8B-$2.0B",
        "employee_count": "~4,500-5,500",
        "erp_system": "SAP",
        "key_challenge": "Multi-country plumbing/flow control sourcing (US, Europe, China); regulatory compliance for water products; backlog management for infrastructure projects",
        "economic_buyer_title": "CSCO / VP Supply Chain",
        "icp_score": "70",
    },
    "Mueller Industries": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Memphis, TN, USA",
        "revenue_estimate": "$3.5B-$4.0B",
        "employee_count": "~5,000-6,000",
        "erp_system": "Oracle",
        "key_challenge": "Copper and brass rod sourcing cost volatility; HVAC/plumbing component demand planning cycles; multi-plant global manufacturing coordination",
        "economic_buyer_title": "CSCO / VP Supply Chain",
        "icp_score": "76",
    },
    "Chart Industries": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:moderate",
        "hq_location": "Ball Ground, GA, USA",
        "revenue_estimate": "$4B-$4.5B (post-Howden acquisition)",
        "employee_count": "~11,000-12,000",
        "erp_system": "Oracle / SAP (multi-instance post-M&A)",
        "key_challenge": "Post-acquisition ERP integration (Howden); cryogenic equipment project backlog; global sourcing for specialized heat exchangers and industrial gases equipment",
        "economic_buyer_title": "CSCO / VP Supply Chain",
        "icp_score": "77",
    },
    "American Axle & Manufacturing (AAM)": {
        "verticals": ["vertical:capital_goods"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Detroit, MI, USA",
        "revenue_estimate": "$5.5B-$6.0B",
        "employee_count": "~23,000-25,000",
        "erp_system": "SAP",
        "key_challenge": "Automotive Tier 1 just-in-time supply chain; steel/aluminum input cost exposure; EV driveline transition disrupting traditional ICE backlog",
        "economic_buyer_title": "CSCO / VP Purchasing",
        "icp_score": "74",
    },
    "Moog Inc.": {
        "verticals": ["vertical:defense", "vertical:aerospace"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Elma, NY, USA",
        "revenue_estimate": "$3.5B-$4.0B",
        "employee_count": "~12,000-13,000",
        "erp_system": "Unknown (VeriPart blockchain pilot confirmed)",
        "key_challenge": "26-country precision component sourcing; DFARS/ITAR traceability requirements; VP Supply Chain publicly focused on AI-driven planning transformation",
        "economic_buyer_title": "VP Central Supply Chain",
        "icp_score": "87",
    },
    "FreightCar America": {
        "verticals": ["vertical:rail"],
        "size": "size:mid_market",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Chicago, IL, USA",
        "revenue_estimate": "$300M-$500M",
        "employee_count": "~1,000-2,000",
        "erp_system": "Oracle (est.)",
        "key_challenge": "Steel plate and wheel/axle sourcing for railcar manufacturing; cyclical demand from freight rail operators; backlog-based revenue recognition",
        "economic_buyer_title": "VP Supply Chain / VP Manufacturing",
        "icp_score": "78",
    },
    "The Greenbrier Companies": {
        "verticals": ["vertical:rail"],
        "size": "size:enterprise",
        "supply_chain": "supply_chain:complex",
        "hq_location": "Lake Oswego, OR, USA",
        "revenue_estimate": "$3.0B-$3.5B",
        "employee_count": "~14,000-16,000",
        "erp_system": "Oracle (est.)",
        "key_challenge": "Multi-country railcar manufacturing (US/Mexico/Europe); steel/component sourcing; ISSB/IFRS sustainability supply chain disclosure; 33 repair locations demand planning",
        "economic_buyer_title": "VP Supply Chain / Chief Operating Officer",
        "icp_score": "85",
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
        "source": "eloso-prospects-v2",
        "_prov_imported_by": "classify-seed-prospects.py",
        "_prov_source": "eloso-ontology-v1",
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
    print("Eloso Seed Prospect Classification — BIS-335")
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
