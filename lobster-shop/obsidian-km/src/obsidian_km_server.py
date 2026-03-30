#!/usr/bin/env python3
"""
Obsidian KM MCP Server for Lobster

Provides MCP tools for interacting with Obsidian vaults.

Tools provided:
- note_list: List notes with folder/tag filters, sorting, and pagination
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

# Import vault operations
from vault_ops import list_notes, ListNotesResult, SortOrder

# Configuration from environment or preferences
VAULT_PATH = os.environ.get("OBSIDIAN_VAULT_PATH", "")
DEFAULT_LIMIT = int(os.environ.get("OBSIDIAN_DEFAULT_LIMIT", "20"))
DEFAULT_SORT = os.environ.get("OBSIDIAN_DEFAULT_SORT", "modified")

server = Server("obsidian-km")


def text_result(data: Any) -> list[TextContent]:
    """Format a result as MCP text content."""
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def error_result(msg: str) -> list[TextContent]:
    """Format an error as MCP text content."""
    return [TextContent(type="text", text=f"Error: {msg}")]


def validate_vault_path(vault_path: str) -> str | None:
    """
    Validate vault path configuration.

    Returns error message if invalid, None if valid.
    """
    if not vault_path:
        return (
            "Vault path not configured. "
            "Set it with: /skill set obsidian-km vault_path /path/to/vault"
        )

    path = Path(vault_path)
    if not path.exists():
        return f"Vault path does not exist: {vault_path}"

    if not path.is_dir():
        return f"Vault path is not a directory: {vault_path}"

    return None


def validate_sort(sort: str) -> SortOrder:
    """Validate and normalize sort parameter."""
    valid_sorts = ("modified", "created", "title")
    sort_lower = sort.lower()
    if sort_lower in valid_sorts:
        return sort_lower  # type: ignore
    return "modified"


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available obsidian-km tools."""
    return [
        Tool(
            name="note_list",
            description=(
                "List notes in an Obsidian vault with optional filtering and sorting. "
                "Returns note metadata including title, path, tags, timestamps, and size. "
                "Supports folder filtering, tag filtering (checks YAML frontmatter), "
                "and sorting by modified date, created date, or title."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": (
                            "Filter to notes within this folder path "
                            "(relative to vault root, e.g., 'projects/active')"
                        ),
                    },
                    "tag": {
                        "type": "string",
                        "description": (
                            "Filter to notes containing this tag "
                            "(checks YAML frontmatter 'tags' field, e.g., 'project')"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of notes to return",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 1000,
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order for results",
                        "enum": ["modified", "created", "title"],
                        "default": "modified",
                    },
                },
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""

    if name == "note_list":
        # Get vault path from env or arguments
        vault_path = arguments.get("vault_path", VAULT_PATH)

        # Validate vault path
        error = validate_vault_path(vault_path)
        if error:
            return error_result(error)

        # Extract and validate parameters
        folder = arguments.get("folder")
        tag = arguments.get("tag")
        limit = arguments.get("limit", DEFAULT_LIMIT)
        sort = validate_sort(arguments.get("sort", DEFAULT_SORT))

        # Clamp limit to valid range
        limit = max(1, min(1000, int(limit)))

        try:
            # Run the pure function (blocking I/O wrapped in executor)
            result: ListNotesResult = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: list_notes(
                    vault_path=vault_path,
                    folder=folder,
                    tag=tag,
                    limit=limit,
                    sort=sort,
                ),
            )

            return text_result(result.to_dict())

        except Exception as e:
            return error_result(f"Failed to list notes: {type(e).__name__}: {str(e)}")

    else:
        return error_result(f"Unknown tool: {name}")


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
