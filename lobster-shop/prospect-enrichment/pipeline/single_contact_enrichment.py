"""
Single-Contact Enrichment Flow

Triggered when the "Enrich Contact" button is pressed in eloso-bisque.
Runs work_history and connections goals for one Kissinger person entity,
writes results with full provenance, and writes the run manifest.

Entry points:
    enrich_contact(contact_id, dry_run=False) -> RunResult
    (called by POST /api/contacts/[id]/enrich in eloso-bisque)

Pipeline:
    1. Fetch the contact entity from Kissinger
    2. For work_history goal: iterate available sources, enrich, write with provenance
    3. For connections goal: walk graph edges, score connections, write inferred edges
    4. Write run manifest to ~/lobster-workspace/enrichment-runs/{run_id}.json
    5. Return RunResult with run_id and summary

Usage:
    python single_contact_enrichment.py --contact-id ent_abc123 [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Allow running as script
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent / "bin"))

from manifest_loader import load_manifest, available_sources_for_goal, confidence_from_score, hash_response, now_iso
from pipeline.idempotency_check import is_fresh
from pipeline.validator import validate_contact, validate_provenance
from pipeline.audit_log import AuditLog
from pipeline.dry_run import DryRunContext

KISSINGER_ENDPOINT = os.environ.get("KISSINGER_ENDPOINT", "http://localhost:8080/graphql")
KISSINGER_API_TOKEN = os.environ.get("KISSINGER_API_TOKEN", "")
_MANIFEST_PATH = _HERE.parent / "sources" / "manifest.json"
_ENRICHMENT_RUNS_DIR = Path.home() / "lobster-workspace" / "enrichment-runs"


# ---------------------------------------------------------------------------
# GraphQL helpers
# ---------------------------------------------------------------------------

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
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


_ENTITY_QUERY = """
query GetEntity($id: String!) {
  entity(id: $id) {
    id name kind tags notes
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

_CREATE_ENTITY_MUTATION = """
mutation CreateEntity($input: CreateEntityInput!) {
  createEntity(input: $input) { id name kind tags }
}
"""

_CREATE_EDGE_MUTATION = """
mutation CreateEdge($input: CreateEdgeInput!) {
  createEdge(input: $input) { id source target relation }
}
"""

_UPDATE_ENTITY_META_MUTATION = """
mutation UpdateEntityMeta($id: String!, $meta: [MetaInput!]!) {
  updateEntityMeta(id: $id, meta: $meta) { id meta { key value } }
}
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class RunResult:
    run_id: str
    status: str  # "completed" | "failed" | "dry_run"
    dry_run: bool
    contact_id: str
    contact_name: str
    goals_attempted: list[str]
    sources_attempted: list[str]
    sources_skipped: list[str]
    entities_enriched: int
    edges_inferred: int
    skipped_fresh: int
    errors: list[str]
    run_manifest_path: str


# ---------------------------------------------------------------------------
# Source adapters (stub implementations — extend with real API calls)
# ---------------------------------------------------------------------------

def _enrich_work_history_kissinger_graph(
    contact: dict[str, Any],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """
    Use the existing Kissinger graph to infer work history from works_at edges.

    Returns (enrichment_records, raw_response_json).
    """
    works_at = [e for e in edges if e.get("relation") == "works_at"]
    records = []
    for edge in works_at:
        records.append({
            "type": "work_history_edge",
            "target_entity": edge["target"],
            "relation": edge["relation"],
            "strength": edge.get("strength", 0),
            "notes": edge.get("notes", ""),
        })
    raw = json.dumps({"edges": works_at})
    return records, raw


def _enrich_connections_kissinger_graph(
    contact_id: str,
    edges: list[dict[str, Any]],
    endpoint: str,
    token: str,
) -> tuple[list[dict[str, Any]], str]:
    """
    Walk the Kissinger graph to find second-degree connections.

    For each org the contact works_at, find other people at that org.
    Returns (connection_records, raw_response_json).
    """
    works_at_org_ids = [
        e["target"] for e in edges if e.get("relation") == "works_at"
    ]

    connections = []
    all_raw = {"contact_id": contact_id, "orgs_checked": [], "connections_found": []}

    for org_id in works_at_org_ids:
        # Get edges TO this org (i.e., other people who work there)
        try:
            data = _gql(
                """
                query EdgesTo($entityId: String!, $first: Int) {
                  edgesTo(entityId: $entityId, first: $first) {
                    edges { node { source target relation strength } }
                  }
                }
                """,
                {"entityId": org_id, "first": 50},
                endpoint,
                token,
            )
            peers = data.get("edgesTo", {}).get("edges", [])
            all_raw["orgs_checked"].append(org_id)

            for peer_edge in peers:
                peer_id = peer_edge["node"]["source"]
                if peer_id == contact_id:
                    continue  # Skip self
                connections.append({
                    "person_a": contact_id,
                    "person_b": peer_id,
                    "via_org": org_id,
                    "relation_type": "same_org",
                    "strength_estimate": peer_edge["node"].get("strength", 0.3),
                })
                all_raw["connections_found"].append(peer_id)

        except Exception as exc:  # noqa: BLE001
            print(f"[single_enrichment] Failed to get peers at org {org_id}: {exc}", file=sys.stderr)

    return connections, json.dumps(all_raw)


# ---------------------------------------------------------------------------
# Core enrichment function
# ---------------------------------------------------------------------------

def enrich_contact(
    contact_id: str,
    *,
    dry_run: bool = False,
    endpoint: str = KISSINGER_ENDPOINT,
    token: str = KISSINGER_API_TOKEN,
    manifest_path: Path = _MANIFEST_PATH,
    runs_dir: Path = _ENRICHMENT_RUNS_DIR,
    pre_run_id: str | None = None,
) -> RunResult:
    """
    Run the single-contact enrichment pipeline.

    Goals: work_history, then connections.
    Writes full provenance to Kissinger meta on the contact entity.
    Writes run manifest to {runs_dir}/{run_id}.json.

    Args:
        contact_id: Kissinger entity ID of the person to enrich.
        dry_run: If True, no writes are performed.
        endpoint: Kissinger GraphQL endpoint.
        token: Kissinger bearer token.
        manifest_path: Path to sources/manifest.json.
        runs_dir: Directory for run manifests.

    Returns:
        RunResult with run details and summary.
    """
    run_id = pre_run_id or str(uuid.uuid4())
    runs_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(run_id=run_id, dry_run=dry_run, base_dir=runs_dir)
    dry_ctx = DryRunContext(enabled=dry_run)

    sources_attempted: list[str] = []
    sources_skipped: list[str] = []
    entities_enriched = 0
    edges_inferred = 0
    skipped_fresh_count = 0
    errors: list[str] = []

    # -------------------------------------------------------------------------
    # Step 1: Fetch the contact
    # -------------------------------------------------------------------------
    try:
        data = _gql(_ENTITY_QUERY, {"id": contact_id}, endpoint, token)
        contact = data["entity"]
    except Exception as exc:  # noqa: BLE001
        error_msg = f"Failed to fetch entity {contact_id}: {exc}"
        errors.append(error_msg)
        audit.write_error(entity_name=contact_id, source="kissinger", error=error_msg)
        result = RunResult(
            run_id=run_id,
            status="failed",
            dry_run=dry_run,
            contact_id=contact_id,
            contact_name="unknown",
            goals_attempted=[],
            sources_attempted=[],
            sources_skipped=[],
            entities_enriched=0,
            edges_inferred=0,
            skipped_fresh=0,
            errors=errors,
            run_manifest_path=str(runs_dir / f"{run_id}.json"),
        )
        audit.close(_result_to_summary(result))
        return result

    contact_name = contact.get("name", "unknown")
    entity_meta: list[dict[str, Any]] = contact.get("meta", [])

    # -------------------------------------------------------------------------
    # Step 2: Fetch the contact's edges
    # -------------------------------------------------------------------------
    edges: list[dict[str, Any]] = []
    try:
        edges_data = _gql(
            _EDGES_FROM_QUERY,
            {"entityId": contact_id, "first": 100},
            endpoint,
            token,
        )
        edges = [e["node"] for e in edges_data.get("edgesFrom", {}).get("edges", [])]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Failed to fetch edges for {contact_id}: {exc}")

    # -------------------------------------------------------------------------
    # Step 3: Load manifest and find available sources
    # -------------------------------------------------------------------------
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Failed to load manifest: {exc}")
        manifest = {"sources": [], "source_selection_strategy": {}, "goal_definitions": {}}

    goals_to_run = ["work_history", "connections"]

    # -------------------------------------------------------------------------
    # Step 4: Run work_history goal via Kissinger graph (always available)
    # -------------------------------------------------------------------------
    kissinger_source = {
        "source_id": "kissinger_graph",
        "display_name": "Kissinger Graph",
        "goal_scores": {"work_history": 0.45, "connections": 0.85},
        "data_freshness_days": 1,
    }

    # Work history via existing edges
    freshness = is_fresh(
        entity_meta=entity_meta,
        source_id="kissinger_graph",
        data_freshness_days=1,
    )
    if freshness.skip:
        skipped_fresh_count += 1
        audit.skipped_fresh(
            entity_id=contact_id,
            entity_name=contact_name,
            source="kissinger_graph",
            last_enriched_at=freshness.last_enriched_at or "",
            age_days=freshness.age_days or 0,
        )
    else:
        sources_attempted.append("kissinger_graph")
        wh_records, wh_raw = _enrich_work_history_kissinger_graph(contact, edges)

        provenance_meta = _build_provenance_meta(
            source_id="kissinger_graph",
            source_url=endpoint,
            run_id=run_id,
            goal="work_history",
            raw_response=wh_raw,
            goal_score=kissinger_source["goal_scores"]["work_history"],
        )

        if dry_ctx.would_write(contact_name, "updateEntityMeta(work_history)"):
            try:
                _gql(
                    _UPDATE_ENTITY_META_MUTATION,
                    {"id": contact_id, "meta": [{"key": k, "value": v} for k, v in provenance_meta.items()]},
                    endpoint,
                    token,
                )
                entities_enriched += 1
                audit.entity_created(
                    entity_id=contact_id,
                    entity_name=contact_name,
                    source="kissinger_graph",
                    goal="work_history",
                    meta_written=provenance_meta,
                )
            except Exception as exc:  # noqa: BLE001
                err = f"updateEntityMeta failed for {contact_name}: {exc}"
                errors.append(err)
                audit.write_error(entity_name=contact_name, source="kissinger_graph", error=err)
        else:
            audit.dry_run_would_create(
                entity_name=contact_name,
                source="kissinger_graph",
                goal="work_history",
                org_kissinger_id=None,
            )
            entities_enriched += 1  # Count as "would enrich" in dry-run

    # -------------------------------------------------------------------------
    # Step 5: Run connections goal via Kissinger graph
    # -------------------------------------------------------------------------
    freshness_conn = is_fresh(
        entity_meta=entity_meta,
        source_id="kissinger_graph_connections",
        data_freshness_days=1,
    )
    if freshness_conn.skip:
        skipped_fresh_count += 1
    else:
        conn_records, conn_raw = _enrich_connections_kissinger_graph(
            contact_id, edges, endpoint, token
        )

        conn_provenance_meta = _build_provenance_meta(
            source_id="kissinger_graph",
            source_url=endpoint,
            run_id=run_id,
            goal="connections",
            raw_response=conn_raw,
            goal_score=kissinger_source["goal_scores"]["connections"],
            suffix="kissinger_graph_connections",
        )

        if dry_ctx.would_write(contact_name, "updateEntityMeta(connections)"):
            try:
                _gql(
                    _UPDATE_ENTITY_META_MUTATION,
                    {"id": contact_id, "meta": [{"key": k, "value": v} for k, v in conn_provenance_meta.items()]},
                    endpoint,
                    token,
                )
                edges_inferred += len(conn_records)
                for conn in conn_records:
                    audit.edge_created(
                        source_entity=conn["person_a"],
                        target_entity=conn["person_b"],
                        relation="colleague_at",
                    )
            except Exception as exc:  # noqa: BLE001
                err = f"connections meta write failed for {contact_name}: {exc}"
                errors.append(err)
                audit.write_error(entity_name=contact_name, source="kissinger_graph", error=err)
        else:
            edges_inferred += len(conn_records)

    # -------------------------------------------------------------------------
    # Step 6: Try available paid sources for work_history
    # -------------------------------------------------------------------------
    wh_sources = available_sources_for_goal(manifest, "work_history")
    for source in wh_sources:
        sid = source["source_id"]
        if sid == "kissinger_graph":
            continue  # Already handled above

        freshness = is_fresh(
            entity_meta=entity_meta,
            source_id=sid,
            data_freshness_days=source["data_freshness_days"],
        )
        if freshness.skip:
            sources_skipped.append(sid)
            skipped_fresh_count += 1
            audit.skipped_fresh(
                entity_id=contact_id,
                entity_name=contact_name,
                source=sid,
                last_enriched_at=freshness.last_enriched_at or "",
                age_days=freshness.age_days or 0,
            )
            continue

        sources_attempted.append(sid)
        # NOTE: Real API calls would go here. Each source needs its own adapter.
        # The pattern is: call API -> get raw response -> build provenance meta -> write to Kissinger.
        # Placeholder: log that we would call this source.
        print(
            f"[single_enrichment] Source {sid} is available — "
            f"API adapter not yet implemented. Skipping.",
            file=sys.stderr,
        )

    # -------------------------------------------------------------------------
    # Step 7: Write run manifest
    # -------------------------------------------------------------------------
    result = RunResult(
        run_id=run_id,
        status="dry_run" if dry_run else "completed",
        dry_run=dry_run,
        contact_id=contact_id,
        contact_name=contact_name,
        goals_attempted=goals_to_run,
        sources_attempted=sources_attempted,
        sources_skipped=sources_skipped,
        entities_enriched=entities_enriched,
        edges_inferred=edges_inferred,
        skipped_fresh=skipped_fresh_count,
        errors=errors,
        run_manifest_path=str(runs_dir / f"{run_id}.json"),
    )
    audit.close(_result_to_summary(result))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_provenance_meta(
    source_id: str,
    source_url: str,
    run_id: str,
    goal: str,
    raw_response: str,
    goal_score: float,
    suffix: str | None = None,
) -> dict[str, str]:
    """Build the full provenance meta dict for a write."""
    confidence = confidence_from_score(goal_score)
    raw_hash = hash_response(raw_response)
    ts = now_iso()
    effective_source = suffix or source_id

    return {
        # Source-specific keys (multi-source scheme)
        f"provenance.source.{effective_source}": source_id,
        f"provenance.enriched_at.{effective_source}": ts,
        f"provenance.goal.{effective_source}": goal,
        f"provenance.confidence.{effective_source}": confidence,
        f"provenance.raw_response_hash.{effective_source}": raw_hash,
        # Generic keys (always reflect most recent)
        "provenance.source": source_id,
        "provenance.source_url": source_url,
        "provenance.enriched_at": ts,
        "provenance.enriched_by": "wallace",
        "provenance.pipeline_run_id": run_id,
        "provenance.confidence": confidence,
        "provenance.goal": goal,
        "provenance.raw_response_hash": raw_hash,
    }


def _result_to_summary(result: RunResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "contact_id": result.contact_id,
        "contact_name": result.contact_name,
        "goals_attempted": result.goals_attempted,
        "sources_attempted": result.sources_attempted,
        "sources_skipped": result.sources_skipped,
        "entities_enriched": result.entities_enriched,
        "edges_inferred": result.edges_inferred,
        "skipped_fresh": result.skipped_fresh,
        "errors": result.errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich a single Kissinger contact")
    parser.add_argument("--contact-id", required=True, help="Kissinger entity ID")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Pre-assigned run UUID (from the API caller). If omitted, a new UUID is generated.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--endpoint", default=KISSINGER_ENDPOINT)
    parser.add_argument("--token", default=KISSINGER_API_TOKEN)
    args = parser.parse_args()

    # Allow the caller to pre-assign a run_id (so the pending manifest written
    # by the API route and the final manifest written here share the same ID).
    pre_run_id = args.run_id or os.environ.get("ENRICHMENT_RUN_ID")

    result = enrich_contact(
        contact_id=args.contact_id,
        dry_run=args.dry_run,
        endpoint=args.endpoint,
        token=args.token,
        pre_run_id=pre_run_id,
    )
    print(json.dumps(result.__dict__, indent=2))
    if result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
