"""
Tests for manifest_loader.py — Slice 1 validation.

Run: python -m pytest tests/test_manifest.py -v
(from ~/lobster/lobster-shop/prospect-enrichment/)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add bin to path
sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))

from manifest_loader import (
    ManifestError,
    available_sources_for_goal,
    confidence_from_score,
    hash_response,
    load_manifest,
    now_iso,
)

_REAL_MANIFEST = Path(__file__).parent.parent / "sources" / "manifest.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_manifest(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(data))
    return p


def _minimal_source(**overrides) -> dict:
    base = {
        "source_id": "test_src",
        "display_name": "Test Source",
        "api_key_env": None,
        "available": True,
        "goals": ["org_chart"],
        "goal_scores": {"org_chart": 0.8},
        "rate_limits": {"requests_per_minute": 10, "requests_per_day": 100},
        "cost_per_call": 0.0,
        "data_freshness_days": 30,
        "requires_company": True,
        "requires_person": False,
        "output_fields": ["name", "title"],
        "notes": "Test source",
    }
    base.update(overrides)
    return base


def _minimal_manifest(**src_overrides) -> dict:
    return {
        "_schema_version": "1.0.0",
        "sources": [_minimal_source(**src_overrides)],
        "goal_definitions": {},
        "source_selection_strategy": {"org_chart": ["test_src"], "work_history": [], "connections": []},
    }


# ---------------------------------------------------------------------------
# Test: Real manifest loads without error
# ---------------------------------------------------------------------------

def test_real_manifest_loads():
    """The committed manifest.json must load cleanly."""
    manifest = load_manifest(_REAL_MANIFEST)
    assert "sources" in manifest
    assert len(manifest["sources"]) > 0


def test_real_manifest_all_required_fields():
    """Every source in the real manifest has all required fields."""
    from manifest_loader import REQUIRED_FIELDS
    manifest = load_manifest(_REAL_MANIFEST)
    for source in manifest["sources"]:
        missing = REQUIRED_FIELDS - set(source.keys())
        assert not missing, f"Source '{source['source_id']}' missing: {sorted(missing)}"


def test_real_manifest_available_sources_have_valid_goals():
    """All available sources cover at least one valid goal."""
    from manifest_loader import VALID_GOALS
    manifest = load_manifest(_REAL_MANIFEST)
    for source in manifest["sources"]:
        if source["available"]:
            assert source["goals"], f"Source '{source['source_id']}' is available but has no goals"
            for g in source["goals"]:
                assert g in VALID_GOALS, f"Source '{source['source_id']}' has unknown goal '{g}'"


def test_real_manifest_keyless_sources_always_available():
    """Sources with api_key_env=null must be marked available."""
    manifest = load_manifest(_REAL_MANIFEST)
    for source in manifest["sources"]:
        if source["api_key_env"] is None:
            assert source["available"], (
                f"Source '{source['source_id']}' has no api_key_env but is marked unavailable"
            )


# ---------------------------------------------------------------------------
# Test: Availability from environment
# ---------------------------------------------------------------------------

def test_availability_from_env_present(tmp_path, monkeypatch):
    """Source with api_key_env becomes available when env var is set."""
    monkeypatch.setenv("MY_TEST_KEY", "somevalue")
    p = _write_manifest(tmp_path, _minimal_manifest(api_key_env="MY_TEST_KEY", available=False))
    manifest = load_manifest(p)
    assert manifest["sources"][0]["available"] is True


def test_availability_from_env_absent(tmp_path, monkeypatch):
    """Source with api_key_env becomes unavailable when env var is missing."""
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    p = _write_manifest(tmp_path, _minimal_manifest(api_key_env="MY_TEST_KEY", available=True))
    manifest = load_manifest(p)
    assert manifest["sources"][0]["available"] is False


def test_no_api_key_always_available(tmp_path):
    """Source with api_key_env=null is always available."""
    p = _write_manifest(tmp_path, _minimal_manifest(api_key_env=None))
    manifest = load_manifest(p)
    assert manifest["sources"][0]["available"] is True


# ---------------------------------------------------------------------------
# Test: Schema validation
# ---------------------------------------------------------------------------

def test_missing_required_field_raises(tmp_path):
    """Manifest with a source missing a required field raises ManifestError."""
    src = _minimal_source()
    del src["data_freshness_days"]
    p = _write_manifest(tmp_path, {"sources": [src], "goal_definitions": {}, "source_selection_strategy": {}})
    with pytest.raises(ManifestError, match="missing required fields"):
        load_manifest(p)


def test_unknown_goal_raises(tmp_path):
    """Source declaring unknown goal raises ManifestError."""
    p = _write_manifest(tmp_path, _minimal_manifest(goals=["bad_goal"], goal_scores={"bad_goal": 0.5}))
    with pytest.raises(ManifestError, match="unknown goal"):
        load_manifest(p)


def test_missing_goal_score_raises(tmp_path):
    """Source with goal declared but no goal_score raises ManifestError."""
    p = _write_manifest(tmp_path, _minimal_manifest(goals=["org_chart", "work_history"], goal_scores={"org_chart": 0.8}))
    with pytest.raises(ManifestError, match="missing goal_score for"):
        load_manifest(p)


def test_empty_sources_raises(tmp_path):
    """Manifest with empty sources list raises ManifestError."""
    p = _write_manifest(tmp_path, {"sources": [], "goal_definitions": {}, "source_selection_strategy": {}})
    with pytest.raises(ManifestError, match="non-empty list"):
        load_manifest(p)


def test_missing_file_raises():
    """Loading a nonexistent manifest raises ManifestError."""
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(Path("/tmp/nonexistent_manifest_xyz.json"))


# ---------------------------------------------------------------------------
# Test: available_sources_for_goal
# ---------------------------------------------------------------------------

def test_available_sources_sorted_by_score(tmp_path):
    """available_sources_for_goal returns sources sorted by goal_score descending."""
    manifest = {
        "sources": [
            _minimal_source(source_id="a", goal_scores={"org_chart": 0.4}, api_key_env=None),
            _minimal_source(source_id="b", goal_scores={"org_chart": 0.9}, api_key_env=None),
            _minimal_source(source_id="c", goal_scores={"org_chart": 0.6}, api_key_env=None),
        ],
        "goal_definitions": {},
        "source_selection_strategy": {"org_chart": ["b", "c", "a"]},
    }
    p = _write_manifest(tmp_path, manifest)
    loaded = load_manifest(p)
    sources = available_sources_for_goal(loaded, "org_chart")
    ids = [s["source_id"] for s in sources]
    assert ids == ["b", "c", "a"]


def test_unavailable_sources_excluded(tmp_path, monkeypatch):
    """Unavailable sources are excluded from goal source list."""
    monkeypatch.delenv("FAKE_KEY_XYZ", raising=False)
    manifest = {
        "sources": [
            _minimal_source(source_id="avail", api_key_env=None),
            _minimal_source(source_id="unavail", api_key_env="FAKE_KEY_XYZ"),
        ],
        "goal_definitions": {},
        "source_selection_strategy": {"org_chart": []},
    }
    p = _write_manifest(tmp_path, manifest)
    loaded = load_manifest(p)
    sources = available_sources_for_goal(loaded, "org_chart")
    ids = [s["source_id"] for s in sources]
    assert "avail" in ids
    assert "unavail" not in ids


def test_unknown_goal_raises_value_error():
    """Requesting an unknown goal raises ValueError."""
    manifest = {"sources": [], "source_selection_strategy": {}}
    with pytest.raises(ValueError, match="Unknown goal"):
        available_sources_for_goal(manifest, "bad_goal")


# ---------------------------------------------------------------------------
# Test: Utility functions
# ---------------------------------------------------------------------------

def test_confidence_from_score():
    assert confidence_from_score(0.90) == "high"
    assert confidence_from_score(0.75) == "high"
    assert confidence_from_score(0.74) == "medium"
    assert confidence_from_score(0.50) == "medium"
    assert confidence_from_score(0.49) == "low"
    assert confidence_from_score(0.0) == "low"


def test_hash_response_string():
    h = hash_response('{"name": "Jane"}')
    assert h.startswith("sha256:")
    assert len(h) == 7 + 64  # "sha256:" + 64 hex chars


def test_hash_response_bytes():
    h = hash_response(b'raw bytes')
    assert h.startswith("sha256:")


def test_hash_response_deterministic():
    assert hash_response("same") == hash_response("same")
    assert hash_response("a") != hash_response("b")


def test_now_iso_format():
    ts = now_iso()
    # Should be parseable and UTC
    dt = __import__("datetime").datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert dt.tzinfo is not None
