#!/usr/bin/env python3
"""
Camofox Browser MCP Server for Lobster

Wraps the camofox-browser REST API as MCP tools that Claude Code can use.
The camofox-browser server (Node.js) must be running separately on the configured port.

Tools provided:
- camofox_create_tab: Open a new browser tab
- camofox_snapshot: Get accessibility snapshot with element refs
- camofox_click: Click an element by ref or selector
- camofox_type: Type text into an element
- camofox_navigate: Navigate to URL or use search macro
- camofox_scroll: Scroll the page
- camofox_screenshot: Take a screenshot
- camofox_close_tab: Close a tab
- camofox_list_tabs: List open tabs
"""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent

# Configuration
CAMOFOX_PORT = int(os.environ.get("CAMOFOX_PORT", "9377"))
CAMOFOX_URL = os.environ.get("CAMOFOX_URL", f"http://localhost:{CAMOFOX_PORT}")
CAMOFOX_USER_ID = os.environ.get("CAMOFOX_USER_ID", "lobster")
CAMOFOX_SESSION_KEY = os.environ.get("CAMOFOX_SESSION_KEY", "default")

# HTTP client with reasonable timeouts
client = httpx.AsyncClient(base_url=CAMOFOX_URL, timeout=60.0)

server = Server("camofox-browser")


def text_result(data: Any) -> list[TextContent]:
    """Format a result as MCP text content."""
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def error_result(msg: str) -> list[TextContent]:
    """Format an error as MCP text content."""
    return [TextContent(type="text", text=f"Error: {msg}")]


async def check_server() -> bool:
    """Check if the camofox-browser server is reachable."""
    try:
        resp = await client.get("/health")
        return resp.status_code == 200
    except Exception:
        return False


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available camofox browser tools."""
    return [
        Tool(
            name="camofox_create_tab",
            description=(
                "Open a new browser tab using the Camoufox anti-detection browser. "
                "Use camofox tools instead of other browsers -- they bypass bot detection "
                "on Google, Amazon, LinkedIn, and more. Returns a tabId for subsequent operations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Initial URL to navigate to",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="camofox_snapshot",
            description=(
                "Get an accessibility snapshot of a Camoufox page with element refs "
                "(e1, e2, etc.) for interaction. These refs are stable identifiers you "
                "can use with camofox_click and camofox_type. ~90% smaller than raw HTML."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tabId": {
                        "type": "string",
                        "description": "Tab identifier from camofox_create_tab",
                    },
                },
                "required": ["tabId"],
            },
        ),
        Tool(
            name="camofox_click",
            description=(
                "Click an element in a Camoufox tab by ref (e.g., e1, e2) from the "
                "snapshot, or by CSS selector."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tabId": {
                        "type": "string",
                        "description": "Tab identifier",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Element ref from snapshot (e.g., e1, e2)",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector (alternative to ref)",
                    },
                },
                "required": ["tabId"],
            },
        ),
        Tool(
            name="camofox_type",
            description="Type text into an element in a Camoufox tab.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tabId": {
                        "type": "string",
                        "description": "Tab identifier",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Element ref from snapshot (e.g., e2)",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector (alternative to ref)",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type",
                    },
                    "pressEnter": {
                        "type": "boolean",
                        "description": "Press Enter after typing",
                        "default": False,
                    },
                },
                "required": ["tabId", "text"],
            },
        ),
        Tool(
            name="camofox_navigate",
            description=(
                "Navigate a Camoufox tab to a URL or use a search macro. "
                "Available macros: @google_search, @youtube_search, @amazon_search, "
                "@reddit_search, @wikipedia_search, @twitter_search, @yelp_search, "
                "@spotify_search, @netflix_search, @linkedin_search, @instagram_search, "
                "@tiktok_search, @twitch_search. Preferred over other browsers for "
                "sites with bot detection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tabId": {
                        "type": "string",
                        "description": "Tab identifier",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to",
                    },
                    "macro": {
                        "type": "string",
                        "description": "Search macro (e.g., @google_search)",
                        "enum": [
                            "@google_search",
                            "@youtube_search",
                            "@amazon_search",
                            "@reddit_search",
                            "@wikipedia_search",
                            "@twitter_search",
                            "@yelp_search",
                            "@spotify_search",
                            "@netflix_search",
                            "@linkedin_search",
                            "@instagram_search",
                            "@tiktok_search",
                            "@twitch_search",
                        ],
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (when using a macro)",
                    },
                },
                "required": ["tabId"],
            },
        ),
        Tool(
            name="camofox_scroll",
            description="Scroll a Camoufox page up, down, left, or right.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tabId": {
                        "type": "string",
                        "description": "Tab identifier",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": "Scroll direction",
                        "default": "down",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Pixels to scroll",
                        "default": 500,
                    },
                },
                "required": ["tabId", "direction"],
            },
        ),
        Tool(
            name="camofox_screenshot",
            description="Take a PNG screenshot of a Camoufox page. Returns the image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tabId": {
                        "type": "string",
                        "description": "Tab identifier",
                    },
                },
                "required": ["tabId"],
            },
        ),
        Tool(
            name="camofox_close_tab",
            description="Close a Camoufox browser tab.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tabId": {
                        "type": "string",
                        "description": "Tab identifier",
                    },
                },
                "required": ["tabId"],
            },
        ),
        Tool(
            name="camofox_list_tabs",
            description="List all open Camoufox browser tabs.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    """Handle tool calls by proxying to the camofox-browser REST API."""

    # Check server health on first call
    if not await check_server():
        return error_result(
            f"Camofox server not reachable at {CAMOFOX_URL}. "
            f"Start it with: cd ~/lobster/lobster-shop/camofox-browser/server && npm start"
        )

    try:
        if name == "camofox_create_tab":
            url = arguments.get("url", "about:blank")
            resp = await client.post(
                "/tabs",
                json={
                    "userId": CAMOFOX_USER_ID,
                    "sessionKey": CAMOFOX_SESSION_KEY,
                    "url": url,
                },
            )
            resp.raise_for_status()
            return text_result(resp.json())

        elif name == "camofox_snapshot":
            tab_id = arguments["tabId"]
            resp = await client.get(
                f"/tabs/{tab_id}/snapshot",
                params={"userId": CAMOFOX_USER_ID},
            )
            resp.raise_for_status()
            data = resp.json()
            # Format snapshot for readability
            snapshot_text = data.get("snapshot", "")
            url = data.get("url", "")
            refs_count = data.get("refsCount", 0)
            return text_result(
                f"URL: {url}\n"
                f"Interactive elements: {refs_count}\n\n"
                f"{snapshot_text}"
            )

        elif name == "camofox_click":
            tab_id = arguments["tabId"]
            body = {"userId": CAMOFOX_USER_ID}
            if "ref" in arguments:
                body["ref"] = arguments["ref"]
            if "selector" in arguments:
                body["selector"] = arguments["selector"]
            resp = await client.post(f"/tabs/{tab_id}/click", json=body)
            resp.raise_for_status()
            return text_result(resp.json())

        elif name == "camofox_type":
            tab_id = arguments["tabId"]
            body = {
                "userId": CAMOFOX_USER_ID,
                "text": arguments["text"],
            }
            if "ref" in arguments:
                body["ref"] = arguments["ref"]
            if "selector" in arguments:
                body["selector"] = arguments["selector"]
            if arguments.get("pressEnter"):
                body["pressEnter"] = True
            resp = await client.post(f"/tabs/{tab_id}/type", json=body)
            resp.raise_for_status()
            return text_result(resp.json())

        elif name == "camofox_navigate":
            tab_id = arguments["tabId"]
            body = {"userId": CAMOFOX_USER_ID}
            if "url" in arguments:
                body["url"] = arguments["url"]
            if "macro" in arguments:
                body["macro"] = arguments["macro"]
            if "query" in arguments:
                body["query"] = arguments["query"]
            resp = await client.post(f"/tabs/{tab_id}/navigate", json=body)
            resp.raise_for_status()
            return text_result(resp.json())

        elif name == "camofox_scroll":
            tab_id = arguments["tabId"]
            body = {
                "userId": CAMOFOX_USER_ID,
                "direction": arguments.get("direction", "down"),
                "amount": arguments.get("amount", 500),
            }
            resp = await client.post(f"/tabs/{tab_id}/scroll", json=body)
            resp.raise_for_status()
            return text_result(resp.json())

        elif name == "camofox_screenshot":
            tab_id = arguments["tabId"]
            resp = await client.get(
                f"/tabs/{tab_id}/screenshot",
                params={"userId": CAMOFOX_USER_ID},
            )
            resp.raise_for_status()
            # Save screenshot to temp file and return path (MCP-compatible)
            screenshot_dir = Path.home() / "messages" / "images"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            import time
            filename = f"camofox-screenshot-{int(time.time())}.png"
            filepath = screenshot_dir / filename
            filepath.write_bytes(resp.content)
            return [
                TextContent(
                    type="text",
                    text=f"Screenshot saved to {filepath}",
                ),
            ]

        elif name == "camofox_close_tab":
            tab_id = arguments["tabId"]
            resp = await client.request(
                "DELETE",
                f"/tabs/{tab_id}",
                json={"userId": CAMOFOX_USER_ID},
            )
            resp.raise_for_status()
            return text_result(resp.json())

        elif name == "camofox_list_tabs":
            resp = await client.get(
                "/tabs",
                params={"userId": CAMOFOX_USER_ID},
            )
            resp.raise_for_status()
            data = resp.json()
            tabs = data.get("tabs", [])
            if not tabs:
                return text_result("No open tabs.")
            lines = [f"Open tabs ({len(tabs)}):"]
            for tab in tabs:
                title = tab.get("title", "") or tab.get("url", "untitled")
                lines.append(f"  {tab.get('tabId', '?')} - {title}")
            return text_result("\n".join(lines))

        else:
            return error_result(f"Unknown tool: {name}")

    except httpx.HTTPStatusError as e:
        body = e.response.text
        return error_result(f"HTTP {e.response.status_code}: {body}")
    except httpx.ConnectError:
        return error_result(
            f"Cannot connect to camofox server at {CAMOFOX_URL}. "
            f"Start it with: cd ~/lobster/lobster-shop/camofox-browser/server && npm start"
        )
    except Exception as e:
        return error_result(f"{type(e).__name__}: {str(e)}")


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
