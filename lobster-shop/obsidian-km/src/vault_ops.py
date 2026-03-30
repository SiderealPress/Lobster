"""
Vault operations for Obsidian KM skill.

Pure functions for reading, writing, and searching notes in an Obsidian vault.
"""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def get_vault_dir() -> Path:
    """Get the vault directory from environment or default."""
    vault_path = os.environ.get("OBSIDIAN_VAULT_DIR", str(Path.home() / "obsidian-vault"))
    return Path(vault_path).expanduser()


# ---------------------------------------------------------------------------
# Note Metadata Extraction
# ---------------------------------------------------------------------------

def extract_title(path: Path, content: str) -> str:
    """
    Extract title from note. Priority:
    1. YAML frontmatter 'title' field
    2. First H1 heading (# Title)
    3. Filename without extension
    """
    try:
        post = frontmatter.loads(content)
        if post.get("title"):
            return str(post["title"])
    except Exception:
        pass

    # Try to find first H1
    h1_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if h1_match:
        return h1_match.group(1).strip()

    # Fall back to filename
    return path.stem


def extract_tags(content: str) -> list[str]:
    """
    Extract tags from note. Sources:
    1. YAML frontmatter 'tags' field
    2. Inline #tags in content
    """
    tags: set[str] = set()

    try:
        post = frontmatter.loads(content)
        fm_tags = post.get("tags", [])
        if isinstance(fm_tags, list):
            tags.update(str(t) for t in fm_tags)
        elif isinstance(fm_tags, str):
            # Handle comma-separated string
            tags.update(t.strip() for t in fm_tags.split(","))
    except Exception:
        pass

    # Find inline #tags (but not in code blocks or links)
    inline_tags = re.findall(r"(?<!\w)#([a-zA-Z][a-zA-Z0-9_-]*)", content)
    tags.update(inline_tags)

    return sorted(tags)


def extract_excerpt(content: str, query: str, context_chars: int = 100) -> str:
    """
    Extract an excerpt around the first occurrence of the query.
    Returns the first context_chars chars if query not found.
    """
    # Strip frontmatter for excerpt
    try:
        post = frontmatter.loads(content)
        body = post.content
    except Exception:
        body = content

    # Normalize whitespace
    body = re.sub(r"\s+", " ", body).strip()

    if not body:
        return ""

    # Find query position (case-insensitive)
    lower_body = body.lower()
    lower_query = query.lower()
    pos = lower_body.find(lower_query)

    if pos == -1:
        # Query not found in body, return start of content
        return body[:context_chars] + ("..." if len(body) > context_chars else "")

    # Extract context around the match
    start = max(0, pos - context_chars // 2)
    end = min(len(body), pos + len(query) + context_chars // 2)

    excerpt = body[start:end]

    # Add ellipsis if truncated
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(body):
        excerpt = excerpt + "..."

    return excerpt


def get_file_timestamps(path: Path) -> tuple[datetime | None, datetime | None]:
    """
    Get created and modified timestamps for a file.

    Returns (created, modified) as datetime objects.
    Falls back to mtime for created if ctime is not reliable.
    """
    try:
        stat = path.stat()
        # On Unix, st_ctime is inode change time, not creation.
        # Use mtime as a reasonable fallback for created.
        created = datetime.fromtimestamp(stat.st_mtime)  # Fallback
        modified = datetime.fromtimestamp(stat.st_mtime)

        # Try to get birth time if available (macOS, some Linux)
        if hasattr(stat, 'st_birthtime'):
            created = datetime.fromtimestamp(stat.st_birthtime)

        return created, modified
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Note Reading Functions
# ---------------------------------------------------------------------------

def read_note(
    title_or_path: str,
    folder: str | None = None,
) -> dict[str, Any] | None:
    """
    Read a specific note by title or path.

    Args:
        title_or_path: Note title or relative path (with or without .md extension)
        folder: Optional subfolder to restrict search

    Returns:
        Dict with keys: title, content, tags, created, modified, path
        None if not found

    Search strategy:
    1. Exact path match (if title_or_path looks like a path)
    2. Exact title match (case-insensitive)
    3. Fuzzy match (case-insensitive partial title match)
    """
    vault_dir = get_vault_dir()

    if not vault_dir.exists():
        return None

    search_dir = vault_dir / folder if folder else vault_dir
    if not search_dir.exists():
        return None

    # Normalize the query
    query = title_or_path.strip()
    if not query:
        return None

    # Try exact path match first
    note_path = _find_by_exact_path(query, search_dir, vault_dir)
    if note_path:
        return _read_note_file(note_path, vault_dir)

    # Collect all .md files for title matching
    candidates = _collect_note_files(search_dir)

    # Try exact title match (case-insensitive)
    note_path = _find_by_exact_title(query, candidates)
    if note_path:
        return _read_note_file(note_path, vault_dir)

    # Fuzzy match: case-insensitive partial title match
    note_path = _find_by_fuzzy_title(query, candidates)
    if note_path:
        return _read_note_file(note_path, vault_dir)

    return None


def _find_by_exact_path(
    query: str,
    search_dir: Path,
    vault_dir: Path,
) -> Path | None:
    """
    Find note by exact path match.

    Handles paths with or without .md extension,
    relative to vault or search_dir.
    """
    # Normalize: add .md if missing
    path_query = query if query.endswith(".md") else f"{query}.md"

    # Try relative to vault
    full_path = vault_dir / path_query
    if full_path.is_file() and _is_valid_note(full_path):
        return full_path

    # Try relative to search_dir (if different from vault)
    if search_dir != vault_dir:
        full_path = search_dir / path_query
        if full_path.is_file() and _is_valid_note(full_path):
            return full_path

    return None


def _collect_note_files(search_dir: Path) -> list[Path]:
    """
    Collect all .md files in search_dir, excluding hidden directories.
    """
    candidates: list[Path] = []

    for path in search_dir.rglob("*.md"):
        # Skip hidden directories (like .obsidian)
        if any(part.startswith(".") for part in path.parts):
            continue
        candidates.append(path)

    return candidates


def _find_by_exact_title(
    query: str,
    candidates: list[Path],
) -> Path | None:
    """
    Find note by exact title match (case-insensitive).

    Checks both:
    1. Extracted title (from frontmatter/H1/filename)
    2. Filename stem (without extension)
    """
    query_lower = query.lower()

    for path in candidates:
        # Check filename stem first (fast)
        if path.stem.lower() == query_lower:
            return path

        # Check extracted title (requires reading file)
        try:
            content = path.read_text(encoding="utf-8")
            title = extract_title(path, content)
            if title.lower() == query_lower:
                return path
        except Exception:
            continue

    return None


def _find_by_fuzzy_title(
    query: str,
    candidates: list[Path],
) -> Path | None:
    """
    Find note by fuzzy title match (case-insensitive partial match).

    Returns the best match based on:
    1. Shortest title that contains the query (most specific)
    2. Filename stem matches preferred over extracted title matches
    """
    query_lower = query.lower()
    matches: list[tuple[int, bool, Path]] = []  # (len, is_stem_match, path)

    for path in candidates:
        stem_lower = path.stem.lower()

        # Check stem match
        if query_lower in stem_lower:
            matches.append((len(stem_lower), True, path))
            continue

        # Check extracted title match (requires reading file)
        try:
            content = path.read_text(encoding="utf-8")
            title = extract_title(path, content)
            if query_lower in title.lower():
                matches.append((len(title), False, path))
        except Exception:
            continue

    if not matches:
        return None

    # Sort by: stem match preferred, then shortest title
    matches.sort(key=lambda x: (not x[1], x[0]))
    return matches[0][2]


def _is_valid_note(path: Path) -> bool:
    """Check if path is a valid note file."""
    return (
        path.is_file()
        and path.suffix.lower() == ".md"
        and not any(part.startswith(".") for part in path.parts)
    )


def _read_note_file(path: Path, vault_dir: Path) -> dict[str, Any]:
    """
    Read a note file and return structured data.

    Returns dict with: title, content, tags, created, modified, path
    """
    content = path.read_text(encoding="utf-8")
    created, modified = get_file_timestamps(path)
    rel_path = path.relative_to(vault_dir)

    return {
        "title": extract_title(path, content),
        "content": content,
        "tags": extract_tags(content),
        "created": created.isoformat() if created else None,
        "modified": modified.isoformat() if modified else None,
        "path": str(rel_path),
    }


# ---------------------------------------------------------------------------
# Search Functions
# ---------------------------------------------------------------------------

def search_notes(
    query: str,
    folder: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Search notes in the vault using ripgrep.

    Args:
        query: Search query (case-insensitive by default)
        folder: Optional subfolder to restrict search
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: title, path, excerpt, tags
    """
    vault_dir = get_vault_dir()

    if not vault_dir.exists():
        return []

    # Determine search path
    search_path = vault_dir / folder if folder else vault_dir
    if not search_path.exists():
        return []

    # Skip hidden directories like .obsidian
    # Use ripgrep with JSON output for parsing
    cmd = [
        "rg",
        "--json",           # JSON output for structured parsing
        "-i",               # Case insensitive
        "--glob", "*.md",   # Only markdown files
        "--glob", "!.obsidian/**",  # Exclude .obsidian directory
        "-l",               # List files only (faster initial scan)
        query,
        str(search_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,  # 5 second timeout
        )
    except subprocess.TimeoutExpired:
        return []
    except FileNotFoundError:
        # ripgrep not installed
        return _fallback_search(query, search_path, limit)

    if result.returncode not in (0, 1):  # 1 = no matches
        return []

    # Parse JSON lines output
    matching_files: list[Path] = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("type") == "match":
                file_path = data.get("data", {}).get("path", {}).get("text")
                if file_path:
                    matching_files.append(Path(file_path))
        except json.JSONDecodeError:
            continue

    # Process results up to limit
    results: list[dict[str, Any]] = []
    for path in matching_files[:limit]:
        try:
            content = path.read_text(encoding="utf-8")
            rel_path = path.relative_to(vault_dir)
            results.append({
                "title": extract_title(path, content),
                "path": str(rel_path),
                "excerpt": extract_excerpt(content, query),
                "tags": extract_tags(content),
            })
        except Exception:
            continue

    return results


def _fallback_search(
    query: str,
    search_path: Path,
    limit: int,
) -> list[dict[str, Any]]:
    """
    Fallback search using pure Python when ripgrep is not available.
    Slower but functional.
    """
    vault_dir = get_vault_dir()
    query_lower = query.lower()
    results: list[dict[str, Any]] = []

    for path in search_path.rglob("*.md"):
        # Skip hidden directories
        if any(part.startswith(".") for part in path.parts):
            continue

        try:
            content = path.read_text(encoding="utf-8")
            if query_lower in content.lower():
                rel_path = path.relative_to(vault_dir)
                results.append({
                    "title": extract_title(path, content),
                    "path": str(rel_path),
                    "excerpt": extract_excerpt(content, query),
                    "tags": extract_tags(content),
                })
                if len(results) >= limit:
                    break
        except Exception:
            continue

    return results
