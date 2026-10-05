"""Tests for get_person_context alias resolution (case-insensitive, slug, unique prefix)."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

import src.mcp.inbox_server as inbox_server


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    people = tmp_path / "people"
    people.mkdir()
    (people / "alex-rivera.md").write_text("# Alex Rivera\n")
    (people / "sam.md").write_text("# Sam\n")
    (people / "kai.md").write_text("# Kai\n")
    (people / "kat.md").write_text("# Kat\n")
    return tmp_path


def _call(person: str, canonical: Path) -> str:
    with patch.object(inbox_server, "CANONICAL_DIR", canonical):
        result = asyncio.run(inbox_server.handle_get_person_context({"person": person}))
    return result[0].text


@pytest.mark.parametrize("name", ["alex-rivera", "Alex Rivera", "alex", "ALEX", "Alex-Rivera"])
def test_aliases_resolve_to_full_slug(name, canonical):
    assert _call(name, canonical) == "# Alex Rivera\n"


def test_case_insensitive_exact(canonical):
    assert _call("Sam", canonical) == "# Sam\n"


def test_ambiguous_prefix_not_resolved(canonical):
    # "ka" matches both kai and kat -> keep existing error text
    text = _call("ka", canonical)
    assert text.startswith("No person file for 'ka'. Available:")


def test_unknown_person_keeps_error(canonical):
    text = _call("zelda", canonical)
    assert text.startswith("No person file for 'zelda'. Available:")
    assert "alex-rivera" in text


def test_path_traversal_still_rejected(canonical):
    assert _call("../sam", canonical) == "Error: invalid person name."
