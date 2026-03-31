#!/usr/bin/env python3
"""
Obsidian Knowledge Management MCP Server for Lobster

Provides tools for interacting with an Obsidian vault:
- Create notes in the vault
- Search vault content
- Capture and archive links
- Manage tags and organization

Configuration is loaded from ~/lobster-config/obsidian.env at startup.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------

def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Ignores comments and blank lines."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            result[key] = val
    return result


def _load_obsidian_config() -> dict[str, str]:
    """Load Obsidian config from ~/lobster-config/obsidian.env.

    Falls back to environment variables if file doesn't exist.
    """
    config_file = Path.home() / "lobster-config" / "obsidian.env"
    return _load_env_file(config_file)


# Load configuration at module startup
_CONFIG = _load_obsidian_config()

# ---------------------------------------------------------------------------
# Preference Accessors
# ---------------------------------------------------------------------------

def _get_config(key: str, default: str = "") -> str:
    """Get config value, checking env vars first, then config file."""
    return os.getenv(key, _CONFIG.get(key, default))


# Preference constants (loaded from obsidian.env with defaults)
DEFAULT_FOLDER = _get_config("OBSIDIAN_DEFAULT_FOLDER", "Inbox")
AUTO_CAPTURE_LINKS = _get_config("OBSIDIAN_AUTO_CAPTURE_LINKS", "true").lower() == "true"
DEFAULT_TAGS = [t.strip() for t in _get_config("OBSIDIAN_DEFAULT_TAGS", "").split(",") if t.strip()]
LINK_FOLDER = _get_config("OBSIDIAN_LINK_FOLDER", "Links")
MAX_SEARCH_RESULTS = int(_get_config("OBSIDIAN_MAX_SEARCH_RESULTS", "10"))
VAULT_PATH = _get_config("OBSIDIAN_VAULT_PATH", "")

# ---------------------------------------------------------------------------
# Vault Operations
# ---------------------------------------------------------------------------

def _get_vault_path() -> Path | None:
    """Get the configured vault path, or None if not configured."""
    if not VAULT_PATH:
        return None
    path = Path(VAULT_PATH).expanduser()
    if path.exists() and path.is_dir():
        return path
    return None


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    # Remove or replace invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*]', '', name)
    # Collapse multiple spaces/underscores
    sanitized = re.sub(r'[\s_]+', ' ', sanitized)
    return sanitized.strip()[:200]  # Limit length


def _format_tags(tags: list[str]) -> str:
    """Format tags for Obsidian frontmatter."""
    if not tags:
        return ""
    return ", ".join(f'"{t}"' for t in tags)


def _create_note(
    title: str,
    content: str,
    folder: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new note in the Obsidian vault.

    Returns dict with 'path' on success, or 'error' on failure.
    """
    vault = _get_vault_path()
    if not vault:
        return {"error": "OBSIDIAN_VAULT_PATH not configured. Add it to ~/lobster-config/obsidian.env"}

    # Use default folder if not specified
    target_folder = folder or DEFAULT_FOLDER
    folder_path = vault / target_folder
    folder_path.mkdir(parents=True, exist_ok=True)

    # Combine default tags with any specified tags
    all_tags = list(DEFAULT_TAGS)
    if tags:
        all_tags.extend(t for t in tags if t not in all_tags)

    # Generate filename
    safe_title = _sanitize_filename(title)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_title}.md" if safe_title else f"Note_{timestamp}.md"
    note_path = folder_path / filename

    # Handle existing files
    if note_path.exists():
        filename = f"{safe_title}_{timestamp}.md" if safe_title else f"Note_{timestamp}.md"
        note_path = folder_path / filename

    # Build note content with frontmatter
    lines = ["---"]
    lines.append(f'title: "{title}"')
    lines.append(f"created: {datetime.now(timezone.utc).isoformat()}")
    if all_tags:
        lines.append(f"tags: [{_format_tags(all_tags)}]")
    lines.append("source: lobster")
    lines.append("---")
    lines.append("")
    lines.append(content)

    try:
        note_path.write_text("\n".join(lines), encoding="utf-8")
        return {
            "path": str(note_path),
            "relative_path": str(note_path.relative_to(vault)),
            "folder": target_folder,
            "title": title,
        }
    except Exception as e:
        return {"error": f"Failed to create note: {e}"}


def _search_vault(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    """Search the vault for notes matching the query.

    Returns a list of matching notes with metadata.
    """
    vault = _get_vault_path()
    if not vault:
        return [{"error": "OBSIDIAN_VAULT_PATH not configured"}]

    limit = max_results or MAX_SEARCH_RESULTS
    results: list[dict[str, Any]] = []
    query_lower = query.lower()

    for md_file in vault.rglob("*.md"):
        if len(results) >= limit:
            break

        try:
            content = md_file.read_text(encoding="utf-8")
            # Search in filename and content
            if query_lower in md_file.stem.lower() or query_lower in content.lower():
                # Extract title from frontmatter or filename
                title = md_file.stem
                if match := re.search(r'^title:\s*["\']?([^"\'\n]+)', content, re.MULTILINE):
                    title = match.group(1).strip()

                results.append({
                    "path": str(md_file.relative_to(vault)),
                    "title": title,
                    "modified": datetime.fromtimestamp(md_file.stat().st_mtime).isoformat(),
                })
        except Exception:
            continue  # Skip unreadable files

    return results


def _capture_link(
    url: str,
    title: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Capture and archive a link to the vault.

    Creates a note in LINK_FOLDER with the link details.
    """
    if not AUTO_CAPTURE_LINKS:
        return {"skipped": True, "reason": "Auto-capture links is disabled"}

    # Generate title from URL if not provided
    if not title:
        # Extract domain or path for title
        from urllib.parse import urlparse
        parsed = urlparse(url)
        title = parsed.netloc or parsed.path or url[:50]

    # Build note content
    content_lines = [
        f"# {title}",
        "",
        f"**URL:** {url}",
        f"**Captured:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]

    if notes:
        content_lines.extend(["", "## Notes", "", notes])

    return _create_note(
        title=f"Link - {title}",
        content="\n".join(content_lines),
        folder=LINK_FOLDER,
        tags=["link", "captured"],
    )


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("obsidian-km")


def text_result(data: Any) -> list[TextContent]:
    """Format a result as MCP text content."""
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def error_result(msg: str) -> list[TextContent]:
    """Format an error as MCP text content."""
    return [TextContent(type="text", text=f"Error: {msg}")]


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available Obsidian KM tools."""
    return [
        Tool(
            name="obsidian_create_note",
            description=(
                "Create a new note in the Obsidian vault. "
                f"Default folder: '{DEFAULT_FOLDER}'. "
                f"Default tags: {DEFAULT_TAGS or '(none)'}. "
                "Requires OBSIDIAN_VAULT_PATH in ~/lobster-config/obsidian.env."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Title of the note",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content of the note (markdown)",
                    },
                    "folder": {
                        "type": "string",
                        "description": f"Folder to create the note in (default: {DEFAULT_FOLDER})",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional tags to apply to the note",
                    },
                },
                "required": ["title", "content"],
            },
        ),
        Tool(
            name="obsidian_search",
            description=(
                f"Search the Obsidian vault for notes matching a query. "
                f"Returns up to {MAX_SEARCH_RESULTS} results by default. "
                "Searches both filenames and content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (searches filenames and content)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Maximum results to return (default: {MAX_SEARCH_RESULTS})",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="obsidian_capture_link",
            description=(
                f"Capture and archive a link to the Obsidian vault in the '{LINK_FOLDER}' folder. "
                f"Auto-capture is {'enabled' if AUTO_CAPTURE_LINKS else 'disabled'}. "
                "Creates a note with the URL, title, and optional notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to capture",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the link (extracted from URL if not provided)",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes or context about the link",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="obsidian_get_preferences",
            description="Get current Obsidian KM preferences and configuration.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls."""
    try:
        if name == "obsidian_create_note":
            return await handle_create_note(arguments)
        elif name == "obsidian_search":
            return await handle_search(arguments)
        elif name == "obsidian_capture_link":
            return await handle_capture_link(arguments)
        elif name == "obsidian_get_preferences":
            return await handle_get_preferences(arguments)
        else:
            return error_result(f"Unknown tool: {name}")
    except Exception as e:
        return error_result(f"Tool '{name}' failed: {e}")


async def handle_create_note(args: dict) -> list[TextContent]:
    """Handle obsidian_create_note tool call."""
    title = str(args.get("title", "")).strip()
    content = str(args.get("content", "")).strip()
    folder = args.get("folder")
    tags = args.get("tags", [])

    if not title:
        return error_result("'title' is required")
    if not content:
        return error_result("'content' is required")

    result = _create_note(title=title, content=content, folder=folder, tags=tags)

    if result.get("error"):
        return error_result(result["error"])

    return text_result({
        "status": "created",
        "path": result["relative_path"],
        "folder": result["folder"],
        "title": result["title"],
    })


async def handle_search(args: dict) -> list[TextContent]:
    """Handle obsidian_search tool call."""
    query = str(args.get("query", "")).strip()
    max_results = args.get("max_results")

    if not query:
        return error_result("'query' is required")

    results = _search_vault(query=query, max_results=max_results)

    if results and results[0].get("error"):
        return error_result(results[0]["error"])

    return text_result({
        "query": query,
        "count": len(results),
        "results": results,
    })


async def handle_capture_link(args: dict) -> list[TextContent]:
    """Handle obsidian_capture_link tool call."""
    url = str(args.get("url", "")).strip()
    title = args.get("title")
    notes = str(args.get("notes", "")).strip()

    if not url:
        return error_result("'url' is required")

    result = _capture_link(url=url, title=title, notes=notes)

    if result.get("skipped"):
        return text_result({"status": "skipped", "reason": result["reason"]})

    if result.get("error"):
        return error_result(result["error"])

    return text_result({
        "status": "captured",
        "path": result["relative_path"],
        "folder": result["folder"],
        "title": result["title"],
    })


async def handle_get_preferences(args: dict) -> list[TextContent]:
    """Handle obsidian_get_preferences tool call."""
    vault_path = _get_vault_path()
    return text_result({
        "vault_configured": vault_path is not None,
        "vault_path": VAULT_PATH or "(not set)",
        "default_folder": DEFAULT_FOLDER,
        "link_folder": LINK_FOLDER,
        "auto_capture_links": AUTO_CAPTURE_LINKS,
        "default_tags": DEFAULT_TAGS,
        "max_search_results": MAX_SEARCH_RESULTS,
        "config_file": str(Path.home() / "lobster-config" / "obsidian.env"),
    })


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

async def main():
    """Start the Obsidian KM MCP server."""
    vault_status = "configured" if _get_vault_path() else "not configured"
    print(f"[INFO] Obsidian KM MCP Server starting...", file=sys.stderr)
    print(f"[INFO] Vault: {vault_status} ({VAULT_PATH or 'path not set'})", file=sys.stderr)
    print(f"[INFO] Default folder: {DEFAULT_FOLDER}", file=sys.stderr)
    print(f"[INFO] Link folder: {LINK_FOLDER}", file=sys.stderr)
    print(f"[INFO] Auto-capture links: {AUTO_CAPTURE_LINKS}", file=sys.stderr)
    print(f"[INFO] Default tags: {DEFAULT_TAGS or '(none)'}", file=sys.stderr)
    print(f"[INFO] Max search results: {MAX_SEARCH_RESULTS}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
