#!/usr/bin/env python3
"""
Lobster Context Bridge — Layer 1: Read-only memory and state API server

Exposes Lobster's internal state to remote clients via a Streamable HTTP
MCP server on port 8743. Remote clients (Claude Desktop, bisque, agents on
other machines) connect to this server as an MCP server and gain read access
to memory, tasks, projects, pending agents, and canonical documents.

This is distinct from:
  - Port 8741 (lobster-inbox-http): read-only clone of inbox MCP server
  - Port 8742 (lobster-observability): metrics/telemetry JSON endpoint

Usage:
    python server.py [--port 8743]

Environment:
    LOBSTER_CONTEXT_BRIDGE_TOKEN  — Bearer token for authentication (required)
                                    Falls back to MCP_HTTP_TOKEN if not set.
    LOBSTER_MESSAGES              — Path to messages directory (default: ~/messages)
    LOBSTER_WORKSPACE             — Path to workspace directory (default: ~/lobster-workspace)

Remote Claude Code config (claude_desktop_config.json):
    {
      "mcpServers": {
        "lobster-context-bridge": {
          "type": "http",
          "url": "http://<your-vps-ip>:8743/mcp",
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory constants — mirrors inbox_server.py layout
# ---------------------------------------------------------------------------
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))

TASKS_FILE = _MESSAGES / "tasks.json"
CONFIG_DIR = _MESSAGES / "config"
PENDING_AGENTS_FILE = CONFIG_DIR / "pending-agents.json"
CANONICAL_DIR = _WORKSPACE / "memory" / "canonical"

# ---------------------------------------------------------------------------
# Auth — prefer LOBSTER_CONTEXT_BRIDGE_TOKEN, fall back to MCP_HTTP_TOKEN,
# then check config file (same lookup chain as inbox_server_http.py).
# ---------------------------------------------------------------------------
_AUTH_TOKEN = (
    os.environ.get("LOBSTER_CONTEXT_BRIDGE_TOKEN", "")
    or os.environ.get("MCP_HTTP_TOKEN", "")
)
if not _AUTH_TOKEN:
    _auth_file = Path(__file__).parent.parent.parent / "config" / "mcp-http-auth.env"
    if _auth_file.exists():
        for _line in _auth_file.read_text().splitlines():
            if _line.strip().startswith("MCP_HTTP_TOKEN="):
                _AUTH_TOKEN = _line.split("=", 1)[1].strip()
                break

if not _AUTH_TOKEN:
    logger.error(
        "No auth token configured. Set LOBSTER_CONTEXT_BRIDGE_TOKEN or MCP_HTTP_TOKEN."
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Memory provider — optional, degrades gracefully to canonical-file-only mode
# ---------------------------------------------------------------------------
_memory_provider = None
try:
    # The memory module lives in src/mcp/memory/ — add that directory to the
    # path so we can import it from src/context_bridge/.
    _mcp_dir = Path(__file__).parent.parent / "mcp"
    sys.path.insert(0, str(_mcp_dir))
    from memory import create_memory_provider  # type: ignore[import]
    _memory_provider = create_memory_provider(use_vector=True)
    logger.info("Memory provider initialized: %s", type(_memory_provider).__name__)
except Exception as _mem_err:
    logger.warning("Memory system unavailable (%s); falling back to canonical files only.", _mem_err)

# ---------------------------------------------------------------------------
# Pure helpers — stateless reader functions with no side effects beyond I/O
# ---------------------------------------------------------------------------


def _read_canonical_file(relative_path: str, missing_message: str) -> str:
    """Read a file under CANONICAL_DIR or return a fallback message."""
    path = CANONICAL_DIR / relative_path
    if path.exists():
        return path.read_text()
    return missing_message


def _list_project_names() -> list[dict]:
    """List project markdown files under CANONICAL_DIR/projects/."""
    projects_dir = CANONICAL_DIR / "projects"
    if not projects_dir.exists():
        return []
    return [
        {"name": f.stem, "path": str(f)}
        for f in sorted(projects_dir.glob("*.md"))
    ]


def _get_project_context(project: str) -> str:
    """Read a project's canonical file, guarding against path traversal."""
    if "/" in project or "\\" in project or ".." in project:
        return "Error: invalid project name."
    path = CANONICAL_DIR / "projects" / f"{project}.md"
    if path.exists():
        return path.read_text()
    available = (
        [f.stem for f in (CANONICAL_DIR / "projects").glob("*.md")]
        if (CANONICAL_DIR / "projects").exists()
        else []
    )
    return f"No project file for '{project}'. Available: {', '.join(available) or 'none'}"


def _load_tasks(status_filter: str = "all") -> list[dict]:
    """Load tasks from tasks.json, optionally filtered by status."""
    if not TASKS_FILE.exists():
        return []
    try:
        data = json.loads(TASKS_FILE.read_text())
        tasks = data.get("tasks", [])
        if status_filter != "all":
            tasks = [t for t in tasks if t.get("status", "").lower() == status_filter]
        return tasks
    except Exception:
        return []


def _load_pending_agents() -> list[dict]:
    """Load the pending-agents.json file, returning an empty list on any error."""
    if not PENDING_AGENTS_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_AGENTS_FILE.read_text())
        # The file may be a list or a dict with an "agents" key
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("agents", list(data.values()))
        return []
    except Exception:
        return []


def _format_tasks(tasks: list[dict]) -> str:
    """Format a list of task dicts into a readable multi-group string."""
    if not tasks:
        return "No tasks found."

    pending = [t for t in tasks if t.get("status") == "pending"]
    in_progress = [t for t in tasks if t.get("status") == "in_progress"]
    completed = [t for t in tasks if t.get("status") == "completed"]

    lines = ["Tasks:"]
    if in_progress:
        lines.append("\nIn Progress:")
        lines.extend(f"  #{t['id']} {t['subject']}" for t in in_progress)
    if pending:
        lines.append("\nPending:")
        lines.extend(f"  #{t['id']} {t['subject']}" for t in pending)
    if completed:
        lines.append("\nCompleted:")
        lines.extend(f"  #{t['id']} {t['subject']}" for t in completed)
    lines.append(f"\n---\nTotal: {len(tasks)} task(s)")
    return "\n".join(lines)


def _format_memory_events(events: list, header: str) -> str:
    """Format a list of MemoryEvent objects into a readable string."""
    if not events:
        return f"No events found ({header})."
    lines = [f"{header} ({len(events)} events):"]
    for event in events:
        ts = event.timestamp.strftime("%Y-%m-%d %H:%M") if event.timestamp else "?"
        proj = f" [{event.project}]" if getattr(event, "project", None) else ""
        eid = f"#{event.id}" if getattr(event, "id", None) else ""
        preview = event.content[:200] + "..." if len(event.content) > 200 else event.content
        lines.append(f"- {eid} {ts} ({event.type}/{event.source}{proj}): {preview}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------
_TOOLS: list[Tool] = [
    Tool(
        name="get_memory",
        description=(
            "Search Lobster's memory using hybrid vector + keyword search. "
            "Returns the most relevant events matching the query."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Can be natural language.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default: 10.",
                    "default": 10,
                },
                "project": {
                    "type": "string",
                    "description": "Optional project filter.",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_recent_memory",
        description=(
            "Get recent events from Lobster's memory. "
            "Returns events from the last N hours, newest first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Number of hours to look back. Default: 24.",
                    "default": 24,
                },
                "project": {
                    "type": "string",
                    "description": "Optional project filter.",
                },
            },
        },
    ),
    Tool(
        name="get_priorities",
        description=(
            "Fetch Lobster's current priority stack. Returns the canonical priorities.md "
            "file, updated nightly by the consolidation process."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_projects",
        description=(
            "List all projects tracked in Lobster's canonical memory. "
            "Returns project names for use with get_project_context()."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_project_context",
        description=(
            "Fetch status and context for a specific project. Returns project status, "
            "recent decisions, pending items, and blockers from the canonical project file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project name (e.g., 'lobster', 'kissinger', 'transformers')",
                },
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="list_tasks",
        description=(
            "List all tasks tracked by the main Lobster instance. "
            "Tasks are shared across all Lobster sessions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: pending, in_progress, completed, or all (default).",
                    "default": "all",
                },
            },
        },
    ),
    Tool(
        name="get_pending_agents",
        description=(
            "Get the list of background agents currently in-flight on the main "
            "Lobster instance. Each entry shows what work is being done and for whom."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_handoff",
        description=(
            "Read the current handoff document — a complete briefing on Lobster's "
            "identity, architecture, current state, and pending items."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_daily_digest",
        description=(
            "Fetch the latest daily digest. Summarizes recent activity: key "
            "conversations, task progress, decisions made, and items needing follow-up."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
]


# ---------------------------------------------------------------------------
# Tool call dispatch — pure dispatch table, each handler is a function
# ---------------------------------------------------------------------------


async def _handle_get_memory(args: dict[str, Any]) -> str:
    if _memory_provider is None:
        return "Memory system is not available (vector DB not initialized on this instance)."
    query = args.get("query", "")
    if not query:
        return "Error: query is required."
    limit = int(args.get("limit", 10))
    project = args.get("project")
    try:
        events = _memory_provider.search(query, limit=limit, project=project)
        return _format_memory_events(events, f'Memory search: "{query}"')
    except Exception as e:
        logger.error("get_memory failed: %s", e, exc_info=True)
        return f"Error searching memory: {e}"


async def _handle_get_recent_memory(args: dict[str, Any]) -> str:
    if _memory_provider is None:
        return "Memory system is not available (vector DB not initialized on this instance)."
    hours = int(args.get("hours", 24))
    project = args.get("project")
    try:
        events = _memory_provider.recent(hours=hours, project=project)
        return _format_memory_events(events, f"Recent memory (last {hours}h)")
    except Exception as e:
        logger.error("get_recent_memory failed: %s", e, exc_info=True)
        return f"Error getting recent events: {e}"


async def _handle_get_priorities(_args: dict[str, Any]) -> str:
    return _read_canonical_file(
        "priorities.md",
        "No priorities file found. Nightly consolidation has not run yet.",
    )


async def _handle_list_projects(_args: dict[str, Any]) -> str:
    projects = _list_project_names()
    if not projects:
        return "No project files found in canonical memory."
    return json.dumps(projects, indent=2)


async def _handle_get_project_context(args: dict[str, Any]) -> str:
    project = args.get("project", "").strip()
    if not project:
        return "Error: project name is required."
    return _get_project_context(project)


async def _handle_list_tasks(args: dict[str, Any]) -> str:
    status_filter = args.get("status", "all").lower()
    tasks = _load_tasks(status_filter)
    return _format_tasks(tasks)


async def _handle_get_pending_agents(_args: dict[str, Any]) -> str:
    agents = _load_pending_agents()
    if not agents:
        return "No background agents currently in-flight."
    lines = [f"Pending agents ({len(agents)}):"]
    for agent in agents:
        agent_id = agent.get("agent_id", agent.get("id", "?"))
        description = agent.get("description", "no description")
        started = agent.get("started_at", agent.get("created_at", ""))
        started_str = f" (started: {started})" if started else ""
        lines.append(f"  - [{agent_id}] {description}{started_str}")
    return "\n".join(lines)


async def _handle_get_handoff(_args: dict[str, Any]) -> str:
    return _read_canonical_file(
        "handoff.md",
        "No handoff document found. Nightly consolidation may not have run yet.",
    )


async def _handle_get_daily_digest(_args: dict[str, Any]) -> str:
    return _read_canonical_file(
        "daily-digest.md",
        "No daily digest found. Nightly consolidation may not have run yet.",
    )


# Dispatch table: tool name -> handler function
_DISPATCH: dict[str, Any] = {
    "get_memory": _handle_get_memory,
    "get_recent_memory": _handle_get_recent_memory,
    "get_priorities": _handle_get_priorities,
    "list_projects": _handle_list_projects,
    "get_project_context": _handle_get_project_context,
    "list_tasks": _handle_list_tasks,
    "get_pending_agents": _handle_get_pending_agents,
    "get_handoff": _handle_get_handoff,
    "get_daily_digest": _handle_get_daily_digest,
}

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
_bridge_server = Server("lobster-context-bridge")


@_bridge_server.list_tools()
async def _list_tools() -> list[Tool]:
    return _TOOLS


@_bridge_server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    start = time.time()
    try:
        result = await handler(arguments)
        elapsed_ms = int((time.time() - start) * 1000)
        logger.info("context-bridge tool %s OK (%dms)", name, elapsed_ms)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        logger.error("context-bridge tool %s failed (%dms): %s", name, elapsed_ms, e, exc_info=True)
        return [TextContent(type="text", text=f"Error in {name}: {e}")]


# ---------------------------------------------------------------------------
# Streamable HTTP transport
# ---------------------------------------------------------------------------
_session_manager = StreamableHTTPSessionManager(
    app=_bridge_server,
    stateless=True,
)


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    async with _session_manager.run():
        logger.info("Lobster Context Bridge started (port configured at launch)")
        yield
    logger.info("Lobster Context Bridge stopped")


def _check_auth(request: Request) -> bool:
    """Return True if the request carries the correct Bearer token."""
    auth_header = request.headers.get("authorization", "")
    return auth_header.startswith("Bearer ") and auth_header[7:] == _AUTH_TOKEN


async def _health_endpoint(scope: Any, receive: Any, send: Any) -> None:
    """Health check — no auth required."""
    response = JSONResponse({
        "healthy": True,
        "service": "lobster-context-bridge",
        "layer": 1,
        "tools": len(_TOOLS),
        "memory_available": _memory_provider is not None,
    })
    await response(scope, receive, send)


async def _mcp_endpoint(scope: Any, receive: Any, send: Any) -> None:
    """Auth check, then delegate to MCP session manager."""
    request = Request(scope, receive)
    path = request.url.path

    if path == "/health":
        await _health_endpoint(scope, receive, send)
        return

    if path != "/mcp":
        response = Response("Not Found", status_code=404)
        await response(scope, receive, send)
        return

    if not _check_auth(request):
        response = Response("Unauthorized", status_code=401)
        await response(scope, receive, send)
        return

    await _session_manager.handle_request(scope, receive, send)


# Inner Starlette app handles the lifespan (session manager startup/shutdown).
# The outer ASGI callable routes HTTP requests through _mcp_endpoint.
_inner_app = Starlette(lifespan=_lifespan)


async def app(scope: Any, receive: Any, send: Any) -> None:
    """ASGI entrypoint: lifecycle via Starlette, requests via _mcp_endpoint."""
    if scope["type"] == "lifespan":
        await _inner_app(scope, receive, send)
    elif scope["type"] == "http":
        await _mcp_endpoint(scope, receive, send)


if __name__ == "__main__":
    port = 8743
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    logger.info("Starting Lobster Context Bridge on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
