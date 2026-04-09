"""
Audit Log — Pipeline Hygiene

Writes a JSONL audit trail for every enrichment write, skip, and error to:
    ~/lobster-workspace/enrichment-runs/{run_id}-rollback.jsonl

Also writes the final run summary manifest to:
    ~/lobster-workspace/enrichment-runs/{run_id}.json

Implements the Rollback Log Schema and Run Summary Schema from provenance/ontology.md.

Usage:
    from pipeline.audit_log import AuditLog

    log = AuditLog(run_id="a3f8c1d2-...", dry_run=False)
    log.entity_created(entity_id="ent_abc", entity_name="Jane Smith", source="apollo", goal="org_chart", meta_written={...})
    log.edge_created(source_entity="ent_abc", target_entity="ent_org456", relation="works_at")
    log.skipped_fresh(entity_id="ent_xyz", entity_name="Bob Jones", source="google_serp_free", last_enriched_at="...", age_days=1.25)
    log.write_error(entity_name="Unknown", source="apollo", error="HTTP 500")
    log.close(summary={...})
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DEFAULT_BASE_DIR = Path.home() / "lobster-workspace" / "enrichment-runs"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    """
    Append-only JSONL audit log for one pipeline run.

    Thread-safety: not thread-safe. Each run should have its own AuditLog instance.
    """

    def __init__(
        self,
        run_id: str,
        dry_run: bool = False,
        base_dir: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.dry_run = dry_run
        self._base_dir = base_dir or _DEFAULT_BASE_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._rollback_path = self._base_dir / f"{run_id}-rollback.jsonl"
        self._summary_path = self._base_dir / f"{run_id}.json"
        self._started_at = _now_iso()

    # -------------------------------------------------------------------------
    # Event writers
    # -------------------------------------------------------------------------

    def entity_created(
        self,
        entity_id: str,
        entity_name: str,
        source: str,
        goal: str,
        meta_written: dict[str, str],
    ) -> None:
        """Log a successful entity creation."""
        self._append({
            "event": "entity_created",
            "run_id": self.run_id,
            "timestamp": _now_iso(),
            "entity_id": entity_id,
            "entity_name": entity_name,
            "source": source,
            "goal": goal,
            "dry_run": self.dry_run,
            "meta_written": meta_written,
        })

    def edge_created(
        self,
        source_entity: str,
        target_entity: str,
        relation: str,
    ) -> None:
        """Log a successful edge creation."""
        self._append({
            "event": "edge_created",
            "run_id": self.run_id,
            "timestamp": _now_iso(),
            "source_entity": source_entity,
            "target_entity": target_entity,
            "relation": relation,
            "dry_run": self.dry_run,
        })

    def skipped_fresh(
        self,
        entity_id: str,
        entity_name: str,
        source: str,
        last_enriched_at: str,
        age_days: float,
    ) -> None:
        """Log a skipped entity because it was enriched recently."""
        self._append({
            "event": "skipped_fresh",
            "run_id": self.run_id,
            "timestamp": _now_iso(),
            "entity_id": entity_id,
            "entity_name": entity_name,
            "source": source,
            "last_enriched_at": last_enriched_at,
            "age_days": round(age_days, 3),
        })

    def skipped_validation(
        self,
        entity_name: str,
        source: str,
        errors: list[str],
    ) -> None:
        """Log a skipped entity because it failed validation."""
        self._append({
            "event": "skipped_validation",
            "run_id": self.run_id,
            "timestamp": _now_iso(),
            "entity_name": entity_name,
            "source": source,
            "validation_errors": errors,
        })

    def write_error(
        self,
        entity_name: str,
        source: str,
        error: str,
    ) -> None:
        """Log a write failure."""
        self._append({
            "event": "error",
            "run_id": self.run_id,
            "timestamp": _now_iso(),
            "entity_name": entity_name,
            "source": source,
            "error": error,
        })

    def dry_run_would_create(
        self,
        entity_name: str,
        source: str,
        goal: str,
        org_kissinger_id: str | None,
    ) -> None:
        """Log a dry-run simulation of an entity + edge creation."""
        self._append({
            "event": "dry_run_would_create",
            "run_id": self.run_id,
            "timestamp": _now_iso(),
            "entity_name": entity_name,
            "source": source,
            "goal": goal,
            "org_kissinger_id": org_kissinger_id,
            "dry_run": True,
        })

    # -------------------------------------------------------------------------
    # Summary writer
    # -------------------------------------------------------------------------

    def close(self, summary: dict[str, Any]) -> Path:
        """
        Write the final run summary JSON.

        Args:
            summary: Dict matching the Run Summary Schema from ontology.md.

        Returns:
            Path to the written summary file.
        """
        full_summary = {
            "run_id": self.run_id,
            "started_at": self._started_at,
            "finished_at": _now_iso(),
            "dry_run": self.dry_run,
            "rollback_log": str(self._rollback_path),
            **summary,
        }
        self._summary_path.write_text(json.dumps(full_summary, indent=2))
        return self._summary_path

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _append(self, event: dict[str, Any]) -> None:
        """Append one event line to the JSONL rollback log."""
        with open(self._rollback_path, "a") as f:
            f.write(json.dumps(event) + "\n")

    @property
    def rollback_path(self) -> Path:
        return self._rollback_path

    @property
    def summary_path(self) -> Path:
        return self._summary_path


# ---------------------------------------------------------------------------
# Convenience: read a run summary
# ---------------------------------------------------------------------------

def read_run_summary(run_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    """
    Read the summary JSON for a completed run.

    Returns None if the run doesn't exist yet (still running or never started).
    """
    base = base_dir or _DEFAULT_BASE_DIR
    path = base / f"{run_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_recent_runs(
    base_dir: Path | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    List recent run summaries, newest first.

    Args:
        base_dir: Directory containing run files.
        limit: Maximum number of runs to return.

    Returns:
        List of summary dicts (may have partial data if runs are still in progress).
    """
    base = base_dir or _DEFAULT_BASE_DIR
    if not base.exists():
        return []

    summaries = []
    for path in sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            summaries.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
        if len(summaries) >= limit:
            break
    return summaries
