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
import json
import logging
import os
import stat
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
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
    "get_message_by_telegram_id",
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
    # Convenience tools (canonical memory readers)
    "get_priorities",
    "get_project_context",
    "get_daily_digest",
    "list_projects",
    "get_person_context",
    "list_people",
    # Utilities (read-only)
    "fetch_page",
    "transcribe_audio",
    # Skill reading
    "get_skill_context",
    "list_skills",
    "get_skill_preferences",
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


# ---------------------------------------------------------------------------
# Calendar and Gmail token push endpoints
# ---------------------------------------------------------------------------

_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_GCAL_TOKEN_DIR: Path = _MESSAGES_DIR / "config" / "gcal-tokens"
_GMAIL_TOKEN_DIR: Path = _MESSAGES_DIR / "config" / "gmail-tokens"
_TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR

_INTERNAL_SECRET: str = os.environ.get("LOBSTER_INTERNAL_SECRET", "").strip()


def _is_authorized_internal(request: Request) -> bool:
    """Return True if the request carries a valid LOBSTER_INTERNAL_SECRET."""
    if not _INTERNAL_SECRET:
        logger.error("LOBSTER_INTERNAL_SECRET not configured — push-calendar-token endpoint disabled")
        return False
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    return auth_header[7:].strip() == _INTERNAL_SECRET


async def push_calendar_token_endpoint(scope, receive, send):
    """POST /api/push-calendar-token — receive a token pushed by myownlobster.ai.

    Expected JSON body::

        {
          "chat_id":       "<telegram chat_id as string>",
          "access_token":  "<string>",
          "refresh_token": "<string>",
          "expires_at":    "<ISO 8601 UTC string>",
          "scope":         "<space-separated scopes>"
        }

    Authentication: ``Authorization: Bearer <LOBSTER_INTERNAL_SECRET>``

    Writes the token to ``~/messages/config/gcal-tokens/{chat_id}.json``
    with mode 0o600.
    """
    request = Request(scope, receive)

    if not _is_authorized_internal(request):
        response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        await response(scope, receive, send)
        return

    try:
        body = await request.json()
    except Exception:
        response = JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        await response(scope, receive, send)
        return

    chat_id = body.get("chat_id", "").strip()
    access_token = body.get("access_token", "").strip()
    refresh_token = body.get("refresh_token")
    expires_at_raw = body.get("expires_at", "").strip()
    scope_str = body.get("scope", "")

    if not chat_id or not access_token or not expires_at_raw:
        response = JSONResponse(
            {"error": "Missing required fields: chat_id, access_token, expires_at"},
            status_code=400,
        )
        await response(scope, receive, send)
        return

    # Sanitise chat_id to prevent path traversal
    safe_chat_id = "".join(c for c in chat_id if c.isalnum() or c in ("-", "_"))
    if not safe_chat_id:
        response = JSONResponse({"error": "Invalid chat_id"}, status_code=400)
        await response(scope, receive, send)
        return

    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except ValueError:
        response = JSONResponse(
            {"error": "Invalid expires_at: must be ISO 8601"},
            status_code=400,
        )
        await response(scope, receive, send)
        return

    token_data = {
        "access_token": access_token,
        "expires_at": expires_at.isoformat(),
        "scope": scope_str,
        "refresh_token": refresh_token,
    }

    try:
        _GCAL_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = _GCAL_TOKEN_DIR / f"{safe_chat_id}.json"
        tmp_path = token_path.with_suffix(".json.tmp")
        payload = json.dumps(token_data, indent=2)
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.rename(str(tmp_path), str(token_path))
        logger.info("Calendar token pushed and saved for chat_id=%r", safe_chat_id)
    except Exception as exc:
        logger.error("Failed to write calendar token for chat_id=%r: %s", safe_chat_id, exc)
        response = JSONResponse({"error": "Failed to write token"}, status_code=500)
        await response(scope, receive, send)
        return

    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


async def push_gmail_token_endpoint(scope, receive, send):
    """POST /api/push-gmail-token — receive a Gmail token pushed by myownlobster.ai.

    Expected JSON body::

        {
          "chat_id":       "<telegram chat_id as string>",
          "access_token":  "<string>",
          "refresh_token": "<string>",
          "expires_at":    "<ISO 8601 UTC string>",
          "scope":         "<space-separated scopes>"
        }

    Authentication: ``Authorization: Bearer <LOBSTER_INTERNAL_SECRET>``

    Writes the token to ``~/messages/config/gmail-tokens/{chat_id}.json``
    with mode 0o600.
    """
    request = Request(scope, receive)

    if not _is_authorized_internal(request):
        response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        await response(scope, receive, send)
        return

    try:
        body = await request.json()
    except Exception:
        response = JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        await response(scope, receive, send)
        return

    chat_id = body.get("chat_id", "").strip()
    access_token = body.get("access_token", "").strip()
    refresh_token = body.get("refresh_token")
    expires_at_raw = body.get("expires_at", "").strip()
    scope_str = body.get("scope", "")

    if not chat_id or not access_token or not expires_at_raw:
        response = JSONResponse(
            {"error": "Missing required fields: chat_id, access_token, expires_at"},
            status_code=400,
        )
        await response(scope, receive, send)
        return

    # Sanitise chat_id to prevent path traversal
    safe_chat_id = "".join(c for c in chat_id if c.isalnum() or c in ("-", "_"))
    if not safe_chat_id:
        response = JSONResponse({"error": "Invalid chat_id"}, status_code=400)
        await response(scope, receive, send)
        return

    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except ValueError:
        response = JSONResponse(
            {"error": "Invalid expires_at: must be ISO 8601"},
            status_code=400,
        )
        await response(scope, receive, send)
        return

    token_data = {
        "access_token": access_token,
        "expires_at": expires_at.isoformat(),
        "scope": scope_str,
        "refresh_token": refresh_token,
    }

    try:
        _GMAIL_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = _GMAIL_TOKEN_DIR / f"{safe_chat_id}.json"
        tmp_path = token_path.with_suffix(".json.tmp")
        payload = json.dumps(token_data, indent=2)
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.rename(str(tmp_path), str(token_path))
        logger.info("Gmail token pushed and saved for chat_id=%r", safe_chat_id)
    except Exception as exc:
        logger.error("Failed to write Gmail token for chat_id=%r: %s", safe_chat_id, exc)
        response = JSONResponse({"error": "Failed to write token"}, status_code=500)
        await response(scope, receive, send)
        return

    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


async def mcp_endpoint(scope, receive, send):
    """Handle all requests: auth check then delegate to MCP."""
    request = Request(scope, receive)
    path = request.url.path

    # Health endpoint — no auth required
    if path == "/health":
        await health_endpoint(scope, receive, send)
        return

    # Calendar token push — authenticated by LOBSTER_INTERNAL_SECRET
    if path == "/api/push-calendar-token":
        await push_calendar_token_endpoint(scope, receive, send)
        return

    # Gmail token push — authenticated by LOBSTER_INTERNAL_SECRET
    if path == "/api/push-gmail-token":
        await push_gmail_token_endpoint(scope, receive, send)
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
