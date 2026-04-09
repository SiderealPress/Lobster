"""
Pipeline Hygiene Layer — Slice 2

Wraps all Kissinger writes with:
  1. Manifest-driven source validation (unavailable sources rejected at call time)
  2. Idempotency check (per-source freshness guard before any write)
  3. Provenance metadata injection (all 8 required fields, per ontology)
  4. Rollback log (JSONL, one entry per event)
  5. Dry-run mode (log all planned writes, execute nothing)

This module is the single point of contact for enrichment writes. The existing
add_contacts_provenance.py is wrapped by HygieneLayer; it should not be called
directly from new pipeline code.

Usage:
    from pipeline_hygiene import HygieneLayer

    layer = HygieneLayer(
        run_id="uuid",
        source_id="google_serp_free",
        goal="org_chart",
        manifest=loaded_manifest,
        dry_run=False,
        endpoint="http://localhost:8080/graphql",
        token="",
    )

    result = layer.write_contact(contact_dict, org_kissinger_id="ent_abc")
    # result: {"status": "written"|"skipped"|"dry_run"|"error", ...}
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Add bin dir to sys.path so sibling imports work when run as script
_BIN = Path(__file__).parent
sys.path.insert(0, str(_BIN))

from manifest_loader import (
    ManifestError,
    confidence_from_score,
    hash_response,
    load_manifest,
    now_iso,
)

_RUNS_DIR = Path.home() / "lobster-workspace" / "enrichment-runs"

_CREATE_ENTITY_MUTATION = """
mutation CreateEntity($input: CreateEntityInput!) {
  createEntity(input: $input) {
    id name kind tags
  }
}
"""

_CREATE_EDGE_MUTATION = """
mutation CreateEdge($input: CreateEdgeInput!) {
  createEdge(input: $input) {
    id source target relation
  }
}
"""

_ENTITY_META_QUERY = """
query EntityMeta($id: String!) {
  entity(id: $id) {
    id name
    meta { key value }
  }
}
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _gql(
    query: str,
    variables: dict[str, Any],
    endpoint: str,
    token: str,
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


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# HygieneLayer
# ---------------------------------------------------------------------------

class HygieneLayer:
    """
    Single point of contact for all enrichment writes.

    One instance per pipeline run. Shared across all contacts in a run.
    Thread-safe for sequential use (not concurrent).
    """

    def __init__(
        self,
        *,
        run_id: str,
        source_id: str,
        goal: str,
        manifest: dict[str, Any],
        dry_run: bool = False,
        endpoint: str = "http://localhost:8080/graphql",
        token: str = "",
    ) -> None:
        # Validate source is in manifest and available
        self._source = self._resolve_source(source_id, manifest)
        self._run_id = run_id
        self._goal = goal
        self._dry_run = dry_run
        self._endpoint = endpoint
        self._token = token

        # Confidence for this source+goal combo
        goal_score = self._source["goal_scores"].get(goal, 0.0)
        self._confidence = confidence_from_score(goal_score)

        # Rollback log file (opened on first write)
        _RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self._rollback_path = _RUNS_DIR / f"{run_id}-rollback.jsonl"
        self._log_file = open(self._rollback_path, "a")  # noqa: WPS515

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_contact(
        self,
        contact: dict[str, Any],
        org_kissinger_id: str | None = None,
        raw_response: str | bytes | None = None,
    ) -> dict[str, Any]:
        """
        Write a single contact to Kissinger with full provenance hygiene.

        Args:
            contact: Dict with at least {"name": str}. Optional: title, source_url.
            org_kissinger_id: If set, creates a works_at edge to this org entity.
            raw_response: Raw API response string/bytes to hash for audit.

        Returns:
            {
                "status": "written" | "skipped" | "dry_run" | "error",
                "entity_id": str | None,
                "edge_created": bool,
                "reason": str | None,  # for skipped/error
            }
        """
        name = (contact.get("name") or "").strip()
        if not name:
            result: dict[str, Any] = {
                "status": "error",
                "entity_id": None,
                "edge_created": False,
                "reason": "Missing required field: name",
            }
            self._log_event({
                "event": "error",
                "run_id": self._run_id,
                "timestamp": now_iso(),
                "entity_name": name or "<no name>",
                "source": self._source["source_id"],
                "error": "Missing required field: name",
            })
            return result

        # --- Idempotency check (only for existing entities by name) ---
        # Note: new contacts don't have a Kissinger ID yet, so we check by
        # searching the entity store for this name + org. The dedup step
        # (BIS-298) handles duplicate prevention for new contacts; here we
        # guard against re-enriching already-enriched entities.
        # For single-contact enrichment (from the UI), check by entity ID
        # if provided.
        entity_id_hint = contact.get("kissinger_id")
        if entity_id_hint:
            skip_reason = self._check_freshness(entity_id_hint)
            if skip_reason:
                self._log_event({
                    "event": "skipped_fresh",
                    "run_id": self._run_id,
                    "timestamp": now_iso(),
                    "entity_id": entity_id_hint,
                    "entity_name": name,
                    "source": self._source["source_id"],
                    "reason": skip_reason,
                })
                return {
                    "status": "skipped",
                    "entity_id": entity_id_hint,
                    "edge_created": False,
                    "reason": skip_reason,
                }

        # --- Build provenance metadata ---
        raw_hash = hash_response(raw_response or json.dumps(contact))
        source_id = self._source["source_id"]
        source_url = contact.get("source_url") or f"source:{source_id}"
        ts = now_iso()

        provenance_meta = [
            {"key": "provenance.source", "value": source_id},
            {"key": f"provenance.source.{source_id}", "value": source_id},
            {"key": "provenance.source_url", "value": source_url},
            {"key": "provenance.enriched_at", "value": ts},
            {"key": f"provenance.enriched_at.{source_id}", "value": ts},
            {"key": "provenance.enriched_by", "value": "wallace"},
            {"key": "provenance.pipeline_run_id", "value": self._run_id},
            {"key": "provenance.confidence", "value": self._confidence},
            {"key": "provenance.goal", "value": self._goal},
            {"key": "provenance.raw_response_hash", "value": raw_hash},
        ]

        # Contact-specific meta
        contact_meta = []
        if contact.get("title"):
            contact_meta.append({"key": "title", "value": contact["title"].strip()})
        if contact.get("email"):
            contact_meta.append({"key": "email", "value": contact["email"].strip()})
        if contact.get("company"):
            contact_meta.append({"key": "company", "value": contact["company"].strip()})

        # Legacy provenance field (backwards compat with existing pipeline)
        contact_meta.append({"key": "provenance", "value": "wallace"})

        all_meta = contact_meta + provenance_meta

        entity_input = {
            "kind": "person",
            "name": name,
            "tags": ["supply-chain", "prospect-enrichment"],
            "meta": all_meta,
        }

        # --- Dry run: log and return without writing ---
        if self._dry_run:
            self._log_event({
                "event": "entity_created",
                "run_id": self._run_id,
                "timestamp": ts,
                "entity_id": None,
                "entity_name": name,
                "source": source_id,
                "goal": self._goal,
                "dry_run": True,
                "meta_written": {m["key"]: m["value"] for m in all_meta},
            })
            if org_kissinger_id:
                self._log_event({
                    "event": "edge_created",
                    "run_id": self._run_id,
                    "timestamp": ts,
                    "source_entity": "<dry_run>",
                    "target_entity": org_kissinger_id,
                    "relation": "works_at",
                    "dry_run": True,
                })
            print(
                f"[dry-run] Would createEntity: {name} "
                f"(source={source_id}, goal={self._goal})",
                file=sys.stderr,
            )
            return {
                "status": "dry_run",
                "entity_id": None,
                "edge_created": False,
                "reason": None,
            }

        # --- Live write: createEntity ---
        entity_id: str | None = None
        try:
            data = _gql(
                _CREATE_ENTITY_MUTATION,
                {"input": entity_input},
                self._endpoint,
                self._token,
            )
            entity_id = data["createEntity"]["id"]
        except Exception as exc:  # noqa: BLE001
            err = f"createEntity failed: {exc}"
            self._log_event({
                "event": "error",
                "run_id": self._run_id,
                "timestamp": now_iso(),
                "entity_name": name,
                "source": source_id,
                "error": err,
            })
            return {
                "status": "error",
                "entity_id": None,
                "edge_created": False,
                "reason": err,
            }

        self._log_event({
            "event": "entity_created",
            "run_id": self._run_id,
            "timestamp": ts,
            "entity_id": entity_id,
            "entity_name": name,
            "source": source_id,
            "goal": self._goal,
            "dry_run": False,
            "meta_written": {m["key"]: m["value"] for m in all_meta},
        })

        # --- createEdge ---
        edge_created = False
        if org_kissinger_id and entity_id:
            try:
                _gql(
                    _CREATE_EDGE_MUTATION,
                    {"input": {
                        "source": entity_id,
                        "target": org_kissinger_id,
                        "relation": "works_at",
                    }},
                    self._endpoint,
                    self._token,
                )
                edge_created = True
                self._log_event({
                    "event": "edge_created",
                    "run_id": self._run_id,
                    "timestamp": now_iso(),
                    "source_entity": entity_id,
                    "target_entity": org_kissinger_id,
                    "relation": "works_at",
                    "dry_run": False,
                })
            except Exception as exc:  # noqa: BLE001
                # Non-fatal — entity was written, just the edge failed
                self._log_event({
                    "event": "error",
                    "run_id": self._run_id,
                    "timestamp": now_iso(),
                    "entity_name": name,
                    "source": source_id,
                    "error": f"createEdge failed: {exc}",
                })

        return {
            "status": "written",
            "entity_id": entity_id,
            "edge_created": edge_created,
            "reason": None,
        }

    def enrich_existing_entity(
        self,
        entity_id: str,
        meta_updates: dict[str, str],
        raw_response: str | bytes | None = None,
    ) -> dict[str, Any]:
        """
        Add provenance meta to an existing Kissinger entity (e.g. enriching
        a contact that was manually added). Uses updateEntity mutation.

        Args:
            entity_id: Kissinger entity ID.
            meta_updates: Dict of {key: value} pairs to add/update on the entity.
            raw_response: Raw API response for hash.

        Returns:
            {"status": "written"|"skipped"|"dry_run"|"error", ...}
        """
        # Idempotency check
        skip_reason = self._check_freshness(entity_id)
        if skip_reason:
            self._log_event({
                "event": "skipped_fresh",
                "run_id": self._run_id,
                "timestamp": now_iso(),
                "entity_id": entity_id,
                "source": self._source["source_id"],
                "reason": skip_reason,
            })
            return {
                "status": "skipped",
                "entity_id": entity_id,
                "reason": skip_reason,
            }

        raw_hash = hash_response(raw_response or json.dumps(meta_updates))
        source_id = self._source["source_id"]
        ts = now_iso()

        # Build full meta: provided updates + provenance
        all_meta: list[dict[str, str]] = [
            {"key": k, "value": v} for k, v in meta_updates.items()
        ]
        all_meta += [
            {"key": "provenance.source", "value": source_id},
            {"key": f"provenance.source.{source_id}", "value": source_id},
            {"key": "provenance.enriched_at", "value": ts},
            {"key": f"provenance.enriched_at.{source_id}", "value": ts},
            {"key": "provenance.enriched_by", "value": "wallace"},
            {"key": "provenance.pipeline_run_id", "value": self._run_id},
            {"key": "provenance.confidence", "value": self._confidence},
            {"key": "provenance.goal", "value": self._goal},
            {"key": "provenance.raw_response_hash", "value": raw_hash},
        ]

        if self._dry_run:
            self._log_event({
                "event": "entity_enriched",
                "run_id": self._run_id,
                "timestamp": ts,
                "entity_id": entity_id,
                "source": source_id,
                "goal": self._goal,
                "dry_run": True,
                "meta_written": {m["key"]: m["value"] for m in all_meta},
            })
            return {"status": "dry_run", "entity_id": entity_id, "reason": None}

        _UPDATE_ENTITY = """
        mutation UpdateEntity($id: String!, $input: UpdateEntityInput!) {
          updateEntity(id: $id, input: $input) { id name }
        }
        """
        try:
            _gql(
                _UPDATE_ENTITY,
                {"id": entity_id, "input": {"meta": all_meta}},
                self._endpoint,
                self._token,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"updateEntity failed: {exc}"
            self._log_event({
                "event": "error",
                "run_id": self._run_id,
                "timestamp": now_iso(),
                "entity_id": entity_id,
                "source": source_id,
                "error": err,
            })
            return {"status": "error", "entity_id": entity_id, "reason": err}

        self._log_event({
            "event": "entity_enriched",
            "run_id": self._run_id,
            "timestamp": ts,
            "entity_id": entity_id,
            "source": source_id,
            "goal": self._goal,
            "dry_run": False,
            "meta_written": {m["key"]: m["value"] for m in all_meta},
        })
        return {"status": "written", "entity_id": entity_id, "reason": None}

    def close(self) -> None:
        """Flush and close the rollback log file."""
        if self._log_file and not self._log_file.closed:
            self._log_file.flush()
            self._log_file.close()

    def __enter__(self) -> "HygieneLayer":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_source(
        self,
        source_id: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Find and validate the source in the manifest."""
        for src in manifest.get("sources", []):
            if src["source_id"] == source_id:
                if not src["available"]:
                    raise ManifestError(
                        f"Source '{source_id}' is not available "
                        f"(API key not configured). Cannot use for enrichment."
                    )
                return src
        raise ManifestError(
            f"Source '{source_id}' not found in manifest. "
            f"Known sources: {[s['source_id'] for s in manifest.get('sources', [])]}"
        )

    def _check_freshness(self, entity_id: str) -> str | None:
        """
        Check if this entity was already enriched by this source recently.

        Returns a human-readable reason string if fresh (should skip),
        or None if stale/missing (should proceed).

        Fetches the entity's meta from Kissinger. Non-fatal on network error
        (returns None = proceed with write rather than silently skipping).
        """
        try:
            data = _gql(
                _ENTITY_META_QUERY,
                {"id": entity_id},
                self._endpoint,
                self._token,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[hygiene] freshness check failed for {entity_id}: {exc} — proceeding",
                file=sys.stderr,
            )
            return None

        entity = data.get("entity")
        if not entity:
            return None

        meta = {m["key"]: m["value"] for m in entity.get("meta", [])}
        source_id = self._source["source_id"]

        # Check source-specific enriched_at first, then fall back to generic
        last_ts = (
            meta.get(f"provenance.enriched_at.{source_id}")
            or (meta.get("provenance.enriched_at") if meta.get("provenance.source") == source_id else None)
        )

        if not last_ts:
            return None

        try:
            age_days = (
                datetime.now(tz=timezone.utc) - _parse_iso(last_ts)
            ).total_seconds() / 86400
        except ValueError:
            return None

        freshness = self._source["data_freshness_days"]
        if age_days < freshness:
            return (
                f"Enriched by {source_id} {age_days:.1f}d ago "
                f"(fresh window: {freshness}d)"
            )
        return None

    def _log_event(self, event: dict[str, Any]) -> None:
        """Append one JSONL line to the rollback log."""
        try:
            self._log_file.write(json.dumps(event) + "\n")
            self._log_file.flush()
        except OSError as exc:
            print(f"[hygiene] rollback log write failed: {exc}", file=sys.stderr)
