"""
Unit tests for score_contact.py

Tests cover:
- Empty / minimal contacts
- Title / seniority scoring
- Org type scoring
- Interaction recency (including stale contacts)
- Network proximity
- Record completeness
- End-to-end: high-value CSCO at a prospect org scores 70+
- End-to-end: stale, incomplete contact at unknown org scores <30
"""

import pytest
from datetime import datetime, timezone, timedelta

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from scoring.score_contact import (
    score_contact,
    _score_title_relevance,
    _score_seniority,
    _score_org_type,
    _score_interaction_recency,
    _score_network_proximity,
    _score_record_completeness,
    WEIGHTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_contact(**kwargs):
    """Minimal valid contact dict."""
    base = {
        "id": "test-001",
        "name": "Test Person",
        "kind": "person",
        "tags": [],
        "notes": "",
        "meta": [],
        "updatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "edges": [],
    }
    base.update(kwargs)
    return base


def days_ago(n: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Weights sanity
# ---------------------------------------------------------------------------

def test_weights_sum_to_one():
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Empty / minimal contact
# ---------------------------------------------------------------------------

def test_empty_contact_returns_score():
    result = score_contact({})
    assert "score" in result
    assert 0 <= result["score"] <= 100
    assert "breakdown" in result


def test_no_title_no_org():
    c = make_contact()
    result = score_contact(c)
    assert result["score"] < 30, "Unknown contact with no data should score low"


def test_score_structure():
    result = score_contact(make_contact())
    bd = result["breakdown"]
    expected_keys = {
        "title_relevance", "seniority", "org_type",
        "interaction_recency", "network_proximity", "record_completeness"
    }
    assert set(bd.keys()) == expected_keys
    for k, v in bd.items():
        assert "raw" in v
        assert "weight" in v
        assert "weighted" in v
        assert 0.0 <= v["raw"] <= 1.0


# ---------------------------------------------------------------------------
# Title relevance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected_min", [
    ("Chief Supply Chain Officer", 0.9),
    ("CSCO", 0.9),
    ("VP Supply Chain", 0.9),
    ("Director of Supply Chain", 0.9),
    ("Demand Planner", 0.9),
    ("Supply Chain Planner", 0.9),
    ("Head of Procurement", 0.9),
    ("VP Operations", 0.5),           # medium-tier
    ("CEO", 0.5),                      # economic buyer, medium tier
    ("Software Engineer", 0.0),        # no match
    ("", 0.0),
])
def test_title_relevance(title, expected_min):
    c = make_contact(meta=[{"key": "title", "value": title}])
    score = _score_title_relevance(c)
    assert score >= expected_min, f"title={title!r} expected >= {expected_min}, got {score}"


def test_title_relevance_case_insensitive():
    c = make_contact(meta=[{"key": "title", "value": "vp supply chain"}])
    assert _score_title_relevance(c) >= 0.9


def test_title_relevance_no_title():
    c = make_contact()
    assert _score_title_relevance(c) == 0.0


def test_title_in_tags():
    c = make_contact(tags=["supply-chain", "linkedin"])
    assert _score_title_relevance(c) >= 0.9


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected_min", [
    ("CEO", 0.9),
    ("Chief Supply Chain Officer", 0.9),
    ("Founder", 0.9),
    ("VP of Engineering", 0.75),
    ("SVP Operations", 0.75),
    ("Director of Supply Chain", 0.60),
    ("Head of Planning", 0.60),
    ("Senior Manager", 0.35),
    ("Lead Planner", 0.35),
    ("Analyst", 0.15),
    ("", 0.0),
])
def test_seniority(title, expected_min):
    c = make_contact(meta=[{"key": "title", "value": title}])
    score = _score_seniority(c)
    assert score >= expected_min, f"title={title!r} expected >= {expected_min}, got {score}"


# ---------------------------------------------------------------------------
# Org type
# ---------------------------------------------------------------------------

def test_org_type_prospect_tag():
    c = make_contact(tags=["prospect"])
    assert _score_org_type(c) >= 0.9


def test_org_type_eloso_tag():
    c = make_contact(tags=["eloso"])
    assert _score_org_type(c) >= 0.9


def test_org_type_vc():
    c = make_contact(tags=["vc", "seed"])
    score = _score_org_type(c)
    assert 0.3 <= score <= 0.6


def test_org_type_ally():
    c = make_contact(tags=["ally"])
    score = _score_org_type(c)
    assert score >= 0.6


def test_org_type_unknown():
    c = make_contact(tags=["linkedin"])
    score = _score_org_type(c)
    assert score < 0.3


def test_org_type_via_org_tags():
    c = make_contact(tags=[], org_tags=["prospect"])
    assert _score_org_type(c) >= 0.9


# ---------------------------------------------------------------------------
# Interaction recency
# ---------------------------------------------------------------------------

def test_recency_very_recent():
    c = make_contact(last_interaction_at=days_ago(10))
    score = _score_interaction_recency(c)
    assert score == 1.0


def test_recency_recent():
    c = make_contact(last_interaction_at=days_ago(60))
    score = _score_interaction_recency(c)
    assert 0.65 <= score <= 1.0


def test_recency_stale():
    c = make_contact(last_interaction_at=days_ago(150))
    score = _score_interaction_recency(c)
    assert 0.30 <= score <= 0.60


def test_recency_cold():
    c = make_contact(last_interaction_at=days_ago(300))
    score = _score_interaction_recency(c)
    assert 0.10 <= score <= 0.35


def test_recency_ancient():
    c = make_contact(last_interaction_at=days_ago(500))
    score = _score_interaction_recency(c)
    assert score <= 0.10


def test_recency_no_interaction_fallback_to_updated_at():
    # No last_interaction_at — fall back to updatedAt (capped at 0.6)
    c = make_contact(updatedAt=days_ago(5))
    score = _score_interaction_recency(c)
    assert score <= 0.6  # capped


def test_recency_no_dates():
    c = make_contact(updatedAt=None)
    score = _score_interaction_recency(c)
    assert score == 0.1


# ---------------------------------------------------------------------------
# Network proximity
# ---------------------------------------------------------------------------

def test_proximity_no_edges():
    c = make_contact(edges=[])
    score = _score_network_proximity(c)
    assert score == 0.1


def test_proximity_ally_edge():
    c = make_contact(edges=[{"relation": "ally", "strength": 0.8, "target_tags": []}])
    score = _score_network_proximity(c)
    assert score >= 0.85


def test_proximity_works_at_prospect():
    c = make_contact(edges=[{
        "relation": "works_at",
        "strength": 0.9,
        "target_tags": ["prospect"]
    }])
    score = _score_network_proximity(c)
    assert score >= 0.60


def test_proximity_works_at_unknown_org():
    c = make_contact(edges=[{
        "relation": "works_at",
        "strength": 0.5,
        "target_tags": ["linkedin"]
    }])
    score = _score_network_proximity(c)
    # Should be lower than prospect
    assert 0.20 <= score <= 0.55


# ---------------------------------------------------------------------------
# Record completeness
# ---------------------------------------------------------------------------

def test_completeness_full_record():
    c = make_contact(
        meta=[
            {"key": "email", "value": "alice@example.com"},
            {"key": "title", "value": "VP Supply Chain"},
            {"key": "company", "value": "Acme Corp"},
            {"key": "linkedin_url", "value": "https://linkedin.com/in/alice"},
        ],
        notes="Very relevant contact for Q3",
    )
    score = _score_record_completeness(c)
    assert score == 1.0


def test_completeness_email_only():
    c = make_contact(meta=[{"key": "email", "value": "bob@example.com"}])
    score = _score_record_completeness(c)
    assert 0.30 <= score <= 0.40


def test_completeness_empty():
    c = make_contact(meta=[], notes="")
    score = _score_record_completeness(c)
    assert score == 0.0


def test_completeness_company_via_edge():
    c = make_contact(
        meta=[{"key": "email", "value": "x@y.com"}],
        edges=[{"relation": "works_at", "strength": 0.5, "target_tags": []}]
    )
    score = _score_record_completeness(c)
    assert score >= 0.55  # email (0.35) + company via edge (0.20)


# ---------------------------------------------------------------------------
# End-to-end: high-value contact
# ---------------------------------------------------------------------------

def test_high_value_csco_at_prospect():
    """CSCO at a prospect org, recently contacted, full record → should score 70+"""
    c = {
        "id": "csco-001",
        "name": "Alice Chen",
        "kind": "person",
        "tags": ["eloso", "prospect-contact"],
        "notes": "Spoke at SCC conference. Very interested in backlog mgmt.",
        "meta": [
            {"key": "email", "value": "alice@heavymfg.com"},
            {"key": "title", "value": "Chief Supply Chain Officer"},
            {"key": "company", "value": "Heavy Mfg Co"},
            {"key": "linkedin_url", "value": "https://linkedin.com/in/alicechen"},
        ],
        "last_interaction_at": days_ago(15),
        "edges": [
            {
                "relation": "works_at",
                "strength": 1.0,
                "target_tags": ["prospect"],
            }
        ],
        "org_tags": ["prospect"],
        "updatedAt": days_ago(15),
    }
    result = score_contact(c)
    assert result["score"] >= 70, f"Expected >=70, got {result['score']}. Breakdown: {result['breakdown']}"


def test_demand_planner_at_prospect():
    """Demand Planner at prospect → should score 60+"""
    c = {
        "id": "dp-001",
        "name": "Bob Smith",
        "kind": "person",
        "tags": ["prospect-contact"],
        "notes": "",
        "meta": [
            {"key": "email", "value": "bob@mfg.com"},
            {"key": "title", "value": "Senior Demand Planner"},
            {"key": "company", "value": "Mfg Corp"},
        ],
        "last_interaction_at": days_ago(45),
        "edges": [{"relation": "works_at", "strength": 0.8, "target_tags": ["prospect"]}],
        "org_tags": ["prospect"],
        "updatedAt": days_ago(45),
    }
    result = score_contact(c)
    assert result["score"] >= 60, f"Expected >=60, got {result['score']}"


# ---------------------------------------------------------------------------
# End-to-end: low-value contact
# ---------------------------------------------------------------------------

def test_low_value_stale_incomplete():
    """Unknown engineer, stale, no email → should score under 30"""
    c = {
        "id": "anon-001",
        "name": "John Doe",
        "kind": "person",
        "tags": ["linkedin"],
        "notes": "",
        "meta": [{"key": "title", "value": "Software Engineer"}],
        "updatedAt": days_ago(400),
        "edges": [],
        "org_tags": [],
    }
    result = score_contact(c)
    assert result["score"] < 30, f"Expected <30, got {result['score']}"


# ---------------------------------------------------------------------------
# Score bounds
# ---------------------------------------------------------------------------

def test_score_always_in_range():
    contacts = [
        make_contact(),
        make_contact(tags=["prospect"], meta=[{"key": "title", "value": "CEO"}]),
        make_contact(tags=["vc"], meta=[{"key": "email", "value": "x@y.com"}]),
        {},
    ]
    for c in contacts:
        result = score_contact(c)
        assert 0 <= result["score"] <= 100
