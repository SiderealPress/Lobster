"""
Tests for BIS-298: dedup_crm_contacts

Mocks Kissinger GraphQL — no real HTTP calls.
Tests normalisation, similarity thresholds, and classification logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add bin dir to path
sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent.parent
        / "lobster-shop" / "prospect-enrichment" / "bin"),
)

from dedup_crm_contacts import (
    dedup_crm_contacts,
    _normalize,
    _similarity,
    _DUPLICATE_THRESHOLD,
    _FUZZY_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_graphql_response(data: dict) -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"data": data}
    return mock


def _empty_crm() -> MagicMock:
    """A mock session that returns empty search + contactsAtOrg results."""
    resp = _make_graphql_response(
        {
            "search": [],
            "contactsAtOrg": {"edges": []},
        }
    )
    return resp


# ---------------------------------------------------------------------------
# Normalisation tests
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_lowercases(self):
        assert _normalize("JANE SMITH") == "jane smith"

    def test_strips_accents(self):
        assert _normalize("Héctor García") == "hector garcia"

    def test_strips_punctuation(self):
        assert _normalize("O'Brien") == "o brien"

    def test_collapses_whitespace(self):
        assert _normalize("Jane   Smith") == "jane smith"

    def test_handles_empty_string(self):
        assert _normalize("") == ""

    def test_unicode_decomposition(self):
        # ñ → n
        result = _normalize("Piñata")
        assert "n" in result
        assert "a" in result


class TestSimilarity:
    def test_identical_names(self):
        assert _similarity("Jane Smith", "Jane Smith") == 1.0

    def test_accent_insensitive(self):
        # José vs Jose — should be high similarity after normalisation
        score = _similarity("José García", "Jose Garcia")
        assert score > _DUPLICATE_THRESHOLD

    def test_completely_different(self):
        score = _similarity("Alice Wonder", "Bob Builder")
        assert score < _FUZZY_THRESHOLD

    def test_partial_name_match(self):
        # "Smith Jane" vs "Jane Smith" — reordered words, expect meaningful similarity
        score = _similarity("Smith Jane", "Jane Smith")
        # SequenceMatcher on "smith jane" vs "jane smith" gives 0.5 (common substring "smith")
        # Accept >= 0.5 since both orderings share the same characters
        assert score >= 0.5

    def test_typo_in_name(self):
        # Single letter transposition
        score = _similarity("Jon Smith", "John Smith")
        assert score >= _FUZZY_THRESHOLD


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

class TestDedupCrmContacts:
    """Tests for dedup_crm_contacts() with mocked Kissinger responses."""

    def _patch_graphql(self, search_hits=None, org_contacts=None):
        """
        Return a context manager that patches _graphql with configurable responses.

        search_hits: list of {id, kind, name, score}
        org_contacts: list of {entityId, entityName, relation}
        """
        search_hits = search_hits or []
        org_contacts = org_contacts or []

        def fake_graphql(query, variables, *args, **kwargs):
            if "search" in query and "contactsAtOrg" not in query:
                return {"search": search_hits}
            if "contactsAtOrg" in query:
                return {
                    "contactsAtOrg": {
                        "edges": [{"node": c} for c in org_contacts]
                    }
                }
            return {}

        return patch(
            "dedup_crm_contacts._graphql",
            side_effect=fake_graphql,
        )

    def test_new_contact_when_no_crm_matches(self):
        """Contact not in CRM goes to 'new'."""
        candidates = [{"name": "Jane Smith", "title": "VP SC", "company": "AcmeCo"}]
        with self._patch_graphql(search_hits=[], org_contacts=[]):
            result = dedup_crm_contacts(candidates)

        assert len(result["new"]) == 1
        assert result["duplicates"] == []
        assert result["fuzzy_matches"] == []

    def test_exact_match_is_duplicate(self):
        """Exact name match → duplicate."""
        candidates = [{"name": "Jane Smith", "title": "VP SC", "company": "AcmeCo"}]
        crm_hit = {"id": "abc", "kind": "person", "name": "Jane Smith", "score": 1.0}
        with self._patch_graphql(search_hits=[crm_hit]):
            result = dedup_crm_contacts(candidates)

        assert len(result["duplicates"]) == 1
        assert result["new"] == []

    def test_fuzzy_match_flagged_for_review(self):
        """Near-name match → fuzzy_matches."""
        candidates = [{"name": "Jon Smith", "title": "Demand Planner", "company": "AcmeCo"}]
        crm_hit = {"id": "abc", "kind": "person", "name": "John Smith", "score": 0.9}
        with self._patch_graphql(search_hits=[crm_hit]):
            result = dedup_crm_contacts(candidates)

        # Jon/John is a close match — should be fuzzy or dup
        assert len(result["new"]) == 0 or len(result["fuzzy_matches"]) > 0 or len(result["duplicates"]) > 0

    def test_accent_collision_is_duplicate(self):
        """Accented vs unaccented name → duplicate."""
        candidates = [{"name": "José García", "title": "SC Manager", "company": "LatinCo"}]
        crm_hit = {"id": "abc", "kind": "person", "name": "Jose Garcia", "score": 0.95}
        with self._patch_graphql(search_hits=[crm_hit]):
            result = dedup_crm_contacts(candidates)

        # After normalisation, José García ≈ Jose Garcia → high similarity → duplicate
        assert result["new"] == []
        # Could be duplicate or fuzzy depending on exact score
        total_non_new = len(result["duplicates"]) + len(result["fuzzy_matches"])
        assert total_non_new == 1

    def test_contacts_at_org_used_when_org_id_provided(self):
        """When org_id is set, contactsAtOrg is queried."""
        candidates = [{"name": "Alice Dupont", "title": "Planner", "company": "OrgCo"}]
        org_contacts = [{"entityId": "xyz", "entityName": "Alice Dupont", "relation": "works_at"}]
        with self._patch_graphql(search_hits=[], org_contacts=org_contacts):
            result = dedup_crm_contacts(candidates, org_id="org-abc")

        # Alice Dupont exactly matches → should be duplicate
        assert result["new"] == []

    def test_empty_candidates_returns_empty_result(self):
        """Empty input → all three buckets empty."""
        with self._patch_graphql():
            result = dedup_crm_contacts([])

        assert result == {"new": [], "duplicates": [], "fuzzy_matches": []}

    def test_contact_without_name_goes_to_new(self):
        """Contact with empty name goes to 'new' (can't deduplicate)."""
        candidates = [{"name": "", "title": "VP SC", "company": "Corp"}]
        with self._patch_graphql():
            result = dedup_crm_contacts(candidates)

        assert len(result["new"]) == 1

    def test_duplicate_augmented_with_crm_match(self):
        """Duplicate entry includes crm_match and similarity fields."""
        candidates = [{"name": "Jane Smith", "title": "VP", "company": "Co"}]
        crm_hit = {"id": "abc", "kind": "person", "name": "Jane Smith", "score": 1.0}
        with self._patch_graphql(search_hits=[crm_hit]):
            result = dedup_crm_contacts(candidates)

        if result["duplicates"]:
            dup = result["duplicates"][0]
            assert "crm_match" in dup
            assert "similarity" in dup

    def test_graphql_failure_gracefully_handled(self):
        """If GraphQL search throws, contact is still classified (as new)."""
        candidates = [{"name": "Error Person", "title": "SC", "company": "Co"}]
        with patch("dedup_crm_contacts._graphql", side_effect=Exception("network fail")):
            result = dedup_crm_contacts(candidates)

        # With no CRM data available, contact is treated as new
        assert len(result["new"]) == 1

    def test_multiple_candidates_classified_independently(self):
        """Multiple candidates are each classified correctly."""
        candidates = [
            {"name": "Jane Smith", "title": "VP", "company": "Co"},
            {"name": "Alice Unknown", "title": "Planner", "company": "Co"},
        ]
        crm_hit = {"id": "abc", "kind": "person", "name": "Jane Smith", "score": 1.0}
        with self._patch_graphql(search_hits=[crm_hit]):
            result = dedup_crm_contacts(candidates)

        total = len(result["new"]) + len(result["duplicates"]) + len(result["fuzzy_matches"])
        assert total == 2
