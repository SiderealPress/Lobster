#!/usr/bin/env python3
"""
Obsidian KM MCP Server

MCP server providing knowledge management tools for Obsidian vaults.
Uses FastMCP for clean tool registration.

Tools:
- note_create: Create a new note with YAML frontmatter
"""

import sys
from pathlib import Path

# Ensure the src directory is in the path for imports
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

from vault_ops import create_note

mcp = FastMCP("obsidian-km")


@mcp.tool()
def note_create(
    title: str,
    content: str,
    folder: str = "Inbox",
    tags: list[str] | None = None,
) -> str:
    """
    Create a new note in the Obsidian vault.

    Creates a markdown file with YAML frontmatter containing title, tags,
    and creation timestamp (ISO 8601). The note is placed in the specified
    folder within the vault.

    Args:
        title: Note title (also used as filename after sanitization)
        content: Note content in markdown format
        folder: Target folder within the vault (default: "Inbox")
        tags: Optional list of tags to include in frontmatter

    Returns:
        Success message with the created file path

    Raises:
        FileExistsError: If a note with this title already exists
        ValueError: If title contains only invalid filename characters
    """
    try:
        path = create_note(
            title=title,
            content=content,
            folder=folder,
            tags=tags or [],
        )
        return f"Created note at {path}"
    except FileExistsError as e:
        return f"Error: {e}"
    except ValueError as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
