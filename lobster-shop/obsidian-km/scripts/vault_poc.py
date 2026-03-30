#!/usr/bin/env python3
"""
vault_poc.py — Proof of concept for vault_ops module.

Tests: create_note, read_note, search_notes, append_to_note, list_notes.

Creates a temporary test vault, runs all operations, and reports results.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from vault_ops import (
    append_to_note,
    create_note,
    list_notes,
    read_note,
    sanitize_title,
    search_notes,
)


# =============================================================================
# Test Utilities
# =============================================================================

class TestResult:
    """Collects test results."""

    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  ✓ {name}")

    def fail(self, name: str, error: str) -> None:
        self.failed.append((name, error))
        print(f"  ✗ {name}: {error}")

    def summary(self) -> bool:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'='*60}")
        print(f"Results: {len(self.passed)}/{total} passed")
        if self.failed:
            print("\nFailed tests:")
            for name, error in self.failed:
                print(f"  - {name}: {error}")
        return len(self.failed) == 0


def assert_eq(actual, expected, msg: str = ""):
    """Assert equality with descriptive error."""
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_in(needle, haystack, msg: str = ""):
    """Assert substring/element presence."""
    if needle not in haystack:
        raise AssertionError(f"{msg}: {needle!r} not in {haystack!r}")


# =============================================================================
# Tests
# =============================================================================

def test_sanitize_title(results: TestResult) -> None:
    """Test title sanitization."""
    try:
        assert_eq(sanitize_title("Simple Title"), "Simple Title", "simple")
        assert_eq(sanitize_title("Has: Colon"), "Has- Colon", "colon")
        assert_eq(sanitize_title("Question?"), "Question-", "question mark")
        assert_eq(sanitize_title("Path/With/Slashes"), "Path-With-Slashes", "slashes")
        assert_eq(sanitize_title('Quote "Test"'), "Quote -Test-", "quotes")
        results.ok("sanitize_title")
    except AssertionError as e:
        results.fail("sanitize_title", str(e))


def test_create_note(vault: Path, results: TestResult) -> Path | None:
    """Test note creation."""
    try:
        path = create_note(
            title="Test Note",
            content="# Test Note\n\nThis is a test note.",
            folder="Inbox",
            tags=["test", "poc"],
            vault=vault,
        )

        assert path.exists(), "Note file should exist"
        assert path.name == "Test Note.md", "Filename should match title"
        assert "Inbox" in str(path), "Should be in Inbox folder"

        results.ok("create_note")
        return path
    except Exception as e:
        results.fail("create_note", str(e))
        return None


def test_create_note_duplicate(vault: Path, results: TestResult) -> None:
    """Test that duplicate creation raises error."""
    try:
        # Create first note
        create_note(
            title="Duplicate Test",
            content="First version",
            folder="Inbox",
            vault=vault,
        )

        # Try to create again - should fail
        try:
            create_note(
                title="Duplicate Test",
                content="Second version",
                folder="Inbox",
                vault=vault,
            )
            results.fail("create_note_duplicate", "Should have raised FileExistsError")
        except FileExistsError:
            results.ok("create_note_duplicate")
    except Exception as e:
        results.fail("create_note_duplicate", str(e))


def test_read_note(vault: Path, results: TestResult) -> None:
    """Test note reading."""
    try:
        # First create a note
        create_note(
            title="Read Test",
            content="Content to read",
            folder="Inbox",
            tags=["readable"],
            vault=vault,
        )

        # Read by title
        note = read_note("Read Test", folder="Inbox", vault=vault)

        assert_eq(note["title"], "Read Test", "title")
        assert_eq(note["content"], "Content to read", "content")
        assert_in("readable", note["tags"], "tags")
        assert note["created"], "should have created timestamp"
        assert note["modified"], "should have modified timestamp"
        assert_eq(note["path"], "Inbox/Read Test.md", "path")

        results.ok("read_note")
    except Exception as e:
        results.fail("read_note", str(e))


def test_read_note_by_path(vault: Path, results: TestResult) -> None:
    """Test reading note by path."""
    try:
        create_note(
            title="Path Read Test",
            content="Read by path",
            folder="Projects",
            vault=vault,
        )

        # Read by relative path
        note = read_note("Projects/Path Read Test.md", vault=vault)
        assert_eq(note["title"], "Path Read Test", "title")

        # Read by path without extension
        note2 = read_note("Projects/Path Read Test", vault=vault)
        assert_eq(note2["title"], "Path Read Test", "title without ext")

        results.ok("read_note_by_path")
    except Exception as e:
        results.fail("read_note_by_path", str(e))


def test_read_note_not_found(vault: Path, results: TestResult) -> None:
    """Test that reading nonexistent note raises error."""
    try:
        try:
            read_note("Nonexistent Note", vault=vault)
            results.fail("read_note_not_found", "Should have raised FileNotFoundError")
        except FileNotFoundError:
            results.ok("read_note_not_found")
    except Exception as e:
        results.fail("read_note_not_found", str(e))


def test_search_notes(vault: Path, results: TestResult) -> None:
    """Test full-text search."""
    try:
        # Create searchable notes
        create_note(
            title="Search Target",
            content="This note contains UNIQUE_SEARCH_TERM_XYZ123 for testing.",
            folder="Inbox",
            vault=vault,
        )
        create_note(
            title="Another Note",
            content="This is unrelated content.",
            folder="Inbox",
            vault=vault,
        )

        # Search
        matches = search_notes("UNIQUE_SEARCH_TERM_XYZ123", vault=vault)

        assert len(matches) >= 1, "Should find at least one match"
        assert any("Search Target" in m["title"] for m in matches), "Should find target note"

        results.ok("search_notes")
    except Exception as e:
        results.fail("search_notes", str(e))


def test_search_notes_folder(vault: Path, results: TestResult) -> None:
    """Test search limited to folder."""
    try:
        # Create notes in different folders
        create_note(
            title="Project Note",
            content="FOLDER_SEARCH_TEST in projects",
            folder="Projects",
            vault=vault,
        )
        create_note(
            title="Archive Note",
            content="FOLDER_SEARCH_TEST in archive",
            folder="Archive",
            vault=vault,
        )

        # Search only in Projects
        matches = search_notes("FOLDER_SEARCH_TEST", folder="Projects", vault=vault)

        # All matches should be in Projects
        assert all("Projects" in m["path"] for m in matches), "All should be in Projects"

        results.ok("search_notes_folder")
    except Exception as e:
        results.fail("search_notes_folder", str(e))


def test_append_to_note(vault: Path, results: TestResult) -> None:
    """Test appending to note."""
    try:
        # Create note
        create_note(
            title="Append Test",
            content="Initial content.",
            folder="Inbox",
            vault=vault,
        )

        # Append
        updated = append_to_note(
            "Append Test",
            "\n## New Section\n\nAppended content.",
            vault=vault,
        )

        assert_in("Initial content", updated["content"], "original preserved")
        assert_in("Appended content", updated["content"], "new content added")
        assert_in("New Section", updated["content"], "section header added")

        # Read back to verify persistence
        note = read_note("Append Test", vault=vault)
        assert_in("Appended content", note["content"], "persisted")

        results.ok("append_to_note")
    except Exception as e:
        results.fail("append_to_note", str(e))


def test_list_notes(vault: Path, results: TestResult) -> None:
    """Test listing notes."""
    try:
        # Create several notes
        for i in range(3):
            create_note(
                title=f"List Test {i}",
                content=f"Content {i}",
                folder="ListFolder",
                tags=["list-test"] if i < 2 else ["other"],
                vault=vault,
            )

        # List all in folder
        result = list_notes(folder="ListFolder", vault=vault)
        assert_eq(result["total"], 3, "total count")
        assert len(result["notes"]) == 3, "notes count"

        # List with tag filter
        tagged = list_notes(folder="ListFolder", tag="list-test", vault=vault)
        assert_eq(tagged["total"], 2, "tagged count")

        results.ok("list_notes")
    except Exception as e:
        results.fail("list_notes", str(e))


def test_list_notes_sort(vault: Path, results: TestResult) -> None:
    """Test list_notes sorting."""
    try:
        # Create notes with different titles
        for title in ["Zebra", "Apple", "Mango"]:
            create_note(
                title=f"Sort {title}",
                content=f"Content for {title}",
                folder="SortTest",
                vault=vault,
            )

        # Sort by title
        result = list_notes(folder="SortTest", sort="title", vault=vault)
        titles = [n["title"] for n in result["notes"]]

        assert_eq(titles[0], "Sort Apple", "first should be Apple")
        assert_eq(titles[1], "Sort Mango", "second should be Mango")
        assert_eq(titles[2], "Sort Zebra", "third should be Zebra")

        results.ok("list_notes_sort")
    except Exception as e:
        results.fail("list_notes_sort", str(e))


def test_list_notes_limit(vault: Path, results: TestResult) -> None:
    """Test list_notes limit."""
    try:
        # Create many notes
        for i in range(10):
            create_note(
                title=f"Limit Test {i}",
                content=f"Content {i}",
                folder="LimitTest",
                vault=vault,
            )

        # List with limit
        result = list_notes(folder="LimitTest", limit=3, vault=vault)
        assert_eq(result["total"], 10, "total should be full count")
        assert_eq(len(result["notes"]), 3, "notes should be limited")

        results.ok("list_notes_limit")
    except Exception as e:
        results.fail("list_notes_limit", str(e))


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    """Run all tests."""
    print("="*60)
    print("vault_ops.py Proof of Concept")
    print("="*60)

    # Create temporary vault
    with tempfile.TemporaryDirectory(prefix="vault_poc_") as tmpdir:
        vault = Path(tmpdir)
        print(f"\nTest vault: {vault}")
        print("-"*60)

        results = TestResult()

        # Run tests
        print("\n[Unit Tests]")
        test_sanitize_title(results)

        print("\n[Create Tests]")
        test_create_note(vault, results)
        test_create_note_duplicate(vault, results)

        print("\n[Read Tests]")
        test_read_note(vault, results)
        test_read_note_by_path(vault, results)
        test_read_note_not_found(vault, results)

        print("\n[Search Tests]")
        test_search_notes(vault, results)
        test_search_notes_folder(vault, results)

        print("\n[Append Tests]")
        test_append_to_note(vault, results)

        print("\n[List Tests]")
        test_list_notes(vault, results)
        test_list_notes_sort(vault, results)
        test_list_notes_limit(vault, results)

        # Summary
        success = results.summary()

    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        traceback.print_exc()
        sys.exit(2)
