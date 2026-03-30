#!/usr/bin/env python3
"""
Obsidian KM MCP Server for Lobster

Provides MCP tools for managing notes in an Obsidian vault:
- note_create: Create a new note with optional tags
- note_read: Read a note by title or path
- note_search: Full-text search using ripgrep
- note_append: Append content to an existing note
- note_list: List notes with optional filters

Design: Pure functional implementation backed by vault_ops.py.
All operations are explicit and side-effect-isolated.
"""

import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Add src directory to path for vault_ops import
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from vault_ops import (
    create_note,
    read_note,
    append_to_note,
    search_notes,
    list_notes,
    resolve_vault_path,
)


# =============================================================================
# Configuration
# =============================================================================

VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH", Path.home() / "obsidian-vault"))
LOG_PATH = Path(os.environ.get("OBSIDIAN_KM_LOG_PATH", Path.home() / "logs" / "obsidian-km-mcp.log"))


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging() -> logging.Logger:
    """Configure rotating file logger."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("obsidian-km-mcp")
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)

    return logger


logger = setup_logging()


# =============================================================================
# MCP Server
# =============================================================================

server = Server("obsidian-km")


def text_result(data: Any) -> list[TextContent]:
    """Format a result as MCP text content."""
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def error_result(msg: str) -> list[TextContent]:
    """Format an error as MCP text content."""
    logger.error(msg)
    return [TextContent(type="text", text=f"Error: {msg}")]


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available Obsidian KM tools."""
    return [
        Tool(
            name="note_create",
            description=(
                "Create a new note in the Obsidian vault. "
                "Notes are created with YAML frontmatter containing title, tags, and timestamps. "
                "Default folder is 'Inbox'. Returns the path to the created note."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The note title (used as filename)",
                    },
                    "content": {
                        "type": "string",
                        "description": "The note body content (markdown)",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder within the vault (default: 'Inbox')",
                        "default": "Inbox",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for frontmatter",
                    },
                },
                "required": ["title", "content"],
            },
        ),
        Tool(
            name="note_read",
            description=(
                "Read a note from the Obsidian vault by title or path. "
                "Returns the note's metadata (title, tags, created, modified) and content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title_or_path": {
                        "type": "string",
                        "description": "Note title or relative path within vault (e.g., 'Meeting Notes' or 'Projects/ProjectX.md')",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Optional folder to search in (when using title)",
                    },
                },
                "required": ["title_or_path"],
            },
        ),
        Tool(
            name="note_search",
            description=(
                "Full-text search across notes in the Obsidian vault using ripgrep. "
                "Returns matching notes with line numbers and content snippets. "
                "Supports regex patterns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (regex supported)",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Optional folder to limit search to",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="note_append",
            description=(
                "Append content to an existing note in the Obsidian vault. "
                "Updates the note's modified timestamp. "
                "Useful for adding items to lists, logs, or daily notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title_or_path": {
                        "type": "string",
                        "description": "Note title or relative path within vault",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to append",
                    },
                    "separator": {
                        "type": "string",
                        "description": "Separator between existing content and new content (default: newline)",
                        "default": "\n",
                    },
                },
                "required": ["title_or_path", "content"],
            },
        ),
        Tool(
            name="note_list",
            description=(
                "List notes in the Obsidian vault with optional filters. "
                "Can filter by folder and/or tag. "
                "Returns note metadata with content previews."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Optional folder to list from",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Optional tag filter (notes must have this tag)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum notes to return (default: 20)",
                        "default": 20,
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["modified", "created", "title"],
                        "description": "Sort field (default: 'modified')",
                        "default": "modified",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls by delegating to vault_ops functions."""

    logger.info(f"Tool call: {name} with args: {arguments}")

    try:
        if name == "note_create":
            title = arguments["title"]
            content = arguments["content"]
            folder = arguments.get("folder", "Inbox")
            tags = arguments.get("tags")

            path = create_note(
                title=title,
                content=content,
                folder=folder,
                tags=tags,
                vault=VAULT_PATH,
            )

            result = {
                "status": "created",
                "path": str(path.relative_to(VAULT_PATH)),
                "title": title,
                "folder": folder,
            }
            if tags:
                result["tags"] = tags

            logger.info(f"Created note: {result['path']}")
            return text_result(result)

        elif name == "note_read":
            title_or_path = arguments["title_or_path"]
            folder = arguments.get("folder")

            note = read_note(
                title_or_path=title_or_path,
                folder=folder,
                vault=VAULT_PATH,
            )

            logger.info(f"Read note: {note['path']}")
            return text_result(note)

        elif name == "note_search":
            query = arguments["query"]
            folder = arguments.get("folder")
            limit = arguments.get("limit", 10)

            matches = search_notes(
                query=query,
                folder=folder,
                limit=limit,
                vault=VAULT_PATH,
            )

            result = {
                "query": query,
                "count": len(matches),
                "matches": matches,
            }

            logger.info(f"Search '{query}': {len(matches)} matches")
            return text_result(result)

        elif name == "note_append":
            title_or_path = arguments["title_or_path"]
            content = arguments["content"]
            separator = arguments.get("separator", "\n")

            note = append_to_note(
                title_or_path=title_or_path,
                content=content,
                separator=separator,
                vault=VAULT_PATH,
            )

            result = {
                "status": "appended",
                "path": note["path"],
                "title": note["title"],
                "modified": note["modified"],
            }

            logger.info(f"Appended to note: {note['path']}")
            return text_result(result)

        elif name == "note_list":
            folder = arguments.get("folder")
            tag = arguments.get("tag")
            limit = arguments.get("limit", 20)
            sort = arguments.get("sort", "modified")

            result = list_notes(
                folder=folder,
                tag=tag,
                limit=limit,
                sort=sort,
                vault=VAULT_PATH,
            )

            logger.info(f"Listed {result['total']} notes (showing {len(result['notes'])})")
            return text_result(result)

        else:
            return error_result(f"Unknown tool: {name}")

    except FileNotFoundError as e:
        return error_result(f"Note not found: {e}")
    except FileExistsError as e:
        return error_result(f"Note already exists: {e}")
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return error_result(f"{type(e).__name__}: {str(e)}")


# =============================================================================
# Main Entry Point
# =============================================================================

async def main():
    """Run the MCP server."""
    logger.info(f"Starting obsidian-km MCP server, vault: {VAULT_PATH}")

    # Ensure vault directory exists
    if not VAULT_PATH.exists():
        VAULT_PATH.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created vault directory: {VAULT_PATH}")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
