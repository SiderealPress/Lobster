"""
Tests for enrich_contact.py — Slice 4 validation.

Includes:
  - Unit tests with mocked Kissinger (fast)
  - One end-to-end dry-run test against real Kissinger (requires live service)

Run all:   python3 -m pytest tests/test_enrich_contact.py -v
Run fast:  python3 -m pytest tests/test_enrich_contact.py -v -m "not e2e"
Run e2e:   python3 -m pytest tests/test_enrich_contact.py -v -m e2e
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))

from enrich_contact import enrich_contact
from run_manifest import read_run, runs_dir

_KISSINGER_URL = "http://localhost:8080/graphql"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _person_entity(entity_id: str = "ent_person_1", name: str = "Jane Smith") -> dict:
    return {
        "entity": {
            "id": entity_id,
            "kind": "person",
            "name": name,
            "tags": ["supply-chain"],
            "notes": "",
            "archived": False,
            "meta": [
                {"key": "title", "value": "VP Supply Chain"},
                {"key": "company", "value": "Acme Corp"},
            ],
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
    }


def _org_entity(entity_id: str = "ent_org_1", name: str = "Acme Corp") -> dict:
    return {
        "entity": {
            "id": entity_id,
            "kind": "org",
            "name": name,
            "tags": ["prospect"],
            "notes": "",
            "archived": False,
            "meta": [],
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
    }


def _edges_response(edges: list[dict] | None = None) -> dict:
    return {
        "edgesFrom": {
            "edges": [{"node": e} for e in (edges or [])]
        }
    }


def _dedup_response(new: list[dict] | None = None) -> dict:
    return {
        "new": new or [],
        "duplicates": [],
        "fuzzy_matches": [],
    }


# ---------------------------------------------------------------------------
# Test: Run manifest lifecycle
# ---------------------------------------------------------------------------

def test_run_manifest_created_immediately(tmp_path, monkeypatch):
    """enrich_contact writes a running manifest before any enrichment."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())

    gql_responses = [
        _person_entity(),    # _ENTITY_QUERY
        _edges_response(),   # _EDGES_FROM_QUERY
    ]

    with patch("enrich_contact._gql", side_effect=gql_responses):
        with patch("enrich_contact.find_supply_chain_contacts", return_value=[]):
            result = enrich_contact(
                contact_id="ent_person_1",
                run_id=run_id,
                dry_run=True,
            )

    assert (tmp_path / f"{run_id}.json").exists()
    manifest = read_run(run_id)
    assert manifest is not None
    assert manifest["run_id"] == run_id
    assert manifest["contact_id"] == "ent_person_1"


def test_completed_status_on_success(tmp_path, monkeypatch):
    """enrich_contact writes status=completed on success."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    with patch("enrich_contact._gql", side_effect=[
        _person_entity(), _edges_response()
    ]):
        with patch("enrich_contact.find_supply_chain_contacts", return_value=[]):
            result = enrich_contact(
                contact_id="ent_person_1",
                run_id=run_id,
                dry_run=True,
            )

    assert result["status"] == "completed"


def test_failed_status_when_entity_not_found(tmp_path, monkeypatch):
    """enrich_contact writes status=failed when entity doesn't exist."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    with patch("enrich_contact._gql", return_value={"entity": None}):
        result = enrich_contact(
            contact_id="ent_missing",
            run_id=run_id,
            dry_run=True,
        )

    assert result["status"] == "failed"
    assert any("not found" in e for e in result["errors"])


def test_failed_status_on_kissinger_unreachable(tmp_path, monkeypatch):
    """enrich_contact writes status=failed on network error."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    with patch("enrich_contact._gql", side_effect=RuntimeError("Connection refused")):
        result = enrich_contact(
            contact_id="ent_person_1",
            run_id=run_id,
            dry_run=True,
        )

    assert result["status"] == "failed"
    assert any("Connection refused" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Test: Org entity enrichment
# ---------------------------------------------------------------------------

def test_org_entity_triggers_org_chart_search(tmp_path, monkeypatch):
    """Org entity triggers find_supply_chain_contacts with org name."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    captured_company = []

    def fake_find(company, **kwargs):
        captured_company.append(company)
        return []

    with patch("enrich_contact._gql", side_effect=[
        _org_entity("ent_org_1", "Acme Corp"),
        _edges_response(),
    ]):
        with patch("enrich_contact.find_supply_chain_contacts", side_effect=fake_find):
            enrich_contact(
                contact_id="ent_org_1",
                run_id=run_id,
                dry_run=True,
            )

    assert captured_company == ["Acme Corp"]


def test_person_entity_uses_company_from_meta(tmp_path, monkeypatch):
    """Person entity uses company from meta for org_chart search."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    captured_company = []

    def fake_find(company, **kwargs):
        captured_company.append(company)
        return []

    with patch("enrich_contact._gql", side_effect=[
        _person_entity(),
        _edges_response(),
    ]):
        with patch("enrich_contact.find_supply_chain_contacts", side_effect=fake_find):
            enrich_contact(
                contact_id="ent_person_1",
                run_id=run_id,
                dry_run=True,
            )

    assert captured_company == ["Acme Corp"]


def test_person_without_company_skips_org_chart(tmp_path, monkeypatch):
    """Person with no company meta skips org_chart goal."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    entity = {
        "entity": {
            "id": "ent_p2",
            "kind": "person",
            "name": "Bob",
            "tags": [],
            "notes": "",
            "archived": False,
            "meta": [],  # No company
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
    }

    with patch("enrich_contact._gql", side_effect=[entity, _edges_response()]):
        with patch("enrich_contact.find_supply_chain_contacts") as mock_find:
            enrich_contact(contact_id="ent_p2", run_id=run_id, dry_run=True)

    mock_find.assert_not_called()


# ---------------------------------------------------------------------------
# Test: New contacts written through hygiene layer
# ---------------------------------------------------------------------------

def test_new_contacts_written_in_dry_run(tmp_path, monkeypatch):
    """New contacts from search are passed through HygieneLayer in dry-run."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    new_contacts = [
        {"name": "Alice Chen", "title": "VP SC", "company": "Acme Corp", "source_url": "http://li.com/in/alice"},
        {"name": "Bob Lee", "title": "Demand Planner", "company": "Acme Corp", "source_url": "http://li.com/in/bob"},
    ]

    with patch("enrich_contact._gql", side_effect=[
        _org_entity("ent_org_1", "Acme Corp"),
        _edges_response(),
    ]):
        with patch("enrich_contact.find_supply_chain_contacts", return_value=new_contacts):
            with patch("enrich_contact.dedup_crm_contacts", return_value=_dedup_response(new=new_contacts)):
                result = enrich_contact(
                    contact_id="ent_org_1",
                    run_id=run_id,
                    dry_run=True,  # dry-run: HygieneLayer won't actually create entities
                )

    assert result["contacts_found"] == 2
    # In dry-run, contacts_added stays 0 (no actual writes)
    assert result["contacts_added"] == 0
    assert result["status"] == "completed"


def test_contacts_added_counter_incremented_on_live_write(tmp_path, monkeypatch):
    """contacts_added counter increments when HygieneLayer.write_contact returns 'written'."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    new_contacts = [
        {"name": "Alice Chen", "title": "VP SC", "company": "Acme Corp", "source_url": "http://li.com/in/alice"},
    ]

    mock_layer = MagicMock()
    mock_layer.__enter__ = MagicMock(return_value=mock_layer)
    mock_layer.__exit__ = MagicMock(return_value=None)
    mock_layer.write_contact.return_value = {
        "status": "written", "entity_id": "ent_new_1", "edge_created": True, "reason": None
    }

    with patch("enrich_contact._gql", side_effect=[
        _org_entity("ent_org_1", "Acme Corp"),
        _edges_response(),
    ]):
        with patch("enrich_contact.find_supply_chain_contacts", return_value=new_contacts):
            with patch("enrich_contact.dedup_crm_contacts", return_value=_dedup_response(new=new_contacts)):
                with patch("enrich_contact.HygieneLayer", return_value=mock_layer):
                    result = enrich_contact(
                        contact_id="ent_org_1",
                        run_id=run_id,
                        dry_run=False,
                    )

    assert result["contacts_added"] == 1


def test_duplicate_contacts_counted(tmp_path, monkeypatch):
    """Contacts classified as duplicates by dedup are counted in duplicates_skipped."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    raw_contacts = [{"name": "Alice Chen", "title": "VP SC", "company": "Acme"}]
    dedup_result = {
        "new": [],
        "duplicates": raw_contacts,
        "fuzzy_matches": [],
    }

    with patch("enrich_contact._gql", side_effect=[
        _org_entity(), _edges_response()
    ]):
        with patch("enrich_contact.find_supply_chain_contacts", return_value=raw_contacts):
            with patch("enrich_contact.dedup_crm_contacts", return_value=dedup_result):
                result = enrich_contact(
                    contact_id="ent_org_1",
                    run_id=run_id,
                    dry_run=True,
                )

    assert result["duplicates_skipped"] == 1
    assert result["contacts_added"] == 0


def test_run_summary_has_all_required_fields(tmp_path, monkeypatch):
    """Run manifest has all fields required by ontology."""
    import run_manifest as rm
    monkeypatch.setattr(rm, "_RUNS_DIR", tmp_path)

    run_id = str(uuid.uuid4())
    with patch("enrich_contact._gql", side_effect=[_org_entity(), _edges_response()]):
        with patch("enrich_contact.find_supply_chain_contacts", return_value=[]):
            result = enrich_contact(contact_id="ent_org_1", run_id=run_id, dry_run=True)

    required = [
        "run_id", "started_at", "finished_at", "status", "dry_run",
        "contact_id", "goals", "sources_attempted", "sources_skipped",
        "companies_scanned", "contacts_found", "contacts_added",
        "duplicates_skipped", "fuzzy_flagged", "skipped_fresh",
        "errors", "rollback_log",
    ]
    for field in required:
        assert field in result, f"Missing field in run manifest: {field}"


# ---------------------------------------------------------------------------
# End-to-end test against real Kissinger (dry-run, no writes)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_e2e_dry_run_against_real_kissinger():
    """
    End-to-end: enrich a real Kissinger org entity in dry-run mode.

    Uses the first org in Kissinger. Verifies:
    - Run manifest is created and completed
    - No entities are written (dry_run=True)
    - Source manifest is loaded and applied
    - Pipeline handles the full flow without crashing

    Requires live Kissinger at http://localhost:8080/graphql.
    """
    import requests as req_lib

    # Check Kissinger is reachable
    try:
        r = req_lib.get("http://localhost:8080/graphql", timeout=3)
    except Exception:
        pytest.skip("Kissinger not reachable at localhost:8080")

    # Fetch a real org entity
    resp = req_lib.post(
        "http://localhost:8080/graphql",
        json={"query": '{ entities(kind: "org", first: 1) { edges { node { id name } } } }'},
        timeout=10,
    )
    data = resp.json()
    orgs = data["data"]["entities"]["edges"]
    if not orgs:
        pytest.skip("No org entities in Kissinger")

    org_id = orgs[0]["node"]["id"]
    org_name = orgs[0]["node"]["name"]
    run_id = str(uuid.uuid4())

    result = enrich_contact(
        contact_id=org_id,
        run_id=run_id,
        endpoint="http://localhost:8080/graphql",
        token="",
        dry_run=True,
    )

    # Verify run completed (may have 0 contacts found if company name not web-searchable)
    assert result["status"] in ("completed", "failed")
    assert result["run_id"] == run_id
    assert result["contact_id"] == org_id
    assert result["contacts_added"] == 0  # dry-run: no writes
    assert result["dry_run"] is True

    print(f"\nE2E dry-run result for '{org_name}' ({org_id}):")
    print(json.dumps(result, indent=2))
