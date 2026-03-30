"""
Obsidian Vault Operations — Pure Functions for Note Manipulation

This module provides pure functions for reading, parsing, and modifying
Obsidian markdown notes. Side effects (file I/O) are isolated at the
boundaries via atomic write operations.

Design principles:
- Pure functions for parsing and transformation
- Immutable data structures where practical
- Atomic writes via write-to-temp-then-rename
- Clear separation of frontmatter and body
"""

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple


# =============================================================================
# Data Types
# =============================================================================

@dataclass(frozen=True)
class ParsedNote:
    """Immutable representation of a parsed Obsidian note."""
    frontmatter: dict
    body: str
    raw_frontmatter: str  # Original YAML string (for minimal modifications)


@dataclass(frozen=True)
class AppendResult:
    """Result of an append operation."""
    file_path: str
    char_count: int
    modified_at: str


# =============================================================================
# Frontmatter Parsing — Pure Functions
# =============================================================================

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?",
    re.DOTALL
)


def parse_frontmatter(content: str) -> Tuple[dict, str, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (frontmatter_dict, body, raw_frontmatter_yaml).
    If no frontmatter exists, returns ({}, content, "").

    Note: Uses a simple key-value parser to avoid yaml dependency.
    For complex nested structures, consider adding pyyaml.
    """
    match = _FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}, content, ""

    raw_yaml = match.group(1)
    body = content[match.end():]
    frontmatter = _parse_simple_yaml(raw_yaml)

    return frontmatter, body, raw_yaml


def _parse_simple_yaml(yaml_str: str) -> dict:
    """Parse simple key: value YAML (no nested structures).

    Handles:
    - Simple scalars: key: value
    - Quoted strings: key: "value" or key: 'value'
    - Arrays: key: [a, b, c] (single line only)
    - Multiline ignored for simplicity
    """
    result = {}
    for line in yaml_str.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if ':' not in line:
            continue

        key, _, value = line.partition(':')
        key = key.strip()
        value = value.strip()

        # Remove quotes if present
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]

        # Parse simple arrays
        if value.startswith('[') and value.endswith(']'):
            items = value[1:-1].split(',')
            value = [item.strip().strip('"').strip("'") for item in items if item.strip()]

        result[key] = value

    return result


def serialize_frontmatter(frontmatter: dict) -> str:
    """Serialize frontmatter dict back to YAML string.

    Produces minimal, clean YAML output.
    """
    if not frontmatter:
        return ""

    lines = []
    for key, value in frontmatter.items():
        if isinstance(value, list):
            formatted = '[' + ', '.join(f'"{v}"' if ' ' in str(v) else str(v) for v in value) + ']'
            lines.append(f"{key}: {formatted}")
        elif isinstance(value, str) and (' ' in value or ':' in value):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")

    return '\n'.join(lines)


# =============================================================================
# Note Transformation — Pure Functions
# =============================================================================

def append_to_body(body: str, content: str, separator: str = "\n") -> str:
    """Append content to the note body with the given separator.

    Pure function: returns new string, does not mutate input.
    """
    if not body.strip():
        return content

    # Ensure body ends cleanly before appending
    trimmed_body = body.rstrip()
    return f"{trimmed_body}{separator}{content}"


def update_modified_timestamp(frontmatter: dict) -> dict:
    """Return a new frontmatter dict with updated 'modified' timestamp.

    Pure function: returns new dict, does not mutate input.
    Uses ISO 8601 format compatible with Obsidian.
    """
    new_frontmatter = dict(frontmatter)
    new_frontmatter['modified'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return new_frontmatter


def assemble_note(frontmatter: dict, body: str) -> str:
    """Assemble a complete note from frontmatter and body.

    Pure function: returns complete markdown string.
    """
    if not frontmatter:
        return body

    yaml_content = serialize_frontmatter(frontmatter)
    return f"---\n{yaml_content}\n---\n\n{body}"


# =============================================================================
# File Operations — Side Effects at Boundaries
# =============================================================================

def read_note(file_path: Path) -> str:
    """Read note content from disk.

    Raises FileNotFoundError if note doesn't exist.
    """
    return file_path.read_text(encoding='utf-8')


def atomic_write(file_path: Path, content: str) -> None:
    """Atomically write content to file using temp-then-rename pattern.

    This prevents data loss on crash or power failure:
    1. Write to temp file in same directory
    2. Sync to disk (fsync)
    3. Rename temp to target (atomic on POSIX)
    """
    parent = file_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(parent),
        suffix='.tmp',
        prefix='.note-'
    )

    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(file_path))
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================================================
# High-Level Operations — Composition of Pure Functions
# =============================================================================

def append_to_note(
    file_path: Path,
    content: str,
    separator: str = "\n"
) -> AppendResult:
    """Append content to an existing Obsidian note.

    This is the main entry point for the note_append MCP tool.

    Args:
        file_path: Path to the note (must exist)
        content: Content to append
        separator: Separator between existing content and new content

    Returns:
        AppendResult with file path, new char count, and modification timestamp

    Raises:
        FileNotFoundError: If the note doesn't exist
    """
    # Read existing note
    existing_content = read_note(file_path)

    # Parse into components
    frontmatter, body, _raw = parse_frontmatter(existing_content)

    # Transform (pure functions)
    new_body = append_to_body(body, content, separator)
    new_frontmatter = update_modified_timestamp(frontmatter)
    new_content = assemble_note(new_frontmatter, new_body)

    # Write atomically (side effect)
    atomic_write(file_path, new_content)

    return AppendResult(
        file_path=str(file_path),
        char_count=len(new_content),
        modified_at=new_frontmatter.get('modified', '')
    )


def resolve_note_path(
    vault_path: Path,
    title_or_path: str
) -> Path:
    """Resolve a note title or relative path to an absolute path.

    Handles:
    - Absolute paths (returned as-is if within vault)
    - Relative paths (resolved from vault root)
    - Titles without .md extension (appended automatically)

    Raises:
        ValueError: If path is outside vault
    """
    # If it looks like an absolute path
    if title_or_path.startswith('/'):
        candidate = Path(title_or_path)
    else:
        # Ensure .md extension
        if not title_or_path.endswith('.md'):
            title_or_path = f"{title_or_path}.md"
        candidate = vault_path / title_or_path

    # Resolve to absolute and check it's within vault
    resolved = candidate.resolve()
    vault_resolved = vault_path.resolve()

    try:
        resolved.relative_to(vault_resolved)
    except ValueError:
        raise ValueError(f"Path '{title_or_path}' is outside vault: {vault_path}")

    return resolved
