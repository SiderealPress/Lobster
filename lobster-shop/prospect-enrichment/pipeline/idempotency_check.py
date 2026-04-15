"""
Idempotency Check — Pipeline Hygiene

Before writing enrichment data to Kissinger, check whether the target entity
already has a recent provenance record from the same source. If it does, skip
the write to avoid duplicate enrichment.

Implements the per-source freshness algorithm from provenance/ontology.md.

Usage:
    from pipeline.idempotency_check import is_fresh, FreshnessResult

    result = is_fresh(
        entity_meta=[{"key": "provenance.enriched_at.apollo", "value": "2026-04-01T00:00:00Z"}],
        source_id="apollo",
        data_freshness_days=30,
    )
    if result.skip:
        print(f"SKIP: {result.reason}")
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class FreshnessResult:
    """Result of a freshness check for one (entity, source) pair."""

    skip: bool
    """True if enrichment should be skipped (still fresh)."""

    reason: str
    """Human-readable explanation."""

    source_id: str
    """The source that was checked."""

    last_enriched_at: str | None
    """The ISO timestamp of the last enrichment, or None if never enriched."""

    age_days: float | None
    """Age of the last enrichment in days, or None if never enriched."""


def is_fresh(
    entity_meta: list[dict[str, Any]],
    source_id: str,
    data_freshness_days: int,
    *,
    now: datetime | None = None,
) -> FreshnessResult:
    """
    Check whether an entity has been recently enriched by source_id.

    Implements the multi-source provenance scheme from ontology.md:
    - First checks the source-specific key: provenance.enriched_at.<source_id>
    - Falls back to the generic provenance.enriched_at if provenance.source matches

    Args:
        entity_meta: List of {key, value} meta dicts from the Kissinger entity.
        source_id: The data source to check freshness for.
        data_freshness_days: Maximum age (in days) before re-enrichment is allowed.
        now: Override current time (for testing). Defaults to UTC now.

    Returns:
        FreshnessResult with skip=True if still fresh, skip=False if stale/never enriched.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    meta = {m["key"]: m["value"] for m in entity_meta}

    # 1. Check source-specific key (multi-source suffix scheme)
    source_specific_key = f"provenance.enriched_at.{source_id}"
    last_enriched_str = meta.get(source_specific_key)

    # 2. Fallback: generic key, only if provenance.source matches this source
    if last_enriched_str is None:
        generic_ts = meta.get("provenance.enriched_at")
        generic_src = meta.get("provenance.source")
        if generic_ts and generic_src == source_id:
            last_enriched_str = generic_ts

    if not last_enriched_str:
        return FreshnessResult(
            skip=False,
            reason=f"No prior enrichment by {source_id}",
            source_id=source_id,
            last_enriched_at=None,
            age_days=None,
        )

    # Parse the timestamp
    try:
        last_enriched = datetime.fromisoformat(
            last_enriched_str.replace("Z", "+00:00")
        )
    except ValueError:
        # Unparseable timestamp — treat as stale
        print(
            f"[idempotency] WARNING: unparseable enriched_at '{last_enriched_str}' "
            f"for source {source_id} — treating as stale",
            file=sys.stderr,
        )
        return FreshnessResult(
            skip=False,
            reason=f"Unparseable enriched_at timestamp from {source_id}",
            source_id=source_id,
            last_enriched_at=last_enriched_str,
            age_days=None,
        )

    age_days = (now - last_enriched).total_seconds() / 86400

    if age_days < data_freshness_days:
        return FreshnessResult(
            skip=True,
            reason=(
                f"Enriched by {source_id} {age_days:.1f}d ago "
                f"(fresh window: {data_freshness_days}d)"
            ),
            source_id=source_id,
            last_enriched_at=last_enriched_str,
            age_days=age_days,
        )

    return FreshnessResult(
        skip=False,
        reason=(
            f"Last enriched by {source_id} {age_days:.1f}d ago "
            f"(stale, window: {data_freshness_days}d)"
        ),
        source_id=source_id,
        last_enriched_at=last_enriched_str,
        age_days=age_days,
    )


def check_all_sources(
    entity_meta: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, FreshnessResult]:
    """
    Run freshness checks for all provided sources against one entity's meta.

    Args:
        entity_meta: List of {key, value} meta dicts from the Kissinger entity.
        sources: List of source dicts from the manifest (each has source_id, data_freshness_days).
        now: Override current time (for testing).

    Returns:
        Dict mapping source_id -> FreshnessResult.
    """
    results: dict[str, FreshnessResult] = {}
    for source in sources:
        results[source["source_id"]] = is_fresh(
            entity_meta=entity_meta,
            source_id=source["source_id"],
            data_freshness_days=source["data_freshness_days"],
            now=now,
        )
    return results
