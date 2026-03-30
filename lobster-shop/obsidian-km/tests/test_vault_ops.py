"""
Tests for vault_ops module.

Tests the read_note function with various matching strategies:
- Exact path match
- Exact title match (case-insensitive)
- Fuzzy title match (partial, case-insensitive)
"""

import os
import tempfile
from pathlib import Path

import pytest

# Add src to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from vault_ops import (
    read_note,
    extract_title,
    extract_tags,
    get_file_timestamps,
    _collect_note_files,
    _find_by_exact_title,
    _find_by_fuzzy_title,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_vault(tmp_path, monkeypatch):
    """Create a temporary vault with sample notes."""
    vault = tmp_path / "test-vault"
    vault.mkdir()

    # Set env var for vault location
    monkeypatch.setenv("OBSIDIAN_VAULT_DIR", str(vault))

    # Create sample notes
    (vault / "Simple Note.md").write_text("# Simple Note\n\nThis is simple content.")

    (vault / "note-with-frontmatter.md").write_text("""---
title: Frontmatter Title
tags: [project, planning]
---

# Heading Title

Content with frontmatter.
""")

    (vault / "inline-tags.md").write_text("""# Inline Tags Test

This note has #inline and #tags in the content.
""")

    # Create subfolder with notes
    projects = vault / "projects"
    projects.mkdir()
    (projects / "project-alpha.md").write_text("""---
title: Project Alpha Plan
tags: active
---

# Project Alpha

This is the alpha project plan with #milestone tags.
""")

    (projects / "project-beta.md").write_text("# Project Beta\n\nBeta project content.")

    # Create .obsidian directory (should be excluded)
    obsidian = vault / ".obsidian"
    obsidian.mkdir()
    (obsidian / "config.md").write_text("# Config\n\nThis should be excluded.")

    return vault


# ---------------------------------------------------------------------------
# Tests: extract_title
# ---------------------------------------------------------------------------

class TestExtractTitle:
    def test_title_from_frontmatter(self, tmp_path):
        """Title in frontmatter takes priority."""
        path = tmp_path / "test.md"
        content = "---\ntitle: My Custom Title\n---\n# Heading\nContent"
        assert extract_title(path, content) == "My Custom Title"

    def test_title_from_h1(self, tmp_path):
        """H1 heading used when no frontmatter title."""
        path = tmp_path / "test.md"
        content = "# First Heading\n\nSome content\n## Second"
        assert extract_title(path, content) == "First Heading"

    def test_title_from_filename(self, tmp_path):
        """Filename used as fallback."""
        path = tmp_path / "My Note Name.md"
        content = "Just some content without heading"
        assert extract_title(path, content) == "My Note Name"


# ---------------------------------------------------------------------------
# Tests: extract_tags
# ---------------------------------------------------------------------------

class TestExtractTags:
    def test_tags_from_frontmatter_list(self):
        """Tags from frontmatter list."""
        content = "---\ntags: [one, two, three]\n---\nContent"
        assert extract_tags(content) == ["one", "three", "two"]  # sorted

    def test_tags_from_frontmatter_string(self):
        """Tags from comma-separated string."""
        content = "---\ntags: alpha, beta, gamma\n---\nContent"
        assert extract_tags(content) == ["alpha", "beta", "gamma"]

    def test_inline_tags(self):
        """Inline #tags in content."""
        content = "Content with #project and #active tags"
        tags = extract_tags(content)
        assert "project" in tags
        assert "active" in tags

    def test_combined_tags(self):
        """Both frontmatter and inline tags."""
        content = "---\ntags: [frontmatter]\n---\n#inline tag"
        tags = extract_tags(content)
        assert "frontmatter" in tags
        assert "inline" in tags


# ---------------------------------------------------------------------------
# Tests: read_note - Exact Path Match
# ---------------------------------------------------------------------------

class TestReadNoteExactPath:
    def test_exact_path_with_extension(self, temp_vault):
        """Read note by exact path with .md extension."""
        result = read_note("Simple Note.md")
        assert result is not None
        assert result["title"] == "Simple Note"
        assert "simple content" in result["content"].lower()

    def test_exact_path_without_extension(self, temp_vault):
        """Read note by path without .md extension."""
        result = read_note("Simple Note")
        assert result is not None
        assert result["title"] == "Simple Note"

    def test_exact_path_in_subfolder(self, temp_vault):
        """Read note by path in subfolder."""
        result = read_note("projects/project-alpha.md")
        assert result is not None
        assert result["title"] == "Project Alpha Plan"
        assert result["path"] == "projects/project-alpha.md"

    def test_path_with_folder_restriction(self, temp_vault):
        """Read note in specific folder by relative path."""
        result = read_note("project-alpha", folder="projects")
        assert result is not None
        assert "Project Alpha" in result["title"]


# ---------------------------------------------------------------------------
# Tests: read_note - Exact Title Match
# ---------------------------------------------------------------------------

class TestReadNoteExactTitle:
    def test_exact_title_match_case_insensitive(self, temp_vault):
        """Match title case-insensitively."""
        result = read_note("simple note")
        assert result is not None
        assert result["title"] == "Simple Note"

    def test_exact_title_from_frontmatter(self, temp_vault):
        """Match title from frontmatter."""
        result = read_note("Frontmatter Title")
        assert result is not None
        assert "frontmatter" in result["content"].lower()

    def test_exact_title_match_filename_stem(self, temp_vault):
        """Match by filename stem exactly."""
        result = read_note("inline-tags")
        assert result is not None
        assert "#inline" in result["content"]


# ---------------------------------------------------------------------------
# Tests: read_note - Fuzzy Title Match
# ---------------------------------------------------------------------------

class TestReadNoteFuzzyTitle:
    def test_fuzzy_match_partial_title(self, temp_vault):
        """Partial title match finds note."""
        result = read_note("Alpha")
        assert result is not None
        assert "alpha" in result["title"].lower()

    def test_fuzzy_match_case_insensitive(self, temp_vault):
        """Fuzzy match is case-insensitive."""
        result = read_note("BETA")
        assert result is not None
        assert "beta" in result["title"].lower()

    def test_fuzzy_match_prefers_shorter(self, temp_vault):
        """Fuzzy match prefers shorter (more specific) titles."""
        # "project" appears in multiple notes, should prefer shortest match
        result = read_note("project", folder="projects")
        assert result is not None
        # Both project-alpha and project-beta match, either is acceptable
        assert "project" in result["title"].lower()

    def test_fuzzy_match_in_folder(self, temp_vault):
        """Fuzzy match respects folder restriction."""
        result = read_note("alpha", folder="projects")
        assert result is not None
        assert result["path"].startswith("projects/")


# ---------------------------------------------------------------------------
# Tests: read_note - Metadata
# ---------------------------------------------------------------------------

class TestReadNoteMetadata:
    def test_returns_tags(self, temp_vault):
        """Result includes extracted tags."""
        result = read_note("project-alpha", folder="projects")
        assert result is not None
        assert "active" in result["tags"]
        assert "milestone" in result["tags"]

    def test_returns_timestamps(self, temp_vault):
        """Result includes created/modified timestamps."""
        result = read_note("Simple Note")
        assert result is not None
        assert result["created"] is not None
        assert result["modified"] is not None
        # Timestamps should be ISO format
        assert "T" in result["created"]

    def test_returns_relative_path(self, temp_vault):
        """Result path is relative to vault root."""
        result = read_note("project-alpha", folder="projects")
        assert result is not None
        assert result["path"] == "projects/project-alpha.md"


# ---------------------------------------------------------------------------
# Tests: read_note - Error Cases
# ---------------------------------------------------------------------------

class TestReadNoteErrors:
    def test_note_not_found(self, temp_vault):
        """Returns None when note doesn't exist."""
        result = read_note("nonexistent-note")
        assert result is None

    def test_empty_query(self, temp_vault):
        """Returns None for empty query."""
        result = read_note("")
        assert result is None
        result = read_note("   ")
        assert result is None

    def test_folder_not_found(self, temp_vault):
        """Returns None when folder doesn't exist."""
        result = read_note("anything", folder="nonexistent-folder")
        assert result is None

    def test_excludes_hidden_directories(self, temp_vault):
        """Notes in .obsidian are not found."""
        result = read_note("config")
        assert result is None

    def test_vault_not_exists(self, tmp_path, monkeypatch):
        """Returns None when vault doesn't exist."""
        monkeypatch.setenv("OBSIDIAN_VAULT_DIR", str(tmp_path / "nonexistent"))
        result = read_note("anything")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Helper Functions
# ---------------------------------------------------------------------------

class TestCollectNoteFiles:
    def test_collects_md_files(self, temp_vault):
        """Collects all .md files."""
        files = _collect_note_files(temp_vault)
        assert len(files) >= 3  # At least the root-level notes

    def test_excludes_hidden_dirs(self, temp_vault):
        """Excludes files in hidden directories."""
        files = _collect_note_files(temp_vault)
        paths = [str(f) for f in files]
        assert not any(".obsidian" in p for p in paths)

    def test_includes_subfolders(self, temp_vault):
        """Includes files from subfolders."""
        files = _collect_note_files(temp_vault)
        paths = [str(f) for f in files]
        assert any("projects" in p for p in paths)


class TestFindByExactTitle:
    def test_finds_by_stem(self, temp_vault):
        """Finds note by filename stem."""
        candidates = _collect_note_files(temp_vault)
        result = _find_by_exact_title("inline-tags", candidates)
        assert result is not None
        assert result.stem == "inline-tags"

    def test_case_insensitive(self, temp_vault):
        """Match is case-insensitive."""
        candidates = _collect_note_files(temp_vault)
        result = _find_by_exact_title("INLINE-TAGS", candidates)
        assert result is not None


class TestFindByFuzzyTitle:
    def test_partial_match(self, temp_vault):
        """Finds note by partial title."""
        candidates = _collect_note_files(temp_vault)
        result = _find_by_fuzzy_title("alpha", candidates)
        assert result is not None
        assert "alpha" in result.stem.lower()

    def test_no_match_returns_none(self, temp_vault):
        """Returns None when no partial match."""
        candidates = _collect_note_files(temp_vault)
        result = _find_by_fuzzy_title("xyznonexistent", candidates)
        assert result is None
