#!/usr/bin/env python3
"""
Lobster Bridge — Local MCP Server (Read-Only)

A standalone MCP server that reads canonical memory files from a local
directory (e.g., a git clone of the Lobster repo). Provides the same
convenience tool interface as the VPS bridge, but works entirely offline
with no network dependency.

Configuration:
    LOBSTER_CANONICAL_DIR — Path to the canonical memory directory
                            (e.g., /Users/drew/projects/lobster/memory/canonical)

Usage:
    LOBSTER_CANONICAL_DIR=/path/to/canonical python lobster_bridge_local.py

Claude Code config (~/.claude/settings.json):
    {
      "mcpServers": {
        "lobster-bridge": {
          "command": "python",
          "args": ["/path/to/lobster_bridge_local.py"],
          "env": {
            "LOBSTER_CANONICAL_DIR": "/path/to/memory/canonical"
          }
        }
      }
    }
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

_raw_dir = os.environ.get("LOBSTER_CANONICAL_DIR", "")
CANONICAL_DIR = Path(_raw_dir) if _raw_dir else Path(".")


def _validate_config() -> None:
    """Validate CANONICAL_DIR at startup. Called from main(), not at import time."""
    if not _raw_dir or not CANONICAL_DIR.is_absolute():
        print(
            "Error: LOBSTER_CANONICAL_DIR must be set to an absolute path.\n"
            "Example: LOBSTER_CANONICAL_DIR=/Users/drew/projects/lobster/memory/canonical",
            file=sys.stderr,
        )
        sys.exit(1)

# ---------------------------------------------------------------------------
# Pure helpers — shared logic with no side effects beyond file reads
# ---------------------------------------------------------------------------


def _read_canonical_file(canonical_dir: Path, relative_path: str, missing_message: str) -> str:
    """Read a file under canonical_dir or return a fallback message."""
    path = canonical_dir / relative_path
    if path.exists():
        return path.read_text()
    return missing_message


def _list_project_names(canonical_dir: Path) -> list[dict]:
    """List project markdown files under canonical_dir/projects/."""
    projects_dir = canonical_dir / "projects"
    if not projects_dir.exists():
        return []
    return [
        {"name": f.stem, "path": str(f)}
        for f in sorted(projects_dir.glob("*.md"))
    ]


def _get_project_context(canonical_dir: Path, project: str) -> str:
    """Read a project file or return available projects as a fallback."""
    if "/" in project or "\\" in project or ".." in project:
        return "Error: invalid project name."

    path = canonical_dir / "projects" / f"{project}.md"
    if path.exists():
        return path.read_text()

    available = (
        [f.stem for f in (canonical_dir / "projects").glob("*.md")]
        if (canonical_dir / "projects").exists()
        else []
    )
    return f"No project file for '{project}'. Available: {', '.join(available) or 'none'}"


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("lobster-bridge-local")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available convenience tools."""
    return [
        Tool(
            name="get_priorities",
            description="Fetch Lobster's current priority stack. Returns the canonical priorities.md file, updated nightly by the consolidation process.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_project_context",
            description="Fetch status and context for a specific project. Returns project status, recent decisions, pending items, and blockers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name (e.g., 'lobster', 'govscan', 'transformers')",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="get_daily_digest",
            description="Fetch the latest daily digest. Summarizes recent activity: key conversations, task progress, decisions made, and items needing follow-up.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_projects",
            description="List all projects tracked in Lobster's canonical memory. Returns project names for use with get_project_context().",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_handoff",
            description="Read the current handoff document. Contains identity, architecture, current state, and pending items.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls to pure reader functions."""
    dispatch = {
        "get_priorities": lambda args: _read_canonical_file(
            CANONICAL_DIR,
            "priorities.md",
            "No priorities file found. Nightly consolidation has not run yet.",
        ),
        "get_daily_digest": lambda args: _read_canonical_file(
            CANONICAL_DIR,
            "daily-digest.md",
            "No daily digest found. Nightly consolidation has not run yet.",
        ),
        "get_handoff": lambda args: _read_canonical_file(
            CANONICAL_DIR,
            "handoff.md",
            "No handoff document found.",
        ),
        "get_project_context": lambda args: _get_project_context(
            CANONICAL_DIR,
            args.get("project", ""),
        ),
        "list_projects": lambda args: json.dumps(_list_project_names(CANONICAL_DIR), indent=2)
        if _list_project_names(CANONICAL_DIR)
        else "No project files found in canonical memory.",
    }

    handler = dispatch.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        result = handler(arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        return [TextContent(type="text", text=f"Error in {name}: {e}")]


async def main():
    """Run the local MCP server over stdio."""
    _validate_config()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
