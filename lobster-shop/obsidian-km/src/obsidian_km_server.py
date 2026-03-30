#!/usr/bin/env python3
"""
Obsidian Knowledge Management MCP Server for Lobster

Provides MCP tools for interacting with an Obsidian vault:
- note_append: Append content to an existing note

The server reads the vault path from OBSIDIAN_VAULT_PATH environment variable.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from vault_ops import (
    append_to_note,
    resolve_note_path,
    AppendResult,
)


# =============================================================================
# Configuration
# =============================================================================

def get_vault_path() -> Path:
    """Get the Obsidian vault path from environment."""
    vault_path = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise ValueError(
            "OBSIDIAN_VAULT_PATH environment variable not set. "
            "Set it to your Obsidian vault directory."
        )
    path = Path(vault_path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Vault path does not exist: {path}")
    return path


# =============================================================================
# MCP Server Setup
# =============================================================================

server = Server("obsidian-km")


def text_result(data: Any) -> list[TextContent]:
    """Format a result as MCP text content."""
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def error_result(msg: str) -> list[TextContent]:
    """Format an error as MCP text content."""
    return [TextContent(type="text", text=f"Error: {msg}")]


def result_to_dict(result: AppendResult) -> dict:
    """Convert AppendResult to JSON-serializable dict."""
    return {
        "file_path": result.file_path,
        "char_count": result.char_count,
        "modified_at": result.modified_at,
    }


# =============================================================================
# Tool Definitions
# =============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available Obsidian KM tools."""
    return [
        Tool(
            name="note_append",
            description=(
                "Append content to an existing Obsidian note. "
                "Preserves frontmatter and updates the 'modified' timestamp. "
                "Returns the file path and new character count. "
                "Use this to add journal entries, meeting notes, or any content "
                "that should be appended to an existing note."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title_or_path": {
                        "type": "string",
                        "description": (
                            "Note title (e.g., 'Daily Notes/2024-01-15') or "
                            "relative path from vault root. The .md extension "
                            "is added automatically if not present."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to append to the note.",
                    },
                    "separator": {
                        "type": "string",
                        "description": (
                            "Separator between existing content and new content. "
                            "Defaults to newline. Use '\\n\\n' for paragraph break, "
                            "'\\n---\\n' for horizontal rule, etc."
                        ),
                        "default": "\n",
                    },
                },
                "required": ["title_or_path", "content"],
            },
        ),
    ]


# =============================================================================
# Tool Implementation
# =============================================================================

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""

    if name == "note_append":
        return await handle_note_append(arguments)
    else:
        return error_result(f"Unknown tool: {name}")


async def handle_note_append(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle the note_append tool call."""

    # Validate required arguments
    title_or_path = arguments.get("title_or_path")
    content = arguments.get("content")

    if not title_or_path:
        return error_result("Missing required argument: title_or_path")
    if not content:
        return error_result("Missing required argument: content")

    separator = arguments.get("separator", "\n")

    try:
        # Get vault path
        vault_path = get_vault_path()

        # Resolve note path
        note_path = resolve_note_path(vault_path, title_or_path)

        # Check note exists (no create)
        if not note_path.exists():
            return error_result(
                f"Note not found: {title_or_path}. "
                f"This tool only appends to existing notes."
            )

        # Perform append operation (runs in thread pool to avoid blocking)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            append_to_note,
            note_path,
            content,
            separator,
        )

        return text_result(result_to_dict(result))

    except ValueError as e:
        return error_result(str(e))
    except FileNotFoundError as e:
        return error_result(f"Note not found: {e}")
    except PermissionError as e:
        return error_result(f"Permission denied: {e}")
    except Exception as e:
        return error_result(f"{type(e).__name__}: {e}")


# =============================================================================
# Main Entry Point
# =============================================================================

async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
