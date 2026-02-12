#!/usr/bin/env python3
"""
Lobster Inbox MCP Server — HTTP Transport (Read-Only)

Exposes a READ-ONLY subset of the lobster-inbox MCP server over
Streamable HTTP so remote Claude Code instances can connect to it.

Write tools (send_reply, mark_processed, create_task, etc.) are
intentionally blocked. Remote clients can read context (tasks, memory,
conversation history) but cannot send messages on Lobster's behalf.

Usage:
    python inbox_server_http.py [--port 8741]

Environment:
    MCP_HTTP_TOKEN  — Bearer token for authentication (required)
                      Can also be set in config/mcp-http-auth.env

Remote Claude Code config (claude_desktop_config.json):
    {
      "mcpServers": {
        "lobster-inbox": {
          "type": "http",
          "url": "http://<your-vps-ip>:8741/mcp",
          "headers": {
            "Authorization": "Bearer <your-token>"
          }
        }
      }
    }
"""

import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

# Import the existing server's tool handlers
sys.path.insert(0, str(Path(__file__).parent))
from inbox_server import server as _full_server, list_tools as _full_list_tools, call_tool as _full_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Read-only tool allowlist
# ---------------------------------------------------------------------------
# Only these tools are exposed over the HTTP bridge. All other tools
# (especially write tools like send_reply, mark_processed, etc.) are blocked.
READONLY_TOOLS = frozenset({
    # Inbox reading
    "check_inbox",
    "wait_for_messages",
    "list_sources",
    "get_stats",
    "get_conversation_history",
    # Task reading
    "list_tasks",
    "get_task",
    # Scheduled job reading
    "check_task_outputs",
    "list_scheduled_jobs",
    "get_scheduled_job",
    # Memory reading
    "memory_search",
    "memory_recent",
    "get_handoff",
    # Brain dump reading
    "get_brain_dump_status",
    # Calendar reading
    "list_calendar_events",
    "check_availability",
    "get_week_schedule",
    # Self-update reading
    "check_updates",
    "get_upgrade_plan",
    # Utilities (read-only)
    "fetch_page",
    "transcribe_audio",
})

# ---------------------------------------------------------------------------
# Create a read-only MCP server that wraps the full server
# ---------------------------------------------------------------------------
readonly_server = Server("lobster-inbox-readonly")


@readonly_server.list_tools()
async def http_list_tools() -> list[Tool]:
    """Return only the read-only subset of tools."""
    all_tools = await _full_list_tools()
    filtered = [t for t in all_tools if t.name in READONLY_TOOLS]
    logger.info(
        "HTTP bridge exposing %d/%d tools (read-only)", len(filtered), len(all_tools)
    )
    return filtered


@readonly_server.call_tool()
async def http_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls, blocking any tool not in the allowlist."""
    if name not in READONLY_TOOLS:
        logger.warning("HTTP bridge BLOCKED write tool call: %s", name)
        return [
            TextContent(
                type="text",
                text=f"Error: tool '{name}' is not available over the HTTP bridge "
                     f"(write access is disabled for remote clients).",
            )
        ]
    return await _full_call_tool(name, arguments)


# Load auth token
AUTH_TOKEN = os.environ.get("MCP_HTTP_TOKEN", "")
if not AUTH_TOKEN:
    auth_file = Path(__file__).parent.parent.parent / "config" / "mcp-http-auth.env"
    if auth_file.exists():
        for line in auth_file.read_text().splitlines():
            if line.strip().startswith("MCP_HTTP_TOKEN="):
                AUTH_TOKEN = line.split("=", 1)[1].strip()
                break

if not AUTH_TOKEN:
    logger.error("No MCP_HTTP_TOKEN configured. Set env var or config/mcp-http-auth.env")
    sys.exit(1)

# Create session manager with the READ-ONLY server
session_manager = StreamableHTTPSessionManager(
    app=readonly_server,
    stateless=True,
)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    async with session_manager.run():
        logger.info("Lobster inbox HTTP MCP server started")
        yield
    logger.info("Lobster inbox HTTP MCP server stopped")


def _check_heartbeat(path, max_stale=600):
    """Check if a heartbeat file is fresh."""
    if not path.exists():
        return {"status": "unknown", "detail": "no heartbeat file"}
    age = time.time() - path.stat().st_mtime
    if age > max_stale:
        return {"status": "down", "detail": f"stale ({int(age)}s)", "age_seconds": int(age)}
    return {"status": "ok", "age_seconds": int(age)}


def _check_process(name):
    """Check if a process is running."""
    try:
        result = subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=5)
        return {"status": "ok"} if result.returncode == 0 else {"status": "down"}
    except Exception:
        return {"status": "unknown"}


async def health_endpoint(scope, receive, send):
    """Return health status of all VPS components."""
    home = Path.home()
    health = {
        "lobster_bot": _check_process("lobster_bot.py"),
        "http_bridge": {"status": "ok"},
    }
    all_ok = all(c.get("status") == "ok" for c in health.values())
    status_code = 200 if all_ok else 503
    response = JSONResponse({"healthy": all_ok, "components": health}, status_code=status_code)
    await response(scope, receive, send)


async def mcp_endpoint(scope, receive, send):
    """Handle all requests: auth check then delegate to MCP."""
    request = Request(scope, receive)
    path = request.url.path

    # Health endpoint — no auth required
    if path == "/health":
        await health_endpoint(scope, receive, send)
        return

    # Only handle /mcp
    if path != "/mcp":
        response = Response("Not Found", status_code=404)
        await response(scope, receive, send)
        return

    # Auth check
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AUTH_TOKEN:
        response = Response("Unauthorized", status_code=401)
        await response(scope, receive, send)
        return

    await session_manager.handle_request(scope, receive, send)


# Starlette app with lifespan only (routing handled in mcp_endpoint)
_inner_app = Starlette(lifespan=lifespan)


async def app(scope, receive, send):
    """ASGI entrypoint: lifecycle via Starlette, requests via mcp_endpoint."""
    if scope["type"] == "lifespan":
        await _inner_app(scope, receive, send)
    elif scope["type"] == "http":
        await mcp_endpoint(scope, receive, send)


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8741
    logger.info(f"Starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
