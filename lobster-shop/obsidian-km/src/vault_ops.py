"""
Obsidian Vault Operations — Pure Functions for Vault Access

Design principles:
- Pure functions: no side effects, deterministic outputs
- Immutability: return new data structures, never mutate
- Composition: small functions that compose into larger operations
- Lazy evaluation: use generators for efficient large vault handling
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal


@dataclass(frozen=True)
class NoteMetadata:
    """Immutable note metadata."""
    title: str
    path: str  # Relative to vault root
    tags: tuple[str, ...]  # Immutable tuple of tags
    created: datetime
    modified: datetime
    size: int

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "title": self.title,
            "path": self.path,
            "tags": list(self.tags),
            "created": self.created.isoformat(),
            "modified": self.modified.isoformat(),
            "size": self.size,
        }


@dataclass(frozen=True)
class ListNotesResult:
    """Immutable result from list_notes."""
    notes: tuple[NoteMetadata, ...]
    total: int

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "notes": [n.to_dict() for n in self.notes],
            "total": self.total,
        }


# =============================================================================
# Pure helper functions
# =============================================================================

def is_markdown_file(path: Path) -> bool:
    """Check if path is a markdown file."""
    return path.suffix.lower() in (".md", ".markdown")


def is_hidden(path: Path) -> bool:
    """Check if any component of the path is hidden (starts with .)."""
    return any(part.startswith(".") for part in path.parts)


def extract_title_from_path(path: Path) -> str:
    """Extract note title from file path (stem without extension)."""
    return path.stem


def extract_frontmatter(content: str) -> dict | None:
    """
    Extract YAML frontmatter from markdown content.

    Returns parsed frontmatter dict or None if not present/parseable.
    Handles the common YAML frontmatter format:
    ---
    key: value
    tags: [tag1, tag2]
    ---
    """
    if not content.startswith("---"):
        return None

    # Find closing delimiter
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return None

    yaml_content = content[3:end_match.start() + 3]

    # Simple YAML parsing for common cases (avoid full YAML dependency in hot path)
    # This handles: tags: [tag1, tag2] and tags:\n  - tag1\n  - tag2
    result = {}

    # Parse tags specifically (most common use case)
    # Inline format: tags: [tag1, tag2] or tags: ["tag1", "tag2"]
    inline_tags = re.search(r"^tags:\s*\[([^\]]*)\]", yaml_content, re.MULTILINE)
    if inline_tags:
        tag_str = inline_tags.group(1)
        # Handle both quoted and unquoted tags
        tags = [
            t.strip().strip('"').strip("'").lstrip("#")
            for t in tag_str.split(",")
            if t.strip()
        ]
        result["tags"] = tags
    else:
        # List format: tags:\n  - tag1\n  - tag2
        list_tags = re.search(r"^tags:\s*\n((?:\s+-\s+.+\n?)+)", yaml_content, re.MULTILINE)
        if list_tags:
            tag_lines = list_tags.group(1)
            tags = [
                line.strip().lstrip("-").strip().strip('"').strip("'").lstrip("#")
                for line in tag_lines.split("\n")
                if line.strip().startswith("-")
            ]
            result["tags"] = tags

    return result if result else None


def extract_tags_from_content(content: str) -> tuple[str, ...]:
    """
    Extract tags from note content.

    Checks YAML frontmatter tags field.
    Returns immutable tuple of tag strings (without # prefix).
    """
    frontmatter = extract_frontmatter(content)
    if frontmatter and "tags" in frontmatter:
        return tuple(frontmatter["tags"])
    return ()


def read_frontmatter_only(path: Path, max_bytes: int = 4096) -> str:
    """
    Read only the beginning of a file to extract frontmatter.

    Performance optimization: don't read entire file just for tags.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except (OSError, IOError):
        return ""


def file_stats_to_datetime(stat_result: os.stat_result) -> tuple[datetime, datetime]:
    """Extract created and modified times from stat result."""
    # Use mtime for modified
    modified = datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)

    # For created, use birthtime if available (macOS), otherwise ctime
    # On Linux, ctime is "change time" not "creation time", but it's the best we have
    created_ts = getattr(stat_result, "st_birthtime", stat_result.st_ctime)
    created = datetime.fromtimestamp(created_ts, tz=timezone.utc)

    return created, modified


# =============================================================================
# Scanning functions (generators for lazy evaluation)
# =============================================================================

def scan_markdown_files(vault_path: Path) -> Iterator[Path]:
    """
    Lazily scan vault for markdown files.

    Excludes hidden files/folders and .obsidian directory.
    Uses os.scandir for performance on large vaults.
    """
    def scan_dir(dir_path: Path) -> Iterator[Path]:
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    # Skip hidden entries
                    if entry.name.startswith("."):
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        # Recurse into subdirectories
                        yield from scan_dir(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        path = Path(entry.path)
                        if is_markdown_file(path):
                            yield path
        except PermissionError:
            pass  # Skip directories we can't access

    yield from scan_dir(vault_path)


def build_note_metadata(
    file_path: Path,
    vault_path: Path,
    include_tags: bool = False,
) -> NoteMetadata | None:
    """
    Build NoteMetadata for a single file.

    Returns None if file is inaccessible.
    """
    try:
        stat = file_path.stat()
        created, modified = file_stats_to_datetime(stat)

        # Extract tags only if needed (performance optimization)
        tags: tuple[str, ...] = ()
        if include_tags:
            content = read_frontmatter_only(file_path)
            tags = extract_tags_from_content(content)

        rel_path = file_path.relative_to(vault_path)

        return NoteMetadata(
            title=extract_title_from_path(file_path),
            path=str(rel_path),
            tags=tags,
            created=created,
            modified=modified,
            size=stat.st_size,
        )
    except (OSError, IOError, ValueError):
        return None


# =============================================================================
# Filter functions (pure predicates)
# =============================================================================

def folder_filter(folder: str, vault_path: Path) -> Callable[[Path], bool]:
    """Create a predicate that filters paths by folder prefix."""
    folder_path = vault_path / folder

    def predicate(path: Path) -> bool:
        try:
            path.relative_to(folder_path)
            return True
        except ValueError:
            return False

    return predicate


def tag_filter(tag: str) -> Callable[[NoteMetadata], bool]:
    """Create a predicate that filters notes by tag."""
    normalized_tag = tag.lstrip("#").lower()

    def predicate(note: NoteMetadata) -> bool:
        return any(t.lower() == normalized_tag for t in note.tags)

    return predicate


# =============================================================================
# Sort functions (pure key extractors)
# =============================================================================

SortOrder = Literal["modified", "created", "title"]


def get_sort_key(sort: SortOrder) -> Callable[[NoteMetadata], tuple]:
    """
    Get a sort key function for the given sort order.

    Returns tuple keys for stable sorting with secondary criteria.
    """
    if sort == "modified":
        # Most recent first, then by title
        return lambda n: (-n.modified.timestamp(), n.title.lower())
    elif sort == "created":
        # Most recent first, then by title
        return lambda n: (-n.created.timestamp(), n.title.lower())
    else:  # title
        # Alphabetical, case-insensitive
        return lambda n: (n.title.lower(), -n.modified.timestamp())


# =============================================================================
# Main list_notes function
# =============================================================================

def list_notes(
    vault_path: str | Path,
    folder: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    sort: SortOrder = "modified",
) -> ListNotesResult:
    """
    List notes in an Obsidian vault with filtering and sorting.

    Args:
        vault_path: Path to the Obsidian vault root
        folder: Optional folder filter (relative to vault root)
        tag: Optional tag filter (checks YAML frontmatter tags field)
        limit: Maximum number of notes to return
        sort: Sort order — "modified", "created", or "title"

    Returns:
        ListNotesResult with notes array and total count

    Performance:
        - Uses lazy generators to avoid loading all files into memory
        - Only reads frontmatter when tag filtering is needed
        - < 2 seconds for 10,000 notes typical
    """
    vault = Path(vault_path).resolve()

    if not vault.is_dir():
        return ListNotesResult(notes=(), total=0)

    # Scan all markdown files (lazy generator)
    files = scan_markdown_files(vault)

    # Apply folder filter if specified
    if folder:
        filter_fn = folder_filter(folder, vault)
        files = (f for f in files if filter_fn(f))

    # Build metadata (tags needed if filtering by tag)
    need_tags = tag is not None
    notes_iter = (
        build_note_metadata(f, vault, include_tags=need_tags)
        for f in files
    )

    # Filter out None (inaccessible files)
    notes_iter = (n for n in notes_iter if n is not None)

    # Apply tag filter if specified
    if tag:
        tag_pred = tag_filter(tag)
        notes_iter = (n for n in notes_iter if tag_pred(n))

    # Collect all matching notes for total count
    # (We need to materialize to count and sort)
    all_notes = list(notes_iter)
    total = len(all_notes)

    # Sort
    sort_key = get_sort_key(sort)
    all_notes.sort(key=sort_key)

    # Apply limit
    limited_notes = all_notes[:limit]

    # If we didn't need tags for filtering, fetch them now for the limited set
    if not need_tags:
        limited_notes = [
            NoteMetadata(
                title=n.title,
                path=n.path,
                tags=extract_tags_from_content(
                    read_frontmatter_only(vault / n.path)
                ),
                created=n.created,
                modified=n.modified,
                size=n.size,
            )
            for n in limited_notes
        ]

    return ListNotesResult(
        notes=tuple(limited_notes),
        total=total,
    )
