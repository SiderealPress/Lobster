#!/usr/bin/env python3
"""
Obsidian Vault Operations

Pure functions for managing Obsidian vault operations.
The vault path is configured via OBSIDIAN_VAULT_PATH environment variable,
defaulting to ~/obsidian-vault.
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def get_vault_path() -> Path:
    """Get the configured Obsidian vault path."""
    return Path(os.environ.get("OBSIDIAN_VAULT_PATH", Path.home() / "obsidian-vault"))


def sanitize_filename(title: str) -> str:
    """
    Sanitize a title for use as a filename.

    Removes characters that are invalid in filenames across platforms:
    / \\ : * ? " < > |

    Also strips leading/trailing whitespace and collapses multiple spaces.
    """
    # Remove invalid filename characters
    sanitized = re.sub(r'[/\\:*?"<>|]', '', title)
    # Collapse multiple spaces and strip
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    # Ensure we have something left
    if not sanitized:
        sanitized = "Untitled"
    return sanitized


def format_frontmatter(title: str, tags: list[str], created: datetime) -> str:
    """
    Format YAML frontmatter for an Obsidian note.

    Args:
        title: Note title
        tags: List of tags (without # prefix)
        created: Creation timestamp

    Returns:
        YAML frontmatter string including delimiters
    """
    lines = ["---"]
    lines.append(f"title: \"{title}\"")

    if tags:
        # Format tags as YAML array
        lines.append("tags:")
        for tag in tags:
            # Strip # prefix if present
            clean_tag = tag.lstrip('#')
            lines.append(f"  - {clean_tag}")
    else:
        lines.append("tags: []")

    # ISO 8601 format with timezone
    lines.append(f"created: {created.isoformat()}")
    lines.append("---")

    return "\n".join(lines)


def create_note(
    title: str,
    content: str,
    folder: str = "Inbox",
    tags: Optional[list[str]] = None,
    vault_path: Optional[Path] = None,
) -> Path:
    """
    Create a new note in the Obsidian vault.

    Args:
        title: Note title (will be sanitized for filename)
        content: Note content (markdown)
        folder: Target folder within vault (default: "Inbox")
        tags: Optional list of tags for frontmatter
        vault_path: Optional vault path override (defaults to OBSIDIAN_VAULT_PATH)

    Returns:
        Path to the created note file

    Raises:
        FileExistsError: If a note with this title already exists in the folder
        ValueError: If title is empty after sanitization
    """
    if tags is None:
        tags = []

    if vault_path is None:
        vault_path = get_vault_path()

    # Sanitize and validate filename
    safe_title = sanitize_filename(title)
    if safe_title == "Untitled" and title.strip():
        # Original title had content but sanitized to nothing meaningful
        raise ValueError(f"Title '{title}' contains only invalid characters")

    # Ensure folder exists
    folder_path = vault_path / folder
    folder_path.mkdir(parents=True, exist_ok=True)

    # Build note path
    note_path = folder_path / f"{safe_title}.md"

    # Check for existing note (no overwrite)
    if note_path.exists():
        raise FileExistsError(f"Note already exists: {note_path}")

    # Generate frontmatter
    created = datetime.now(timezone.utc)
    frontmatter = format_frontmatter(title, tags, created)

    # Combine frontmatter and content
    full_content = f"{frontmatter}\n\n{content}"

    # Write the note
    note_path.write_text(full_content, encoding="utf-8")

    return note_path
