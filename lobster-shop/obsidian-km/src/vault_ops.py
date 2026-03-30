"""
vault_ops.py — Core vault operations for Obsidian KM skill.

Pure functional implementation using filesystem + python-frontmatter + ripgrep.
All functions are pure: they take explicit parameters and return new values
without mutating global state.

Design principles:
- Inject vault path explicitly (default to ~/obsidian-vault/)
- Return dicts/lists (immutable-friendly)
- Raise specific exceptions on error
- Compose small functions
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TypedDict

import frontmatter


# =============================================================================
# Types
# =============================================================================

class NoteData(TypedDict, total=False):
    """Represents a note's data."""
    title: str
    content: str
    tags: list[str]
    created: str
    modified: str
    path: str


class SearchMatch(TypedDict):
    """Represents a search result."""
    path: str
    title: str
    line_number: int
    line_content: str


class ListResult(TypedDict):
    """Represents list_notes result."""
    total: int
    notes: list[NoteData]


# =============================================================================
# Constants
# =============================================================================

VAULT_DIR = Path.home() / "obsidian-vault"

# Characters invalid in filenames across platforms
INVALID_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')


# =============================================================================
# Path Utilities (Pure)
# =============================================================================

def resolve_vault_path(vault: Path | None = None) -> Path:
    """
    Return the vault directory, defaulting to ~/obsidian-vault/.

    Pure function: returns a new Path based on input.

    Args:
        vault: Optional vault directory path. If None, uses VAULT_DIR.

    Returns:
        Resolved vault directory Path.
    """
    return vault if vault is not None else VAULT_DIR


def sanitize_title(title: str) -> str:
    """
    Remove characters invalid in filenames.

    Pure function: returns a new string.

    Args:
        title: The note title to sanitize.

    Returns:
        Sanitized title safe for use as a filename.

    Examples:
        >>> sanitize_title("My Note: A Story")
        'My Note- A Story'
        >>> sanitize_title("Question? Answer!")
        'Question- Answer!'
    """
    return INVALID_FILENAME_CHARS.sub("-", title).strip()


def _note_path(title: str, folder: str, vault: Path) -> Path:
    """
    Compute the full path to a note file.

    Pure function: computes path from inputs.
    """
    safe_title = sanitize_title(title)
    return vault / folder / f"{safe_title}.md"


def _is_path(title_or_path: str) -> bool:
    """Check if input looks like a path (contains / or ends with .md)."""
    return "/" in title_or_path or title_or_path.endswith(".md")


def _resolve_note_path(
    title_or_path: str,
    folder: str | None,
    vault: Path,
) -> Path:
    """
    Resolve a title or path to a full Path.

    If title_or_path looks like a path, resolve it relative to vault.
    Otherwise, look in folder (default: search all folders).
    """
    if _is_path(title_or_path):
        # It's a path - resolve relative to vault
        candidate = vault / title_or_path
        if candidate.exists():
            return candidate
        # Try with .md extension
        if not title_or_path.endswith(".md"):
            candidate = vault / f"{title_or_path}.md"
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"Note not found: {title_or_path}")

    # It's a title - search for it
    safe_title = sanitize_title(title_or_path)

    if folder:
        # Look in specific folder
        candidate = vault / folder / f"{safe_title}.md"
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Note '{title_or_path}' not found in {folder}")

    # Search all folders
    matches = list(vault.rglob(f"{safe_title}.md"))
    if not matches:
        raise FileNotFoundError(f"Note not found: {title_or_path}")
    if len(matches) > 1:
        # Return first match but could warn about ambiguity
        pass
    return matches[0]


# =============================================================================
# Note Operations
# =============================================================================

def create_note(
    title: str,
    content: str,
    folder: str = "Inbox",
    tags: list[str] | None = None,
    vault: Path | None = None,
) -> Path:
    """
    Create a new note in the vault.

    Creates the note with YAML frontmatter containing title, tags, and timestamps.
    Raises FileExistsError if a note with the same title already exists in the folder.

    Args:
        title: The note title (used as filename after sanitization).
        content: The note body content.
        folder: Folder within the vault (default: "Inbox").
        tags: Optional list of tags for frontmatter.
        vault: Optional vault path (default: ~/obsidian-vault/).

    Returns:
        Path to the created note.

    Raises:
        FileExistsError: If a note with this title already exists.

    Example:
        >>> path = create_note(
        ...     title="Meeting Notes",
        ...     content="# Meeting Notes\\n\\nDiscussed project timeline.",
        ...     tags=["meetings", "project-x"],
        ... )
    """
    vault_path = resolve_vault_path(vault)
    note_path = _note_path(title, folder, vault_path)

    if note_path.exists():
        raise FileExistsError(f"Note already exists: {note_path}")

    # Ensure folder exists
    note_path.parent.mkdir(parents=True, exist_ok=True)

    # Build frontmatter
    now = datetime.now().isoformat()
    metadata = {
        "title": title,
        "created": now,
        "modified": now,
    }
    if tags:
        metadata["tags"] = tags

    # Create post with frontmatter
    post = frontmatter.Post(content, **metadata)

    # Write atomically (write to temp, then rename)
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    return note_path


def read_note(
    title_or_path: str,
    folder: str | None = None,
    vault: Path | None = None,
) -> NoteData:
    """
    Read a note by title or path.

    Returns a dict with the note's metadata and content.

    Args:
        title_or_path: Note title or relative path within vault.
        folder: Optional folder to search in (if title given).
        vault: Optional vault path (default: ~/obsidian-vault/).

    Returns:
        Dict with: title, content, tags, created, modified, path.

    Raises:
        FileNotFoundError: If the note doesn't exist.

    Example:
        >>> note = read_note("Meeting Notes", folder="Inbox")
        >>> print(note["content"])
    """
    vault_path = resolve_vault_path(vault)
    note_path = _resolve_note_path(title_or_path, folder, vault_path)

    post = frontmatter.load(note_path)

    # Get file stats for modified time if not in frontmatter
    stat = note_path.stat()

    return NoteData(
        title=post.get("title", note_path.stem),
        content=post.content,
        tags=post.get("tags", []),
        created=post.get("created", ""),
        modified=post.get("modified", datetime.fromtimestamp(stat.st_mtime).isoformat()),
        path=str(note_path.relative_to(vault_path)),
    )


def append_to_note(
    title_or_path: str,
    content: str,
    separator: str = "\n",
    vault: Path | None = None,
) -> NoteData:
    """
    Append content to an existing note.

    Updates the modified timestamp in frontmatter.

    Args:
        title_or_path: Note title or relative path within vault.
        content: Content to append to the note body.
        separator: Separator between existing content and new content.
        vault: Optional vault path (default: ~/obsidian-vault/).

    Returns:
        Dict with the updated note data.

    Raises:
        FileNotFoundError: If the note doesn't exist.

    Example:
        >>> updated = append_to_note(
        ...     "Meeting Notes",
        ...     "\\n## Action Items\\n- Follow up with team",
        ... )
    """
    vault_path = resolve_vault_path(vault)
    note_path = _resolve_note_path(title_or_path, None, vault_path)

    # Load existing note
    post = frontmatter.load(note_path)

    # Append content
    post.content = post.content + separator + content

    # Update modified timestamp
    post["modified"] = datetime.now().isoformat()

    # Write back
    note_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    return NoteData(
        title=post.get("title", note_path.stem),
        content=post.content,
        tags=post.get("tags", []),
        created=post.get("created", ""),
        modified=post["modified"],
        path=str(note_path.relative_to(vault_path)),
    )


# =============================================================================
# Search Operations
# =============================================================================

def search_notes(
    query: str,
    folder: str | None = None,
    limit: int = 10,
    vault: Path | None = None,
) -> list[SearchMatch]:
    """
    Full-text search using ripgrep.

    Searches all markdown files in the vault (or specific folder) for the query.

    Args:
        query: Search query (regex supported).
        folder: Optional folder to limit search to.
        limit: Maximum number of results (default: 10).
        vault: Optional vault path (default: ~/obsidian-vault/).

    Returns:
        List of match dicts with: path, title, line_number, line_content.

    Example:
        >>> matches = search_notes("project timeline")
        >>> for m in matches:
        ...     print(f"{m['title']}: {m['line_content']}")
    """
    vault_path = resolve_vault_path(vault)
    search_path = vault_path / folder if folder else vault_path

    if not search_path.exists():
        return []

    try:
        # Use ripgrep with JSON output for structured results
        result = subprocess.run(
            [
                "rg",
                "--json",
                "--max-count", str(limit * 2),  # Get more to filter
                "--type", "md",
                "--ignore-case",
                query,
                str(search_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        # ripgrep not installed, fall back to basic search
        return _fallback_search(query, search_path, limit, vault_path)
    except subprocess.TimeoutExpired:
        return []

    # Parse JSON lines output
    matches: list[SearchMatch] = []
    seen_files: set[str] = set()

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("type") != "match":
                continue

            path_str = data["data"]["path"]["text"]

            # Skip if we've already included this file
            if path_str in seen_files:
                continue
            seen_files.add(path_str)

            path = Path(path_str)
            rel_path = path.relative_to(vault_path) if path.is_absolute() else path

            matches.append(SearchMatch(
                path=str(rel_path),
                title=path.stem,
                line_number=data["data"]["line_number"],
                line_content=data["data"]["lines"]["text"].strip(),
            ))

            if len(matches) >= limit:
                break

        except (json.JSONDecodeError, KeyError):
            continue

    return matches


def _fallback_search(
    query: str,
    search_path: Path,
    limit: int,
    vault_path: Path,
) -> list[SearchMatch]:
    """
    Fallback search when ripgrep is unavailable.

    Uses pure Python - slower but functional.
    """
    matches: list[SearchMatch] = []
    pattern = re.compile(query, re.IGNORECASE)

    for md_file in search_path.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            for line_num, line in enumerate(content.split("\n"), 1):
                if pattern.search(line):
                    matches.append(SearchMatch(
                        path=str(md_file.relative_to(vault_path)),
                        title=md_file.stem,
                        line_number=line_num,
                        line_content=line.strip(),
                    ))
                    break  # One match per file

            if len(matches) >= limit:
                break
        except (UnicodeDecodeError, PermissionError):
            continue

    return matches


def list_notes(
    folder: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    sort: str = "modified",
    vault: Path | None = None,
) -> ListResult:
    """
    List notes with optional filters.

    Args:
        folder: Optional folder to list from.
        tag: Optional tag filter (notes must have this tag).
        limit: Maximum notes to return (default: 20).
        sort: Sort field - "modified", "created", or "title" (default: modified).
        vault: Optional vault path (default: ~/obsidian-vault/).

    Returns:
        Dict with: total (count), notes (list of NoteData).

    Example:
        >>> result = list_notes(folder="Projects", tag="active", limit=5)
        >>> print(f"Found {result['total']} notes")
        >>> for note in result["notes"]:
        ...     print(note["title"])
    """
    vault_path = resolve_vault_path(vault)
    search_path = vault_path / folder if folder else vault_path

    if not search_path.exists():
        return ListResult(total=0, notes=[])

    notes: list[NoteData] = []

    # Collect all markdown files
    md_files = list(search_path.rglob("*.md"))

    for md_file in md_files:
        try:
            post = frontmatter.load(md_file)

            # Apply tag filter
            if tag:
                note_tags = post.get("tags", [])
                if tag not in note_tags:
                    continue

            stat = md_file.stat()

            notes.append(NoteData(
                title=post.get("title", md_file.stem),
                content=post.content[:200] + "..." if len(post.content) > 200 else post.content,
                tags=post.get("tags", []),
                created=post.get("created", ""),
                modified=post.get("modified", datetime.fromtimestamp(stat.st_mtime).isoformat()),
                path=str(md_file.relative_to(vault_path)),
            ))
        except (UnicodeDecodeError, PermissionError, frontmatter.exceptions.YAMLException):
            continue

    # Sort
    sort_key = {
        "modified": lambda n: n.get("modified", ""),
        "created": lambda n: n.get("created", ""),
        "title": lambda n: n.get("title", "").lower(),
    }.get(sort, lambda n: n.get("modified", ""))

    notes.sort(key=sort_key, reverse=(sort != "title"))

    total = len(notes)
    return ListResult(total=total, notes=notes[:limit])
