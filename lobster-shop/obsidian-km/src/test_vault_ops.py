"""
Tests for vault_ops module.

Uses a temporary directory structure to test vault operations.
"""

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vault_ops import (
    NoteMetadata,
    ListNotesResult,
    is_markdown_file,
    is_hidden,
    extract_title_from_path,
    extract_frontmatter,
    extract_tags_from_content,
    scan_markdown_files,
    build_note_metadata,
    folder_filter,
    tag_filter,
    get_sort_key,
    list_notes,
)


# =============================================================================
# Pure function tests
# =============================================================================

class TestIsMarkdownFile:
    def test_md_extension(self):
        assert is_markdown_file(Path("note.md")) is True

    def test_markdown_extension(self):
        assert is_markdown_file(Path("note.markdown")) is True

    def test_uppercase_md(self):
        assert is_markdown_file(Path("NOTE.MD")) is True

    def test_txt_file(self):
        assert is_markdown_file(Path("note.txt")) is False

    def test_no_extension(self):
        assert is_markdown_file(Path("README")) is False


class TestIsHidden:
    def test_hidden_file(self):
        assert is_hidden(Path(".hidden")) is True

    def test_hidden_directory(self):
        assert is_hidden(Path(".obsidian/config.json")) is True

    def test_visible_file(self):
        assert is_hidden(Path("notes/readme.md")) is False

    def test_file_in_hidden_dir(self):
        assert is_hidden(Path("notes/.hidden/file.md")) is True


class TestExtractTitleFromPath:
    def test_simple_file(self):
        assert extract_title_from_path(Path("My Note.md")) == "My Note"

    def test_nested_file(self):
        assert extract_title_from_path(Path("folder/subfolder/Note.md")) == "Note"


class TestExtractFrontmatter:
    def test_inline_tags(self):
        content = """---
tags: [project, active]
---
# Content"""
        result = extract_frontmatter(content)
        assert result == {"tags": ["project", "active"]}

    def test_inline_tags_with_hash(self):
        content = """---
tags: [#project, #active]
---
# Content"""
        result = extract_frontmatter(content)
        assert result == {"tags": ["project", "active"]}

    def test_quoted_tags(self):
        content = """---
tags: ["project", "active"]
---
# Content"""
        result = extract_frontmatter(content)
        assert result == {"tags": ["project", "active"]}

    def test_list_tags(self):
        content = """---
tags:
  - project
  - active
---
# Content"""
        result = extract_frontmatter(content)
        assert result == {"tags": ["project", "active"]}

    def test_no_frontmatter(self):
        content = "# Just a header\n\nContent here."
        assert extract_frontmatter(content) is None

    def test_no_closing_delimiter(self):
        content = """---
tags: [project]
# No closing delimiter"""
        assert extract_frontmatter(content) is None

    def test_empty_tags(self):
        content = """---
title: Note
---
# Content"""
        result = extract_frontmatter(content)
        assert result is None  # No tags key


class TestExtractTagsFromContent:
    def test_with_tags(self):
        content = """---
tags: [project, active]
---
# Content"""
        assert extract_tags_from_content(content) == ("project", "active")

    def test_without_tags(self):
        content = "# No frontmatter"
        assert extract_tags_from_content(content) == ()


# =============================================================================
# Filter tests
# =============================================================================

class TestFolderFilter:
    def test_matching_folder(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "projects").mkdir()

        filter_fn = folder_filter("projects", vault)

        assert filter_fn(vault / "projects" / "note.md") is True
        assert filter_fn(vault / "projects" / "sub" / "note.md") is True
        assert filter_fn(vault / "other" / "note.md") is False


class TestTagFilter:
    def test_matching_tag(self):
        note = NoteMetadata(
            title="Test",
            path="test.md",
            tags=("project", "active"),
            created=datetime.now(timezone.utc),
            modified=datetime.now(timezone.utc),
            size=100,
        )

        filter_fn = tag_filter("project")
        assert filter_fn(note) is True

    def test_non_matching_tag(self):
        note = NoteMetadata(
            title="Test",
            path="test.md",
            tags=("project", "active"),
            created=datetime.now(timezone.utc),
            modified=datetime.now(timezone.utc),
            size=100,
        )

        filter_fn = tag_filter("archive")
        assert filter_fn(note) is False

    def test_case_insensitive(self):
        note = NoteMetadata(
            title="Test",
            path="test.md",
            tags=("Project",),
            created=datetime.now(timezone.utc),
            modified=datetime.now(timezone.utc),
            size=100,
        )

        filter_fn = tag_filter("PROJECT")
        assert filter_fn(note) is True

    def test_hash_prefix_stripped(self):
        note = NoteMetadata(
            title="Test",
            path="test.md",
            tags=("project",),
            created=datetime.now(timezone.utc),
            modified=datetime.now(timezone.utc),
            size=100,
        )

        filter_fn = tag_filter("#project")
        assert filter_fn(note) is True


# =============================================================================
# Sort key tests
# =============================================================================

class TestGetSortKey:
    def test_modified_sort(self):
        now = datetime.now(timezone.utc)
        earlier = datetime(2024, 1, 1, tzinfo=timezone.utc)

        note1 = NoteMetadata("A", "a.md", (), earlier, now, 100)
        note2 = NoteMetadata("B", "b.md", (), earlier, earlier, 100)

        key_fn = get_sort_key("modified")

        # note1 has more recent modified time, should come first (lower key)
        assert key_fn(note1) < key_fn(note2)

    def test_title_sort(self):
        now = datetime.now(timezone.utc)

        note_a = NoteMetadata("Apple", "a.md", (), now, now, 100)
        note_b = NoteMetadata("Banana", "b.md", (), now, now, 100)

        key_fn = get_sort_key("title")

        assert key_fn(note_a) < key_fn(note_b)


# =============================================================================
# Integration tests with temp vault
# =============================================================================

@pytest.fixture
def temp_vault(tmp_path):
    """Create a temporary vault with test notes."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # Create folder structure
    (vault / "projects").mkdir()
    (vault / "archive").mkdir()
    (vault / ".obsidian").mkdir()  # Should be ignored

    # Create notes
    notes = [
        ("projects/active.md", """---
tags: [project, active]
---
# Active Project
"""),
        ("projects/completed.md", """---
tags: [project, completed]
---
# Completed Project
"""),
        ("archive/old.md", """---
tags: [archive]
---
# Old Note
"""),
        ("daily.md", "# Daily Note\nNo frontmatter."),
        (".obsidian/config.json", "{}"),  # Should be ignored
    ]

    for path, content in notes:
        file_path = vault / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        # Add small delay to ensure different mtimes
        time.sleep(0.01)

    return vault


class TestScanMarkdownFiles:
    def test_finds_all_markdown_files(self, temp_vault):
        files = list(scan_markdown_files(temp_vault))
        paths = [str(f.relative_to(temp_vault)) for f in files]

        assert len(files) == 4
        assert "projects/active.md" in paths
        assert "projects/completed.md" in paths
        assert "archive/old.md" in paths
        assert "daily.md" in paths

    def test_excludes_hidden_folders(self, temp_vault):
        files = list(scan_markdown_files(temp_vault))
        paths = [str(f.relative_to(temp_vault)) for f in files]

        assert ".obsidian/config.json" not in paths


class TestBuildNoteMetadata:
    def test_builds_metadata_without_tags(self, temp_vault):
        file_path = temp_vault / "daily.md"
        note = build_note_metadata(file_path, temp_vault, include_tags=False)

        assert note is not None
        assert note.title == "daily"
        assert note.path == "daily.md"
        assert note.tags == ()
        assert note.size > 0

    def test_builds_metadata_with_tags(self, temp_vault):
        file_path = temp_vault / "projects" / "active.md"
        note = build_note_metadata(file_path, temp_vault, include_tags=True)

        assert note is not None
        assert note.title == "active"
        assert note.path == "projects/active.md"
        assert "project" in note.tags
        assert "active" in note.tags


class TestListNotes:
    def test_list_all_notes(self, temp_vault):
        result = list_notes(temp_vault)

        assert result.total == 4
        assert len(result.notes) == 4

    def test_list_with_limit(self, temp_vault):
        result = list_notes(temp_vault, limit=2)

        assert result.total == 4
        assert len(result.notes) == 2

    def test_filter_by_folder(self, temp_vault):
        result = list_notes(temp_vault, folder="projects")

        assert result.total == 2
        paths = [n.path for n in result.notes]
        assert "projects/active.md" in paths
        assert "projects/completed.md" in paths

    def test_filter_by_tag(self, temp_vault):
        result = list_notes(temp_vault, tag="project")

        assert result.total == 2
        titles = [n.title for n in result.notes]
        assert "active" in titles
        assert "completed" in titles

    def test_sort_by_title(self, temp_vault):
        result = list_notes(temp_vault, sort="title")

        titles = [n.title for n in result.notes]
        assert titles == sorted(titles, key=str.lower)

    def test_nonexistent_vault(self):
        result = list_notes("/nonexistent/path")

        assert result.total == 0
        assert len(result.notes) == 0

    def test_combined_filters(self, temp_vault):
        result = list_notes(temp_vault, folder="projects", tag="active")

        assert result.total == 1
        assert result.notes[0].title == "active"


class TestNoteMetadataToDict:
    def test_serialization(self):
        now = datetime(2024, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        note = NoteMetadata(
            title="Test",
            path="test.md",
            tags=("project", "active"),
            created=now,
            modified=now,
            size=1024,
        )

        d = note.to_dict()

        assert d["title"] == "Test"
        assert d["path"] == "test.md"
        assert d["tags"] == ["project", "active"]
        assert d["created"] == "2024-03-20T12:00:00+00:00"
        assert d["modified"] == "2024-03-20T12:00:00+00:00"
        assert d["size"] == 1024


class TestListNotesResultToDict:
    def test_serialization(self):
        now = datetime(2024, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        note = NoteMetadata("Test", "test.md", (), now, now, 100)
        result = ListNotesResult(notes=(note,), total=10)

        d = result.to_dict()

        assert len(d["notes"]) == 1
        assert d["total"] == 10


# =============================================================================
# Performance test (optional, skipped by default)
# =============================================================================

@pytest.mark.skip(reason="Performance test - run manually")
class TestPerformance:
    def test_10000_notes_under_2_seconds(self, tmp_path):
        """Create 10,000 notes and verify list_notes completes in < 2 seconds."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create 10,000 notes across 100 folders
        for folder_idx in range(100):
            folder = vault / f"folder_{folder_idx:03d}"
            folder.mkdir()
            for note_idx in range(100):
                note = folder / f"note_{note_idx:03d}.md"
                note.write_text(f"""---
tags: [test, folder{folder_idx}]
---
# Note {folder_idx}-{note_idx}
""")

        start = time.time()
        result = list_notes(vault, limit=20)
        elapsed = time.time() - start

        assert result.total == 10000
        assert len(result.notes) == 20
        assert elapsed < 2.0, f"Took {elapsed:.2f}s, expected < 2s"
