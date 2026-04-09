"""
Tests for BIS-299: add_contacts_provenance

Mocks Kissinger GraphQL mutations — no real HTTP calls.
Verifies payload structure and provenance metadata.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Add bin dir to path
sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent.parent
        / "lobster-shop" / "prospect-enrichment" / "bin"),
)

from add_contacts_provenance import add_contacts_provenance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_graphql_side_effect(entity_id: str = "new-entity-abc"):
    """
    Return a side_effect function that answers createEntity then createEdge.
    """
    call_count = {"n": 0}

    def side_effect(query, variables, *args, **kwargs):
        call_count["n"] += 1
        if "createEntity" in query:
            return {"createEntity": {"id": entity_id, "name": "test", "kind": "person", "tags": []}}
        if "createEdge" in query:
            return {"createEdge": {"id": "edge-1", "source": entity_id, "target": "org-1", "relation": "works_at"}}
        return {}

    return side_effect


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAddContactsProvenance:
    def test_creates_entity_with_correct_kind(self):
        """Entity is created with kind=person."""
        contacts = [
            {
                "name": "Jane Smith",
                "title": "VP Supply Chain",
                "company": "Acme",
                "source_url": "https://linkedin.com/in/jane-smith",
                "org_kissinger_id": "org-abc",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect()) as mock_gql:
            results = add_contacts_provenance(contacts)

        # First call should be createEntity
        first_call = mock_gql.call_args_list[0]
        query = first_call[0][0]
        variables = first_call[0][1]
        assert "createEntity" in query
        assert variables["input"]["kind"] == "person"

    def test_entity_has_required_tags(self):
        """Entity tags include supply-chain and prospect-enrichment."""
        contacts = [
            {
                "name": "Bob Jones",
                "title": "Demand Planner",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/bob-jones",
                "org_kissinger_id": "org-xyz",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect()) as mock_gql:
            add_contacts_provenance(contacts)

        first_call = mock_gql.call_args_list[0]
        tags = first_call[0][1]["input"]["tags"]
        assert "supply-chain" in tags
        assert "prospect-enrichment" in tags

    def test_provenance_meta_is_wallace(self):
        """Provenance meta entry has value 'wallace'."""
        contacts = [
            {
                "name": "Alice Lee",
                "title": "SC Director",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/alice-lee",
                "org_kissinger_id": "org-1",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect()) as mock_gql:
            add_contacts_provenance(contacts)

        first_call = mock_gql.call_args_list[0]
        meta = first_call[0][1]["input"]["meta"]
        prov = next((m for m in meta if m["key"] == "provenance"), None)
        assert prov is not None
        assert prov["value"] == "wallace"

    def test_enriched_at_is_set(self):
        """enriched_at meta entry is present and looks like ISO-8601."""
        contacts = [
            {
                "name": "Carol Tran",
                "title": "Manager",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/carol",
                "org_kissinger_id": "org-1",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect()) as mock_gql:
            add_contacts_provenance(contacts)

        first_call = mock_gql.call_args_list[0]
        meta = first_call[0][1]["input"]["meta"]
        ts = next((m for m in meta if m["key"] == "enriched_at"), None)
        assert ts is not None
        assert "T" in ts["value"]  # ISO-8601 has T separator

    def test_title_included_in_meta(self):
        """Title is written to meta entry."""
        contacts = [
            {
                "name": "Dave Park",
                "title": "VP Supply Chain",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/dave",
                "org_kissinger_id": "org-1",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect()) as mock_gql:
            add_contacts_provenance(contacts)

        first_call = mock_gql.call_args_list[0]
        meta = first_call[0][1]["input"]["meta"]
        title_entry = next((m for m in meta if m["key"] == "title"), None)
        assert title_entry is not None
        assert title_entry["value"] == "VP Supply Chain"

    def test_source_url_in_meta(self):
        """source_url is written to meta entry."""
        contacts = [
            {
                "name": "Eve Roy",
                "title": "Director",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/eve-roy",
                "org_kissinger_id": "org-1",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect()) as mock_gql:
            add_contacts_provenance(contacts)

        first_call = mock_gql.call_args_list[0]
        meta = first_call[0][1]["input"]["meta"]
        url_entry = next((m for m in meta if m["key"] == "source_url"), None)
        assert url_entry is not None
        assert url_entry["value"] == "https://linkedin.com/in/eve-roy"

    def test_creates_edge_works_at_org(self):
        """createEdge is called with relation=works_at to org_kissinger_id."""
        entity_id = "new-entity-555"
        contacts = [
            {
                "name": "Frank Kim",
                "title": "Planner",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/frank",
                "org_kissinger_id": "org-target",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect(entity_id)) as mock_gql:
            add_contacts_provenance(contacts)

        # Second call should be createEdge
        second_call = mock_gql.call_args_list[1]
        query = second_call[0][0]
        variables = second_call[0][1]
        assert "createEdge" in query
        assert variables["input"]["source"] == entity_id
        assert variables["input"]["target"] == "org-target"
        assert variables["input"]["relation"] == "works_at"

    def test_dry_run_skips_graphql(self):
        """dry_run=True makes no GraphQL calls."""
        contacts = [
            {
                "name": "Grace Liu",
                "title": "VP",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/grace",
                "org_kissinger_id": "org-1",
            }
        ]
        with patch("add_contacts_provenance._graphql") as mock_gql:
            results = add_contacts_provenance(contacts, dry_run=True)

        mock_gql.assert_not_called()
        assert results[0]["dry_run"] is True
        assert results[0]["entity_id"] is None

    def test_missing_name_returns_error(self):
        """Contact without a name gets an error result (no GraphQL call)."""
        contacts = [{"name": "", "title": "VP", "company": "Corp", "org_kissinger_id": "org-1"}]
        with patch("add_contacts_provenance._graphql") as mock_gql:
            results = add_contacts_provenance(contacts)

        mock_gql.assert_not_called()
        assert results[0]["error"] is not None
        assert "name" in results[0]["error"].lower()

    def test_createentity_failure_recorded_in_result(self):
        """If createEntity fails, error is recorded and no createEdge attempted."""
        contacts = [
            {
                "name": "Henry Wu",
                "title": "Manager",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/henry",
                "org_kissinger_id": "org-1",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=RuntimeError("DB down")) as mock_gql:
            results = add_contacts_provenance(contacts)

        assert results[0]["error"] is not None
        assert "createEntity" in results[0]["error"]
        # Should only have been called once (the createEntity call, which failed)
        assert mock_gql.call_count == 1

    def test_createedge_failure_recorded_non_fatal(self):
        """If createEdge fails, entity_id is still set and error is noted."""
        entity_id = "ent-999"

        def side_effect(query, variables, *args, **kwargs):
            if "createEntity" in query:
                return {"createEntity": {"id": entity_id, "name": "test", "kind": "person", "tags": []}}
            raise RuntimeError("edge failed")

        contacts = [
            {
                "name": "Iris Ng",
                "title": "Planner",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/iris",
                "org_kissinger_id": "org-1",
            }
        ]
        with patch("add_contacts_provenance._graphql", side_effect=side_effect):
            results = add_contacts_provenance(contacts)

        assert results[0]["entity_id"] == entity_id
        assert results[0]["edge_created"] is False
        assert results[0]["error"] is not None

    def test_no_org_id_skips_edge(self):
        """Without org_kissinger_id, only createEntity is called."""
        contacts = [
            {
                "name": "Jack Ma",
                "title": "SC VP",
                "company": "Corp",
                "source_url": "https://linkedin.com/in/jack",
            }  # no org_kissinger_id
        ]
        with patch("add_contacts_provenance._graphql", side_effect=_make_graphql_side_effect()) as mock_gql:
            results = add_contacts_provenance(contacts)

        # Only createEntity called — no createEdge
        assert mock_gql.call_count == 1
        assert results[0]["edge_created"] is False

    def test_multiple_contacts_processed(self):
        """Multiple contacts each get their own entity + edge."""
        contacts = [
            {"name": "Person A", "title": "VP", "company": "Co", "source_url": "https://linkedin.com/in/a", "org_kissinger_id": "org-1"},
            {"name": "Person B", "title": "Dir", "company": "Co", "source_url": "https://linkedin.com/in/b", "org_kissinger_id": "org-1"},
        ]
        call_n = {"n": 0}

        def side_effect(query, variables, *args, **kwargs):
            call_n["n"] += 1
            if "createEntity" in query:
                return {"createEntity": {"id": f"ent-{call_n['n']}", "name": "x", "kind": "person", "tags": []}}
            return {"createEdge": {"id": "e", "source": "s", "target": "t", "relation": "works_at"}}

        with patch("add_contacts_provenance._graphql", side_effect=side_effect):
            results = add_contacts_provenance(contacts)

        assert len(results) == 2
        # Each non-erroring result should have an entity_id
        for r in results:
            if not r.get("error"):
                assert r["entity_id"] is not None
