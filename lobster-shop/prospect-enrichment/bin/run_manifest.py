"""
Run Result Manifest

Writes and reads the per-run JSON summary at:
    ~/lobster-workspace/enrichment-runs/{run_id}.json

Used by:
  - The enrichment pipeline (writes status + counters on completion)
  - The eloso-bisque API route (polls status for the UI spinner)

Schema matches provenance/ontology.md.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

_RUNS_DIR = Path.home() / "lobster-workspace" / "enrichment-runs"

RunStatus = Literal["running", "completed", "failed"]


def new_run_id() -> str:
    return str(uuid.uuid4())


def runs_dir() -> Path:
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return _RUNS_DIR


def run_path(run_id: str) -> Path:
    return runs_dir() / f"{run_id}.json"


def rollback_path(run_id: str) -> Path:
    return runs_dir() / f"{run_id}-rollback.jsonl"


def create_run(
    run_id: str,
    *,
    dry_run: bool = False,
    contact_id: str | None = None,
    goals: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create and persist an initial 'running' run manifest.
    Returns the manifest dict.
    """
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "started_at": _now_iso(),
        "finished_at": None,
        "status": "running",
        "dry_run": dry_run,
        "contact_id": contact_id,
        "goals": goals or ["org_chart"],
        "sources_attempted": [],
        "sources_skipped": [],
        "companies_scanned": 0,
        "contacts_found": 0,
        "contacts_added": 0,
        "duplicates_skipped": 0,
        "fuzzy_flagged": 0,
        "skipped_fresh": 0,
        "errors": [],
        "rollback_log": str(rollback_path(run_id)),
    }
    _write(run_id, manifest)
    return manifest


def update_run(run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """
    Merge updates into an existing run manifest and persist.
    Returns the updated manifest.
    """
    manifest = read_run(run_id) or {}
    manifest.update(updates)
    _write(run_id, manifest)
    return manifest


def complete_run(
    run_id: str,
    *,
    status: RunStatus = "completed",
    **counters: Any,
) -> dict[str, Any]:
    """
    Mark run as completed (or failed), set finished_at, merge counters.
    """
    updates = {
        "status": status,
        "finished_at": _now_iso(),
        **counters,
    }
    return update_run(run_id, updates)


def read_run(run_id: str) -> dict[str, Any] | None:
    """Read a run manifest. Returns None if not found."""
    p = run_path(run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write(run_id: str, manifest: dict[str, Any]) -> None:
    run_path(run_id).write_text(json.dumps(manifest, indent=2))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
