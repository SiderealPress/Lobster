#!/usr/bin/env python3
"""
Obsidian KM MCP Server for Lobster

Provides note management tools for Obsidian vault access via Telegram.

Tools provided:
- note_search: Full-text search across vault notes
- note_read: Read a specific note by title or path
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HOME = Path.home()
VAULT_DIR = Path(os.environ.get("OBSIDIAN_VAULT_DIR", _HOME / "obsidian-vault"))


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("obsidian-km")


def text_result(data: Any) -> list[TextContent]:
    """Format a successful result as JSON text content."""
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def error_result(msg: str) -> list[TextContent]:
    """Format an error message as text content."""
    return [TextContent(type="text", text=f"Error: {msg}")]


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available Obsidian KM tools."""
    return [
        Tool(
            name="note_search",
            description=(
                "Search notes in the Obsidian vault using full-text search (ripgrep). "
                "Returns matching notes with title, path, excerpt, and tags. "
                "Case-insensitive by default. Optionally restrict search to a subfolder."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Matches are case-insensitive.",
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Optional subfolder to restrict search. "
                            "Relative to vault root (e.g., 'projects' or 'daily/2024')."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Default: 10.",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="note_read",
            description=(
                "Read a specific note by title or path. "
                "Returns the full note content along with metadata (title, tags, timestamps). "
                "Supports exact match by path, exact match by title (case-insensitive), "
                "and fuzzy match (case-insensitive partial title match) as fallback."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title_or_path": {
                        "type": "string",
                        "description": (
                            "Note title or relative path. "
                            "Examples: 'My Note', 'projects/project-plan', 'daily/2024-01-15.md'. "
                            "The .md extension is optional."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Optional subfolder to restrict search. "
                            "Relative to vault root (e.g., 'projects' or 'daily/2024')."
                        ),
                    },
                },
                "required": ["title_or_path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls."""
    try:
        if name == "note_search":
            return await handle_note_search(arguments)
        elif name == "note_read":
            return await handle_note_read(arguments)
        else:
            return error_result(f"Unknown tool: {name}")
    except Exception as e:
        return error_result(f"Tool '{name}' failed: {e}")


async def handle_note_search(args: dict) -> list[TextContent]:
    """
    Search notes in the Obsidian vault.

    Uses ripgrep for fast full-text search. Returns list of matching notes
    with title, path, excerpt (context around match), and tags.
    """
    from vault_ops import search_notes

    query = str(args.get("query", "")).strip()
    if not query:
        return error_result("'query' is required")

    folder = args.get("folder")
    if folder is not None:
        folder = str(folder).strip() or None

    limit = int(args.get("limit", 10))
    limit = max(1, min(limit, 100))  # Clamp to 1-100

    # Check vault exists
    if not VAULT_DIR.exists():
        return error_result(f"Vault directory not found: {VAULT_DIR}")

    print(f"[INFO] Searching vault for: {query!r} (folder={folder}, limit={limit})", file=sys.stderr)

    # Run search in thread pool to avoid blocking
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(
        None,
        lambda: search_notes(query=query, folder=folder, limit=limit),
    )

    if not results:
        return text_result({
            "query": query,
            "folder": folder,
            "count": 0,
            "results": [],
        })

    return text_result({
        "query": query,
        "folder": folder,
        "count": len(results),
        "results": results,
    })


async def handle_note_read(args: dict) -> list[TextContent]:
    """
    Read a specific note by title or path.

    Returns the full note content along with metadata:
    - title: Extracted title (from frontmatter, H1, or filename)
    - content: Full markdown content
    - tags: List of tags (from frontmatter and inline)
    - created: ISO timestamp of file creation
    - modified: ISO timestamp of last modification
    - path: Relative path from vault root
    """
    from vault_ops import read_note

    title_or_path = str(args.get("title_or_path", "")).strip()
    if not title_or_path:
        return error_result("'title_or_path' is required")

    folder = args.get("folder")
    if folder is not None:
        folder = str(folder).strip() or None

    # Check vault exists
    if not VAULT_DIR.exists():
        return error_result(f"Vault directory not found: {VAULT_DIR}")

    print(f"[INFO] Reading note: {title_or_path!r} (folder={folder})", file=sys.stderr)

    # Run read in thread pool to avoid blocking
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: read_note(title_or_path=title_or_path, folder=folder),
    )

    if result is None:
        # Provide helpful error message
        search_location = f"folder '{folder}'" if folder else "vault"
        return error_result(
            f"Note not found: '{title_or_path}' in {search_location}. "
            "Try using note_search to find available notes."
        )

    return text_result(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    print("[INFO] Obsidian KM MCP Server starting...", file=sys.stderr)
    print(f"[INFO] Vault directory: {VAULT_DIR}", file=sys.stderr)
    print(f"[INFO] Vault exists: {VAULT_DIR.exists()}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
