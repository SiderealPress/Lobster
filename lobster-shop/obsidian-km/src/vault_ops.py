"""
Vault operations for Obsidian KM skill.

Pure functions for reading, writing, and searching notes in an Obsidian vault.
"""

import json
import os
import re
import subprocess
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
