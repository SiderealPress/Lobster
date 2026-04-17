"""
Clay.com Enrichment Script for Kissinger CRM
============================================

Fills gaps that Apollo enrichment couldn't fill. Targets:
  - Contacts missing email
  - Contacts missing LinkedIn URL
  - Contacts missing company/org info

Clay runs waterfall enrichment across 100+ sources (LinkedIn, Apollo, Clearbit,
Hunter, PDL, etc.) achieving ~95% email find rate. It is run AFTER Apollo,
picking up what Apollo missed.

Epistemic standards:
  - Clay-sourced data: tag `clay-enriched` on entity, provenance `clay`
  - Confidence: 0.75 (Apollo is 0.80 — waterfall aggregation uses secondary sources)
  - If Clay conflicts with existing Apollo data: keep Apollo, log discrepancy
  - If Clay fills a gap Apollo didn't: merge in, note source
  - Inferred colleague edges from Clay: inferred=true, how_they_know="Colleague inference (Clay)"

Usage:
    # Set CLAY_API_KEY in environment or ~/lobster-config/config.env
    python3 ~/lobster/src/integrations/clay/enrich.py [--dry-run] [--limit N] [--since-hours N]

    # Smoke test (one lookup to verify API key works)
    python3 ~/lobster/src/integrations/clay/enrich.py --smoke-test

Requirements:
    pip install requests  (already present in prospect-enrichment pipeline)

CLAY_API_KEY must be set (already in ~/lobster-config/config.env).

Exit codes:
    0 — completed (possibly with non-fatal errors)
    1 — fatal failure (no API key, Kissinger unreachable)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ── Path setup ─────────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
_SRC_DIR  = _THIS_DIR.parent.parent  # ~/lobster/src
sys.path.insert(0, str(_SRC_DIR))

from integrations.clay.client import ClayClient, ClayError, ClayPlanError, CLAY_CONFIDENCE, CLAY_TAG, CLAY_PROV_SOURCE

# ── Configuration ──────────────────────────────────────────────────────────────

KISSINGER_BIN      = str(Path.home() / "lobster-workspace/projects/kissinger/target/release/kissinger")
KISSINGER_DB       = os.environ.get("KISSINGER_DB", str(Path.home() / ".kissinger/graph.db"))
KISSINGER_ENDPOINT = os.environ.get("KISSINGER_ENDPOINT", "http://localhost:8080/graphql")
KISSINGER_API_TOKEN= os.environ.get("KISSINGER_API_TOKEN", "")
CONFIG_ENV         = str(Path.home() / "lobster-config/config.env")

# Apollo enrichment tag — contacts with this tag were already enriched by Apollo
APOLLO_ENRICHED_TAG = "apollo-enriched-paid"

# Only gap-fill these fields (don't overwrite fields Apollo already set)
APOLLO_OWNED_FIELDS = frozenset({
    "email", "phone", "linkedin_url", "title", "org",
    "city", "state", "country", "seniority", "departments",
})

# ── GraphQL helpers ────────────────────────────────────────────────────────────

def _gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Execute a GraphQL query against Kissinger."""
    try:
        import urllib.request
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if KISSINGER_API_TOKEN:
            headers["Authorization"] = f"Bearer {KISSINGER_API_TOKEN}"
        req = urllib.request.Request(
            KISSINGER_ENDPOINT,
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"[clay] GraphQL error: {exc}", file=sys.stderr)
        raise

    if "errors" in data and data["errors"]:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data.get("data", {})


_UPDATE_ENTITY_MUTATION = """
mutation UpdateEntity($id: String!, $input: UpdateEntityInput!) {
  updateEntity(id: $id, input: $input) {
    id name tags
    meta { key value }
  }
}
"""

_LOG_CLAIM_MUTATION = """
mutation LogClaim($input: LogClaimInput!) {
  logClaim(input: $input) {
    id fieldName value confidence
  }
}
"""

_REGISTER_SOURCE_MUTATION = """
mutation RegisterSource($input: RegisterSourceInput!) {
  registerSource(input: $input) {
    id name kind reliabilityTier
  }
}
"""

_SOURCES_QUERY = """
query ListSources {
  sources {
    id name kind
  }
}
"""


# ── Source registry ────────────────────────────────────────────────────────────

CLAY_RELIABILITY_TIER = 2  # medium — same tier as Apollo


def ensure_clay_source() -> str:
    """
    Ensure a 'Clay' source entry exists in Kissinger's source registry.
    Idempotent — returns the source ID.
    """
    try:
        data = _gql(_SOURCES_QUERY, {})
        sources = data.get("sources", [])
        for s in sources:
            if s.get("name") == "Clay":
                return s["id"]
    except Exception as exc:
        print(f"  [warn] Could not list sources: {exc}", file=sys.stderr)

    # Create Clay source
    try:
        data = _gql(_REGISTER_SOURCE_MUTATION, {
            "input": {
                "name": "Clay",
                "kind": "api",
                "reliabilityTier": CLAY_RELIABILITY_TIER,
                "url": "https://app.clay.com",
                "notes": (
                    "Clay.com waterfall enrichment — aggregates 100+ sources "
                    "(LinkedIn, Apollo, Clearbit, Hunter, PDL, etc.). "
                    "~95% email find rate. Tier 2 (medium reliability) — data "
                    "freshness 21 days. Used to gap-fill after Apollo."
                ),
            }
        })
        source = data.get("registerSource", {})
        source_id = source.get("id", "clay")
        print(f"  [clay] Registered Clay source in registry (id={source_id})")
        return source_id
    except Exception as exc:
        print(f"  [warn] Could not register Clay source: {exc}. Using synthetic ID.", file=sys.stderr)
        return "clay"


# ── Contact loading ────────────────────────────────────────────────────────────

def load_gap_contacts(since_hours: int | None = None) -> list[dict[str, Any]]:
    """
    Load person entities from Kissinger that have enrichment gaps:
      - missing email, OR
      - missing linkedin_url

    Uses the kissinger CLI export command to get full entity data including meta
    (the GraphQL entities list only returns summaries without meta fields).

    If since_hours is set, also filter to contacts updated in the last N hours
    (for the nightly cron that processes newly added contacts).

    Returns list of entity dicts with keys: id, name, tags, meta (as dict).
    """
    import tempfile
    tmp_file = Path(tempfile.mktemp(suffix=".json"))

    try:
        result = subprocess.run(
            [KISSINGER_BIN, "--db", KISSINGER_DB, "export", "json", str(tmp_file), "--kind", "person"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"[clay] FATAL: kissinger export failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)

        raw_entities = json.loads(tmp_file.read_text())
    except Exception as exc:
        print(f"[clay] FATAL: Could not load entities from Kissinger: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if tmp_file.exists():
            tmp_file.unlink()

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=since_hours) if since_hours else None

    contacts = []
    for entity in raw_entities:
        if entity.get("archived"):
            continue
        if entity.get("kind", "").lower() != "person":
            continue

        # meta may be a dict or a list of {key,value} — normalise to dict
        meta_raw = entity.get("meta", {})
        if isinstance(meta_raw, dict):
            meta = meta_raw
        elif isinstance(meta_raw, list):
            meta = {m["key"]: m["value"] for m in meta_raw if "key" in m}
        else:
            meta = {}

        tags = entity.get("tags", [])

        # Apply time filter if set (use updatedAt as proxy — CLI export may not have createdAt)
        if cutoff:
            ts_str = entity.get("updated_at", entity.get("updatedAt", ""))
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass  # can't parse — include anyway

        # Gap check: missing email OR missing linkedin
        has_email    = bool(meta.get("email", "").strip())
        has_linkedin = bool(meta.get("linkedin_url", "").strip())

        if has_email and has_linkedin:
            continue  # fully enriched — skip

        contacts.append({
            "id":          entity["id"],
            "name":        entity["name"],
            "tags":        tags,
            "meta":        meta,
            "has_email":   has_email,
            "has_linkedin": has_linkedin,
        })

    return contacts


# ── Conflict detection ─────────────────────────────────────────────────────────

def detect_conflicts(
    entity_meta: dict[str, str],
    clay_fields: dict[str, str],
    entity_name: str,
) -> tuple[dict[str, str], list[str]]:
    """
    Compare Clay fields against existing entity meta.

    Rules:
      - If the entity already has a field that was Apollo-sourced (prov_source=apollo):
        keep Apollo, log discrepancy if Clay differs by more than case/whitespace.
      - If the entity has a field from any source and Clay disagrees:
        keep existing, log discrepancy.
      - If the entity is MISSING the field: accept Clay's value.

    Returns:
        (fields_to_merge, conflict_log)
        where fields_to_merge only contains fields that are safe to write,
        and conflict_log contains human-readable descriptions of conflicts.
    """
    fields_to_merge: dict[str, str] = {}
    conflict_log: list[str] = []

    prov_source = entity_meta.get("_prov_source", "")
    is_apollo_enriched = prov_source == "apollo" or "apollo-enriched-paid" in str(
        entity_meta.get("tags", "")
    )

    for field, clay_val in clay_fields.items():
        if not clay_val:
            continue

        existing = entity_meta.get(field, "").strip()

        if not existing:
            # Gap filled — accept Clay's value
            fields_to_merge[field] = clay_val
        else:
            # Field exists — check for conflict
            if existing.lower().strip() != clay_val.lower().strip():
                if is_apollo_enriched and field in APOLLO_OWNED_FIELDS:
                    # Apollo data takes precedence
                    conflict_log.append(
                        f"{entity_name}.{field}: Apollo={repr(existing)} vs Clay={repr(clay_val)} "
                        f"— keeping Apollo (higher confidence)"
                    )
                else:
                    # Non-Apollo source: still keep existing to be conservative
                    conflict_log.append(
                        f"{entity_name}.{field}: existing={repr(existing)} vs Clay={repr(clay_val)} "
                        f"— keeping existing"
                    )
            # Either no conflict (same value) or we're keeping existing — don't merge

    return fields_to_merge, conflict_log


# ── Writing back to Kissinger ──────────────────────────────────────────────────

def write_clay_enrichment(
    entity: dict[str, Any],
    clay_fields: dict[str, str],
    source_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Merge Clay-sourced fields into a Kissinger entity.

    Steps:
      1. Detect conflicts — keep Apollo/existing data, log discrepancies
      2. Build updated meta (existing + Clay gap-fills)
      3. Add `clay-enriched` tag
      4. Set _prov_source = clay for Clay-provided fields
      5. Log provenance claims for each written field
      6. Write via updateEntity mutation

    Returns dict with: fields_written, gaps_filled, conflicts_logged, status
    """
    entity_id   = entity["id"]
    entity_name = entity["name"]
    existing_meta = entity["meta"]
    existing_tags = entity["tags"]

    fields_to_merge, conflicts = detect_conflicts(existing_meta, clay_fields, entity_name)

    if not fields_to_merge:
        return {
            "status": "no_new_data",
            "fields_written": 0,
            "gaps_filled": [],
            "conflicts_logged": conflicts,
        }

    if dry_run:
        gaps = list(fields_to_merge.keys())
        print(f"    [dry-run] Would write {len(fields_to_merge)} field(s): {gaps}")
        if conflicts:
            for c in conflicts:
                print(f"    [dry-run] conflict: {c}")
        return {
            "status": "dry_run",
            "fields_written": len(fields_to_merge),
            "gaps_filled": gaps,
            "conflicts_logged": conflicts,
        }

    # Build new meta: start with existing, merge Clay fields
    new_meta = dict(existing_meta)
    new_meta.update(fields_to_merge)
    new_meta["_prov_source_clay"] = CLAY_PROV_SOURCE
    new_meta["_prov_clay_enriched_at"] = _now_iso()

    # Tags: add clay-enriched (keep existing tags)
    new_tags = list(existing_tags)
    if CLAY_TAG not in new_tags:
        new_tags.append(CLAY_TAG)

    # Write entity update
    meta_input = [{"key": k, "value": v} for k, v in new_meta.items()]
    try:
        _gql(_UPDATE_ENTITY_MUTATION, {
            "id": entity_id,
            "input": {
                "tags": new_tags,
                "meta": meta_input,
            },
        })
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "fields_written": 0,
            "gaps_filled": [],
            "conflicts_logged": conflicts,
        }

    # Log provenance claims for each field Clay provided
    claims_written = 0
    for field_name, value in fields_to_merge.items():
        try:
            _gql(_LOG_CLAIM_MUTATION, {
                "input": {
                    "subjectKind": "entity",
                    "subjectId": entity_id,
                    "fieldName": field_name,
                    "value": value,
                    "sourceId": source_id,
                    "confidence": CLAY_CONFIDENCE,
                    "notes": f"Clay waterfall enrichment — gap-filled (was missing in Apollo)",
                },
            })
            claims_written += 1
        except Exception as exc:
            print(f"    [warn] claim write failed for {field_name}: {exc}", file=sys.stderr)

    gaps_filled = list(fields_to_merge.keys())
    return {
        "status": "written",
        "fields_written": len(fields_to_merge),
        "claims_written": claims_written,
        "gaps_filled": gaps_filled,
        "conflicts_logged": conflicts,
    }


# ── Main enrichment loop ───────────────────────────────────────────────────────

def run_enrichment(
    client: ClayClient,
    source_id: str,
    dry_run: bool = False,
    limit: int | None = None,
    since_hours: int | None = None,
) -> dict[str, Any]:
    """
    Main enrichment loop.

    Loads gap contacts from Kissinger, calls Clay for each, merges results.

    Returns summary dict.
    """
    print(f"\n[clay] Loading gap contacts from Kissinger...")
    contacts = load_gap_contacts(since_hours=since_hours)

    if limit:
        contacts = contacts[:limit]

    print(f"  Found {len(contacts)} contacts with enrichment gaps")
    if not contacts:
        return {
            "status": "ok",
            "total": 0,
            "enriched": 0,
            "gaps_filled": 0,
            "conflicts": 0,
            "errors": 0,
            "no_clay_data": 0,
        }

    stats = {
        "total":        len(contacts),
        "enriched":     0,
        "gaps_filled":  0,
        "conflicts":    0,
        "errors":       0,
        "no_clay_data": 0,
        "dry_run":      dry_run,
        "conflict_log": [],
        "filled_fields": {},  # field_name -> count
    }

    for i, contact in enumerate(contacts, 1):
        name        = contact["name"]
        meta        = contact["meta"]
        has_email   = contact["has_email"]
        has_linkedin = contact["has_linkedin"]

        gaps = []
        if not has_email:    gaps.append("email")
        if not has_linkedin: gaps.append("linkedin")

        print(f"\n  [{i}/{len(contacts)}] {name} — missing: {', '.join(gaps)}")

        # Try Clay lookup in order: email (if we have it) → linkedin (if we have it) → name+org
        clay_person = None
        lookup_method = None

        if has_email and not has_linkedin:
            # We have email but missing LinkedIn — look up by email to get LinkedIn
            email = meta.get("email", "").strip()
            if email:
                print(f"    Trying Clay lookup by email ({email})...")
                try:
                    clay_person = client.lookup_by_email(email)
                    lookup_method = f"email:{email}"
                except ClayPlanError as exc:
                    # Plan limitation — direct API requires Enterprise. Abort the whole run.
                    print(f"[clay] PLAN LIMITATION: {exc}", file=sys.stderr)
                    print("[clay] Stopping enrichment run — direct API unavailable on standard plan.", file=sys.stderr)
                    break
                except ClayError as exc:
                    print(f"    [warn] Clay email lookup failed: {exc}", file=sys.stderr)
                    stats["errors"] += 1
                    continue

        elif has_linkedin and not has_email:
            # We have LinkedIn but missing email — look up by LinkedIn
            linkedin = meta.get("linkedin_url", "").strip()
            if linkedin:
                print(f"    Trying Clay lookup by LinkedIn...")
                try:
                    clay_person = client.lookup_by_linkedin(linkedin)
                    lookup_method = "linkedin"
                except ClayPlanError as exc:
                    print(f"[clay] PLAN LIMITATION: {exc}", file=sys.stderr)
                    print("[clay] Stopping enrichment run — direct API unavailable on standard plan.", file=sys.stderr)
                    break
                except ClayError as exc:
                    print(f"    [warn] Clay LinkedIn lookup failed: {exc}", file=sys.stderr)
                    stats["errors"] += 1
                    continue

        else:
            # Missing both — try name + org
            org = meta.get("org", meta.get("company", meta.get("employer", ""))).strip()
            if not org:
                print(f"    Skipping — no email, no LinkedIn, no org name to search by")
                stats["no_clay_data"] += 1
                continue
            print(f"    Trying Clay lookup by name+org ({org})...")
            try:
                clay_person = client.lookup_by_name(name, company=org)
                lookup_method = f"name:{name}+org:{org}"
            except ClayPlanError as exc:
                print(f"[clay] PLAN LIMITATION: {exc}", file=sys.stderr)
                print("[clay] Stopping enrichment run — direct API unavailable on standard plan.", file=sys.stderr)
                break
            except ClayError as exc:
                print(f"    [warn] Clay name lookup failed: {exc}", file=sys.stderr)
                stats["errors"] += 1
                continue

        if clay_person is None:
            print(f"    No Clay data found (method={lookup_method})")
            stats["no_clay_data"] += 1
            continue

        # Log which sub-sources Clay used
        if clay_person.data_sources:
            print(f"    Clay sources used: {', '.join(clay_person.data_sources)}")

        # Extract fields and write back
        clay_fields = clay_person.to_meta_fields()
        if not clay_fields:
            print(f"    Clay returned person but no useful fields")
            stats["no_clay_data"] += 1
            continue

        result = write_clay_enrichment(contact, clay_fields, source_id, dry_run=dry_run)

        if result["status"] in ("written", "dry_run"):
            stats["enriched"] += 1
            stats["gaps_filled"] += result["fields_written"]
            for f in result.get("gaps_filled", []):
                stats["filled_fields"][f] = stats["filled_fields"].get(f, 0) + 1
            print(f"    OK — wrote {result['fields_written']} field(s): "
                  f"{result.get('gaps_filled', [])}")
        elif result["status"] == "no_new_data":
            print(f"    Clay data matched existing — no new fields to write")
            stats["no_clay_data"] += 1
        else:
            print(f"    Error: {result.get('error', 'unknown')}")
            stats["errors"] += 1

        if result.get("conflicts_logged"):
            stats["conflicts"] += len(result["conflicts_logged"])
            stats["conflict_log"].extend(result["conflicts_logged"])
            for c in result["conflicts_logged"]:
                print(f"    [conflict] {c}")

    return stats


# ── Smoke test ─────────────────────────────────────────────────────────────────

def run_smoke_test(client: ClayClient) -> bool:
    """
    Run a smoke test: one lookup to verify the API key and connectivity.
    Returns True if successful.
    """
    print("\n[clay] Running smoke test...")
    result = client.smoke_test()

    if result["ok"] and result["person"] is not None:
        p = result["person"]
        print(f"  Smoke test PASSED")
        print(f"  Person found: {p.name}")
        print(f"  Title: {p.title}")
        print(f"  Org:   {p.org}")
        print(f"  Email: {p.email or '(none)'}")
        print(f"  LinkedIn: {p.linkedin_url or '(none)'}")
        return True
    elif result["ok"] and result["person"] is None:
        print(f"  Smoke test: API reachable but no data returned for test email")
        print(f"  This may indicate an Enterprise plan requirement for direct lookup.")
        print(f"  API key is valid — proceeding with webhook-table model if needed.")
        return True
    else:
        print(f"  Smoke test FAILED: {result['error']}")
        return False


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clay.com enrichment for Kissinger CRM — gap-fills after Apollo"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't write to Kissinger — show what would be done"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max contacts to process (default: all)"
    )
    parser.add_argument(
        "--since-hours", type=int, default=None,
        help="Only process contacts created in the last N hours (for cron use)"
    )
    parser.add_argument(
        "--smoke-test", action="store_true",
        help="Run a single lookup to verify API key and connectivity, then exit"
    )
    args = parser.parse_args()

    # Init client
    try:
        client = ClayClient()
    except ClayError as exc:
        print(f"[clay] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[clay] API key loaded (len={len(client.api_key)})")

    if args.smoke_test:
        ok = run_smoke_test(client)
        sys.exit(0 if ok else 1)

    # Ensure Clay is registered in Kissinger's source registry
    source_id = ensure_clay_source()
    print(f"[clay] Clay source ID in registry: {source_id}")

    # Run enrichment
    stats = run_enrichment(
        client=client,
        source_id=source_id,
        dry_run=args.dry_run,
        limit=args.limit,
        since_hours=args.since_hours,
    )

    # Summary
    print("\n" + "=" * 60)
    print("CLAY ENRICHMENT SUMMARY")
    print("=" * 60)
    print(f"Total gap contacts examined:  {stats['total']}")
    print(f"Successfully enriched:        {stats['enriched']}")
    print(f"Fields gap-filled:            {stats['gaps_filled']}")
    print(f"No Clay data found:           {stats['no_clay_data']}")
    print(f"Conflicts detected:           {stats['conflicts']}")
    print(f"Errors:                       {stats['errors']}")

    if stats.get("filled_fields"):
        print("\nFields filled by Clay:")
        for field, count in sorted(stats["filled_fields"].items(), key=lambda x: -x[1]):
            print(f"  {field}: {count}")

    if stats.get("conflict_log"):
        print("\nConflicts (Apollo kept):")
        for c in stats["conflict_log"]:
            print(f"  {c}")

    print()
    if args.dry_run:
        print("(dry run — no changes written to Kissinger)")

    # Exit 1 if all attempts failed (hard errors), 0 if at least some succeeded or no data
    if stats["errors"] > 0 and stats["enriched"] == 0 and stats["no_clay_data"] == 0:
        sys.exit(1)


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
