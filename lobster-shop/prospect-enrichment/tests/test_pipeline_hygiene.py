"""
Tests for pipeline_hygiene.py and run_manifest.py — Slice 2 validation.

Run: python3 -m pytest tests/test_pipeline_hygiene.py -v
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))

from manifest_loader import load_manifest, now_iso
from pipeline_hygiene import HygieneLayer
from run_manifest import (
    complete_run,
    create_run,
    new_run_id,
    read_run,
    rollback_path,
    run_path,
    update_run,
)

_REAL_MANIFEST = Path(__file__).parent.parent / "sources" / "manifest.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manifest(sources: list[dict]) -> dict:
    return {
        "_schema_version": "1.0.0",
        "sources": sources,
        "goal_definitions": {},
        "source_selection_strategy": {
            "org_chart": [s["source_id"] for s in sources],
            "work_history": [],
            "connections": [],
        },
    }


def _free_source(source_id: str = "test_free", goal_score: float = 0.8) -> dict:
    return {
        "source_id": source_id,
        "display_name": "Test Free Source",
        "api_key_env": None,
        "available": True,
        "goals": ["org_chart"],
        "goal_scores": {"org_chart": goal_score},
        "rate_limits": {"requests_per_minute": 10, "requests_per_day": 100},
        "cost_per_call": 0.0,
        "data_freshness_days": 7,
        "requires_company": True,
        "requires_person": False,
        "output_fields": ["name", "title"],
        "notes": "Test",
    }


def _make_layer(
    tmp_path: Path,
    manifest: dict | None = None,
    dry_run: bool = False,
    source_id: str = "test_free",
    goal: str = "org_chart",
) -> HygieneLayer:
    if manifest is None:
        manifest = _make_manifest([_free_source(source_id)])
    run_id = str(uuid.uuid4())
    layer = HygieneLayer(
        run_id=run_id,
        source_id=source_id,
        goal=goal,
        manifest=manifest,
        dry_run=dry_run,
        endpoint="http://localhost:8080/graphql",
        token="",
    )
    # Override rollback dir to tmp_path
    import run_manifest as rm
    layer._rollback_path = tmp_path / f"{run_id}-rollback.jsonl"
    layer._log_file.close()
    layer._log_file = open(layer._rollback_path, "a")
    return layer


def _read_rollback(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Test: HygieneLayer — source validation
# ---------------------------------------------------------------------------

def test_unavailable_source_raises(tmp_path):
    """Creating a HygieneLayer with an unavailable source raises ManifestError."""
    from manifest_loader import ManifestError
    source = _free_source()
    source["available"] = False
    manifest = _make_manifest([source])
    with pytest.raises(ManifestError, match="not available"):
        HygieneLayer(
            run_id="x", source_id="test_free", goal="org_chart",
            manifest=manifest, dry_run=False,
        )


def test_unknown_source_raises(tmp_path):
    """Creating a HygieneLayer with unknown source_id raises ManifestError."""
    from manifest_loader import ManifestError
    manifest = _make_manifest([_free_source("source_a")])
    with pytest.raises(ManifestError, match="not found in manifest"):
        HygieneLayer(
            run_id="x", source_id="nonexistent", goal="org_chart",
            manifest=manifest, dry_run=False,
        )


# ---------------------------------------------------------------------------
# Test: Dry-run mode
# ---------------------------------------------------------------------------

def test_dry_run_no_network_calls(tmp_path):
    """Dry-run write_contact makes zero network calls."""
    manifest = _make_manifest([_free_source()])
    layer = _make_layer(tmp_path, manifest=manifest, dry_run=True)
    with patch("pipeline_hygiene._gql") as mock_gql:
        result = layer.write_contact({"name": "Jane Smith", "title": "VP SC"})
        layer.close()
    mock_gql.assert_not_called()
    assert result["status"] == "dry_run"
    assert result["entity_id"] is None


def test_dry_run_logs_to_rollback(tmp_path):
    """Dry-run writes are logged in the rollback JSONL."""
    layer = _make_layer(tmp_path, dry_run=True)
    with patch("pipeline_hygiene._gql"):
        layer.write_contact({"name": "Bob Jones", "title": "Demand Planner"})
        layer.close()

    events = _read_rollback(layer._rollback_path)
    assert len(events) >= 1
    entity_events = [e for e in events if e["event"] == "entity_created"]
    assert entity_events
    assert entity_events[0]["dry_run"] is True
    assert entity_events[0]["entity_name"] == "Bob Jones"


def test_dry_run_edge_logged(tmp_path):
    """Dry-run with org_id logs edge creation event."""
    layer = _make_layer(tmp_path, dry_run=True)
    with patch("pipeline_hygiene._gql"):
        layer.write_contact(
            {"name": "Alice", "title": "SCM"},
            org_kissinger_id="org_123",
        )
        layer.close()
    events = _read_rollback(layer._rollback_path)
    edge_events = [e for e in events if e["event"] == "edge_created"]
    assert edge_events
    assert edge_events[0]["dry_run"] is True
    assert edge_events[0]["target_entity"] == "org_123"


# ---------------------------------------------------------------------------
# Test: Provenance injection
# ---------------------------------------------------------------------------

def test_provenance_fields_written(tmp_path):
    """All 8 required provenance fields appear in meta when entity is created."""
    layer = _make_layer(tmp_path, dry_run=True)

    captured_inputs: list[dict] = []

    def fake_gql(query, variables, endpoint, token):
        captured_inputs.append(variables)
        return {"createEntity": {"id": "ent_abc", "name": "Test", "kind": "person", "tags": []}}

    with patch("pipeline_hygiene._gql", side_effect=fake_gql):
        # Disable dry_run for this test to capture live write path
        layer._dry_run = False
        layer.write_contact({"name": "Jane", "title": "VP SC", "source_url": "http://example.com"})
        layer.close()

    assert captured_inputs
    create_input = captured_inputs[0]["input"]
    meta = {m["key"]: m["value"] for m in create_input["meta"]}

    required_keys = [
        "provenance.source",
        "provenance.source_url",
        "provenance.enriched_at",
        "provenance.enriched_by",
        "provenance.pipeline_run_id",
        "provenance.confidence",
        "provenance.goal",
        "provenance.raw_response_hash",
    ]
    for k in required_keys:
        assert k in meta, f"Missing provenance field: {k}"

    assert meta["provenance.enriched_by"] == "wallace"
    assert meta["provenance.goal"] == "org_chart"
    assert meta["provenance.raw_response_hash"].startswith("sha256:")
    assert meta["provenance.confidence"] in {"high", "medium", "low"}


def test_confidence_high_for_high_score(tmp_path):
    """Source with goal_score 0.9 gets confidence=high."""
    manifest = _make_manifest([_free_source(goal_score=0.9)])
    layer = _make_layer(tmp_path, manifest=manifest, dry_run=True)
    assert layer._confidence == "high"


def test_confidence_medium_for_mid_score(tmp_path):
    """Source with goal_score 0.6 gets confidence=medium."""
    manifest = _make_manifest([_free_source(goal_score=0.6)])
    layer = _make_layer(tmp_path, manifest=manifest, dry_run=True)
    assert layer._confidence == "medium"


def test_confidence_low_for_low_score(tmp_path):
    """Source with goal_score 0.3 gets confidence=low."""
    manifest = _make_manifest([_free_source(goal_score=0.3)])
    layer = _make_layer(tmp_path, manifest=manifest, dry_run=True)
    assert layer._confidence == "low"


# ---------------------------------------------------------------------------
# Test: Idempotency / freshness guard
# ---------------------------------------------------------------------------

def _fresh_meta_response(source_id: str, enriched_at: str) -> dict:
    """Mock Kissinger response for an already-enriched entity."""
    return {
        "entity": {
            "id": "ent_xyz",
            "name": "Already Enriched",
            "meta": [
                {"key": f"provenance.enriched_at.{source_id}", "value": enriched_at},
                {"key": "provenance.source", "value": source_id},
            ],
        }
    }


def test_fresh_entity_is_skipped(tmp_path):
    """Entity enriched recently by same source is skipped."""
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    layer = _make_layer(tmp_path)  # data_freshness_days=7

    with patch("pipeline_hygiene._gql") as mock_gql:
        mock_gql.return_value = _fresh_meta_response("test_free", recent)
        result = layer.write_contact(
            {"name": "Fresh Person", "kissinger_id": "ent_xyz"}
        )
        layer.close()

    assert result["status"] == "skipped"
    assert "fresh" in result["reason"].lower() or "1.0" in result["reason"]
    # Only one call made (the freshness check) — no createEntity
    assert mock_gql.call_count == 1


def test_stale_entity_is_written(tmp_path):
    """Entity enriched longer ago than freshness window is re-enriched."""
    stale = (datetime.now(tz=timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    layer = _make_layer(tmp_path)  # data_freshness_days=7

    call_responses = [
        _fresh_meta_response("test_free", stale),  # first call: freshness check
        {"createEntity": {"id": "ent_new", "name": "Stale Person", "kind": "person", "tags": []}},  # second: create
    ]

    with patch("pipeline_hygiene._gql", side_effect=call_responses):
        result = layer.write_contact(
            {"name": "Stale Person", "kissinger_id": "ent_xyz"}
        )
        layer.close()

    assert result["status"] == "written"
    assert result["entity_id"] == "ent_new"


def test_no_kissinger_id_skips_freshness_check(tmp_path):
    """Contact without kissinger_id skips freshness check, goes straight to create."""
    layer = _make_layer(tmp_path)

    with patch("pipeline_hygiene._gql") as mock_gql:
        mock_gql.return_value = {"createEntity": {"id": "ent_brand_new", "name": "New", "kind": "person", "tags": []}}
        result = layer.write_contact({"name": "New Person", "title": "Director"})
        layer.close()

    assert result["status"] == "written"
    # Exactly one call: createEntity (no freshness check)
    assert mock_gql.call_count == 1


def test_different_source_not_blocked_by_other_source_freshness(tmp_path):
    """Fresh entry from source A does not block enrichment from source B."""
    # source B is test_free_b; entity has fresh provenance from source A
    source_b = dict(_free_source("test_free_b"))
    manifest = _make_manifest([source_b])
    layer = _make_layer(tmp_path, manifest=manifest, source_id="test_free_b")

    # Entity has fresh provenance from source_a — NOT from test_free_b
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_response = {
        "entity": {
            "id": "ent_xyz",
            "name": "Cross Source Person",
            "meta": [
                {"key": "provenance.enriched_at.source_a", "value": recent},
                {"key": "provenance.source", "value": "source_a"},
            ],
        }
    }

    with patch("pipeline_hygiene._gql") as mock_gql:
        mock_gql.side_effect = [
            meta_response,  # freshness check
            {"createEntity": {"id": "ent_new", "name": "Cross Source Person", "kind": "person", "tags": []}},
        ]
        result = layer.write_contact(
            {"name": "Cross Source Person", "kissinger_id": "ent_xyz"}
        )
        layer.close()

    assert result["status"] == "written"


# ---------------------------------------------------------------------------
# Test: Rollback log completeness
# ---------------------------------------------------------------------------

def test_rollback_log_written_on_success(tmp_path):
    """Successful write produces entity_created event in rollback log."""
    layer = _make_layer(tmp_path)
    with patch("pipeline_hygiene._gql") as mock_gql:
        mock_gql.return_value = {"createEntity": {"id": "ent_001", "name": "X", "kind": "person", "tags": []}}
        layer.write_contact({"name": "Test Person"})
        layer.close()

    events = _read_rollback(layer._rollback_path)
    entity_events = [e for e in events if e["event"] == "entity_created"]
    assert entity_events
    e = entity_events[0]
    assert e["entity_id"] == "ent_001"
    assert e["dry_run"] is False
    assert "provenance.enriched_by" in e["meta_written"]
    assert e["meta_written"]["provenance.enriched_by"] == "wallace"


def test_rollback_log_skipped_event(tmp_path):
    """Freshness skip produces skipped_fresh event in rollback log."""
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    layer = _make_layer(tmp_path)
    with patch("pipeline_hygiene._gql") as mock_gql:
        mock_gql.return_value = _fresh_meta_response("test_free", recent)
        layer.write_contact({"name": "Fresh", "kissinger_id": "ent_fresh"})
        layer.close()

    events = _read_rollback(layer._rollback_path)
    skip_events = [e for e in events if e["event"] == "skipped_fresh"]
    assert skip_events
    assert skip_events[0]["entity_id"] == "ent_fresh"


def test_rollback_log_error_event(tmp_path):
    """createEntity failure logs an error event."""
    layer = _make_layer(tmp_path)
    with patch("pipeline_hygiene._gql", side_effect=RuntimeError("Kissinger down")):
        result = layer.write_contact({"name": "Error Person"})
        layer.close()

    assert result["status"] == "error"
    events = _read_rollback(layer._rollback_path)
    error_events = [e for e in events if e["event"] == "error"]
    assert error_events
    assert "Kissinger down" in error_events[0]["error"]


def test_missing_name_returns_error(tmp_path):
    """Contact without a name returns error without network call."""
    layer = _make_layer(tmp_path)
    with patch("pipeline_hygiene._gql") as mock_gql:
        result = layer.write_contact({})
        layer.close()
    mock_gql.assert_not_called()
    assert result["status"] == "error"
    assert "name" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Test: Context manager interface
# ---------------------------------------------------------------------------

def test_context_manager_closes_log(tmp_path):
    """HygieneLayer used as context manager closes rollback log on exit."""
    manifest = _make_manifest([_free_source()])
    run_id = str(uuid.uuid4())
    with HygieneLayer(
        run_id=run_id,
        source_id="test_free",
        goal="org_chart",
        manifest=manifest,
        dry_run=True,
    ) as layer:
        with patch("pipeline_hygiene._gql"):
            layer.write_contact({"name": "Alice"})
    assert layer._log_file.closed


# ---------------------------------------------------------------------------
# Test: Run manifest (run_manifest.py)
# ---------------------------------------------------------------------------

def test_create_run_writes_file(tmp_path, monkeypatch):
    """create_run writes a JSON file in enrichment-runs dir."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)
    run_id = new_run_id()
    manifest = create_run(run_id, dry_run=False)
    assert manifest["status"] == "running"
    assert manifest["run_id"] == run_id
    assert (tmp_path / f"{run_id}.json").exists()


def test_read_run_returns_dict(tmp_path, monkeypatch):
    """read_run returns the created manifest dict."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)
    run_id = new_run_id()
    create_run(run_id)
    result = read_run(run_id)
    assert result is not None
    assert result["run_id"] == run_id


def test_read_run_nonexistent_returns_none(tmp_path, monkeypatch):
    """read_run returns None for unknown run_id."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)
    assert read_run("nonexistent-uuid") is None


def test_complete_run(tmp_path, monkeypatch):
    """complete_run sets status=completed and finished_at."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)
    run_id = new_run_id()
    create_run(run_id)
    result = complete_run(run_id, contacts_added=5, errors=[])
    assert result["status"] == "completed"
    assert result["finished_at"] is not None
    assert result["contacts_added"] == 5


def test_update_run_merges(tmp_path, monkeypatch):
    """update_run merges partial updates without overwriting other fields."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)
    run_id = new_run_id()
    create_run(run_id, dry_run=True)
    update_run(run_id, {"contacts_found": 10})
    r = read_run(run_id)
    assert r["contacts_found"] == 10
    assert r["dry_run"] is True  # not overwritten
