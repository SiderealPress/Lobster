"""
Enrichment Source Manifest Loader

Loads and validates the data source manifest at startup.
Evaluates source availability by checking environment variables.
Provides goal-sorted source lists for the pipeline to use.

Usage:
    from manifest_loader import load_manifest, available_sources_for_goal

    manifest = load_manifest()
    sources = available_sources_for_goal(manifest, "org_chart")
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MANIFEST_PATH = Path(__file__).parent.parent / "sources" / "manifest.json"


class ManifestError(RuntimeError):
    pass


REQUIRED_FIELDS = {
    "source_id", "display_name", "api_key_env", "available",
    "goals", "goal_scores", "rate_limits", "cost_per_call",
    "data_freshness_days", "requires_company", "requires_person",
    "output_fields", "notes",
}

VALID_GOALS = {"org_chart", "work_history", "connections"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def load_manifest(path: Path = _MANIFEST_PATH) -> dict[str, Any]:
    """
    Load and validate the source manifest.

    - Checks required fields on every source entry.
    - Evaluates actual availability by checking environment variables.
    - Logs skipped (unavailable) sources to stderr.

    Returns the validated manifest dict with each source's `available` field
    reflecting real-time env var presence.

    Raises ManifestError on schema violations.
    """
    if not path.exists():
        raise ManifestError(f"Manifest not found: {path}")

    with open(path) as f:
        try:
            manifest = json.load(f)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"Manifest JSON parse error: {exc}") from exc

    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ManifestError("Manifest 'sources' must be a non-empty list")

    validated: list[dict[str, Any]] = []
    for entry in sources:
        source_id = entry.get("source_id", "<unknown>")

        # Required field presence check
        missing = REQUIRED_FIELDS - set(entry.keys())
        if missing:
            raise ManifestError(
                f"Source '{source_id}' missing required fields: {sorted(missing)}"
            )

        # Goal validity
        for g in entry["goals"]:
            if g not in VALID_GOALS:
                raise ManifestError(
                    f"Source '{source_id}' has unknown goal '{g}'. "
                    f"Valid: {sorted(VALID_GOALS)}"
                )

        # Goal scores completeness — every declared goal must have a score
        for g in entry["goals"]:
            if g not in entry["goal_scores"]:
                raise ManifestError(
                    f"Source '{source_id}' missing goal_score for '{g}'"
                )

        # Evaluate actual availability from environment
        api_key_env = entry["api_key_env"]
        if api_key_env is None:
            # No key needed — always available
            actually_available = True
        else:
            key_value = os.environ.get(api_key_env, "").strip()
            actually_available = bool(key_value)

        if not actually_available:
            print(
                f"[manifest] SKIP {source_id} ({entry['display_name']}): "
                f"{'no API key env' if api_key_env else 'unavailable'} "
                f"({api_key_env or 'no key needed but marked unavailable'})",
                file=sys.stderr,
            )
        else:
            print(
                f"[manifest] OK   {source_id} ({entry['display_name']})",
                file=sys.stderr,
            )

        validated_entry = dict(entry)
        validated_entry["available"] = actually_available
        validated.append(validated_entry)

    manifest["sources"] = validated
    return manifest


def available_sources_for_goal(
    manifest: dict[str, Any],
    goal: str,
) -> list[dict[str, Any]]:
    """
    Return available sources that cover the given goal, sorted by goal_score descending.

    Args:
        manifest: Loaded manifest dict (from load_manifest).
        goal: One of "org_chart", "work_history", "connections".

    Returns:
        List of source dicts, highest goal_score first.
    """
    if goal not in VALID_GOALS:
        raise ValueError(f"Unknown goal '{goal}'. Valid: {sorted(VALID_GOALS)}")

    # Use the manifest's preferred ordering as a tiebreaker
    preferred_order = manifest.get("source_selection_strategy", {}).get(goal, [])
    order_map = {sid: i for i, sid in enumerate(preferred_order)}

    sources = [
        s for s in manifest["sources"]
        if s["available"] and goal in s["goals"]
    ]
    sources.sort(
        key=lambda s: (
            -s["goal_scores"].get(goal, 0.0),
            order_map.get(s["source_id"], 999),
        )
    )
    return sources


def confidence_from_score(goal_score: float) -> str:
    """Map a goal score float to a provenance confidence string."""
    if goal_score >= 0.75:
        return "high"
    if goal_score >= 0.50:
        return "medium"
    return "low"


def hash_response(raw: str | bytes) -> str:
    """SHA-256 hash a raw API response for provenance audit trail."""
    if isinstance(raw, str):
        raw = raw.encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def now_iso() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
