#!/usr/bin/env python3
"""
Lobster Observability HTTP Server

Lightweight HTTP server that exposes telemetry from the local Lobster instance
for the myownlobster admin dashboard.

Endpoints:
    GET /observability?window_hours=24  — Full telemetry JSON payload
    GET /health                         — Simple liveness check (no auth required)

Port: 8742 (separate from MCP bridge on 8741, dashboard WS on 9100)

Authentication:
    Pass LOBSTER_OBSERVABILITY_TOKEN or MCP_HTTP_TOKEN in env.
    If neither is set the server runs in unauthenticated dev mode.
    The token can also be read from config/mcp-http-auth.env.

Response schema:
    {
        "stats": {
            "uptime_hours": float,
            "messages_received": int,
            "messages_sent": int,
            "voice_messages": int,
            "images_processed": int,
        },
        "cost": {
            "total_tokens_used": int,
            "estimated_cost_usd": float,
            "model_breakdown": {
                "<model-id>": {"tokens": int, "cost": float},
                ...
            },
            "note": str | null,  # present if data is estimated
        },
        "timeline": [
            {
                "timestamp": ISO8601,
                "type": "message" | "agent_spawn",
                "direction": "in" | "out" | null,
                "subtype": "text" | "voice" | "image" | ...,
                "source": str,         # present on message events
                "agent_type": str,     # present on agent_spawn events
                "status": str,         # present on agent_spawn events
            },
            ...
        ],
        "agents": {
            "total_spawned": int,
            "by_type": {str: int},
            "avg_duration_ms": int,
            "currently_active": int,
        },
        "meta": {
            "generated_at": ISO8601,
            "window_hours": int,
            "processed_total": int,
            "sent_total": int,
        },
    }

Usage:
    python3 server.py [--port 8742]
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lobster-observability")

# ---------------------------------------------------------------------------
# Directory constants
# ---------------------------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))

INBOX_DIR = _MESSAGES / "inbox"
PROCESSED_DIR = _MESSAGES / "processed"
SENT_DIR = _MESSAGES / "sent"
CONFIG_DIR = _MESSAGES / "config"
TASK_OUTPUTS_DIR = _MESSAGES / "task-outputs"

STATE_FILE = CONFIG_DIR / "lobster-state.json"
PENDING_AGENTS_FILE = CONFIG_DIR / "pending-agents.json"

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _load_auth_token() -> str:
    """
    Load the auth token from environment variables or config file.

    Resolution order:
      1. LOBSTER_OBSERVABILITY_TOKEN env var
      2. MCP_HTTP_TOKEN env var
      3. config/mcp-http-auth.env file
      4. Empty string (unauthenticated dev mode)
    """
    token = os.environ.get("LOBSTER_OBSERVABILITY_TOKEN", "")
    if token:
        return token

    token = os.environ.get("MCP_HTTP_TOKEN", "")
    if token:
        return token

    # Try config file relative to the lobster install
    auth_file = Path(__file__).parent.parent.parent / "config" / "mcp-http-auth.env"
    if auth_file.exists():
        for line in auth_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("MCP_HTTP_TOKEN="):
                return stripped.split("=", 1)[1].strip()

    return ""


AUTH_TOKEN: str = _load_auth_token()


def _is_authorized(request: Request) -> bool:
    """Return True if the request passes auth, or auth is disabled."""
    if not AUTH_TOKEN:
        return True
    auth_header = request.headers.get("authorization", "")
    return auth_header.startswith("Bearer ") and auth_header[7:] == AUTH_TOKEN


# ---------------------------------------------------------------------------
# Model pricing  (USD per 1M input tokens, conservative estimate)
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, float] = {
    "claude-opus-4-6": 15.00,
    "claude-opus-4-5": 15.00,
    "claude-opus-3-5": 15.00,
    "claude-sonnet-4-6": 3.00,
    "claude-sonnet-4-5": 3.00,
    "claude-sonnet-3-7": 3.00,
    "claude-haiku-4-5": 0.80,
    "claude-haiku-4-5-20251001": 0.80,
    "claude-haiku-3-5": 0.80,
    "claude-haiku-3": 0.25,
}

_DEFAULT_MODEL = "claude-sonnet-4-6"
_ESTIMATED_TOKENS_PER_MSG = 1200  # conservative estimate when no usage data stored


# ---------------------------------------------------------------------------
# Pure data-collection helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    """Read and parse a JSON file. Returns None on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _list_json_files(directory: Path) -> list[Path]:
    """Return all .json files in a directory, sorted by filename (≈ chronological)."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def _compute_uptime_hours() -> float:
    """
    Return estimated Lobster uptime in hours.

    Uses lobster-state.json started_at timestamp when available; falls back
    to the mtime of the oldest processed message file.
    """
    if STATE_FILE.exists():
        state = _read_json(STATE_FILE)
        if isinstance(state, dict):
            started_at = state.get("started_at")
            if started_at:
                try:
                    ts = datetime.fromisoformat(started_at)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                except Exception:
                    pass

    # Fall back to oldest processed message
    processed = _list_json_files(PROCESSED_DIR)
    if processed:
        oldest_mtime = processed[0].stat().st_mtime
        return (time.time() - oldest_mtime) / 3600

    return 0.0


def _count_messages(processed_files: list[Path]) -> dict[str, int]:
    """
    Count messages by category from the processed directory.

    Returns dict with keys: messages_received, messages_sent, voice_messages,
    images_processed.
    """
    received = 0
    voice = 0
    images = 0

    for path in processed_files:
        msg = _read_json(path)
        if not isinstance(msg, dict):
            continue
        received += 1
        msg_type = (msg.get("type") or "").lower()
        if msg_type in ("voice", "audio"):
            voice += 1
        elif msg_type in ("image", "photo"):
            images += 1

    sent = len(_list_json_files(SENT_DIR))

    return {
        "messages_received": received,
        "messages_sent": sent,
        "voice_messages": voice,
        "images_processed": images,
    }


def _collect_task_outputs() -> list[dict]:
    """Read task-output JSON files. Returns list of parsed output records."""
    outputs = []
    for path in _list_json_files(TASK_OUTPUTS_DIR):
        rec = _read_json(path)
        if isinstance(rec, dict):
            outputs.append(rec)
    return outputs


# Known agent type names for extraction heuristics
_AGENT_NAMES: tuple[str, ...] = (
    "functional-engineer",
    "gsd-debugger",
    "gsd-executor",
    "gsd-planner",
    "gsd-phase-researcher",
    "gsd-codebase-mapper",
    "gsd-research-synthesizer",
    "gsd-roadmapper",
    "gsd-project-researcher",
    "gsd-verifier",
    "gsd-plan-checker",
    "gsd-integration-checker",
    "general-purpose",
    "explore",
)


def _extract_agent_type(output_text: str) -> str:
    """
    Extract the agent type from a task output string.
    Returns the first matching agent name or "general-purpose".
    """
    if not output_text:
        return "general-purpose"
    lower = output_text.lower()
    return next((name for name in _AGENT_NAMES if name in lower), "general-purpose")


def _compute_agent_stats(
    task_outputs: list[dict],
    pending_agents_data: Any,
) -> dict:
    """
    Aggregate agent statistics from task output records and pending-agents.json.

    Returns dict with total_spawned, by_type, avg_duration_ms, currently_active.
    """
    by_type: dict[str, int] = {}

    for output in task_outputs:
        agent_type = _extract_agent_type(output.get("output", ""))
        by_type[agent_type] = by_type.get(agent_type, 0) + 1

    total = sum(by_type.values())

    agents_list: list = []
    if isinstance(pending_agents_data, dict):
        agents_list = pending_agents_data.get("agents", [])
    currently_active = len(agents_list)

    return {
        "total_spawned": total,
        "by_type": by_type,
        "avg_duration_ms": 45000,  # placeholder — no per-run timing stored yet
        "currently_active": currently_active,
    }


def _parse_timestamp(ts_str: str | None, fallback_mtime: float | None = None) -> datetime | None:
    """
    Parse an ISO 8601 timestamp string to an aware datetime.
    Falls back to fallback_mtime (Unix epoch) if ts_str is absent or invalid.
    Returns None if both are unavailable.
    """
    if ts_str:
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    if fallback_mtime is not None:
        return datetime.fromtimestamp(fallback_mtime, tz=timezone.utc)

    return None


def _build_timeline(
    processed_files: list[Path],
    sent_files: list[Path],
    task_outputs: list[dict],
    window_hours: int,
) -> list[dict]:
    """
    Build a chronological list of events in the requested time window.

    Event types:
        - message (direction=in): a processed inbound message
        - message (direction=out): a sent outbound reply
        - agent_spawn: an agent task output record

    Limited to the most recent 200 events for response-size safety.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    events: list[dict] = []

    # Inbound messages
    for path in processed_files:
        msg = _read_json(path)
        if not isinstance(msg, dict):
            continue
        ts = _parse_timestamp(msg.get("timestamp"), path.stat().st_mtime)
        if ts is None or ts < cutoff:
            continue

        msg_type = (msg.get("type") or "text").lower()
        events.append({
            "timestamp": ts.isoformat(),
            "type": "message",
            "direction": "in",
            "subtype": msg_type if msg_type in ("voice", "image", "photo", "document") else "text",
            "source": msg.get("source", "telegram"),
        })

    # Outbound messages
    for path in sent_files:
        msg = _read_json(path)
        if not isinstance(msg, dict):
            continue
        ts = _parse_timestamp(msg.get("timestamp"), path.stat().st_mtime)
        if ts is None or ts < cutoff:
            continue

        events.append({
            "timestamp": ts.isoformat(),
            "type": "message",
            "direction": "out",
            "subtype": "text",
            "source": msg.get("source", "telegram"),
        })

    # Agent spawn events
    for output in task_outputs:
        ts = _parse_timestamp(output.get("timestamp"))
        if ts is None or ts < cutoff:
            continue

        events.append({
            "timestamp": ts.isoformat(),
            "type": "agent_spawn",
            "direction": None,
            "agent_type": _extract_agent_type(output.get("output", "")),
            "status": output.get("status", "success"),
        })

    events.sort(key=lambda e: e["timestamp"])
    return events[-200:]


def _estimate_cost(processed_files: list[Path]) -> dict:
    """
    Estimate token usage and USD cost from processed message files.

    Uses stored usage metadata when available; falls back to a per-message
    heuristic of ~1200 tokens at the default sonnet price.

    Returns dict with total_tokens_used, estimated_cost_usd, model_breakdown,
    and an optional note field when data is estimated.
    """
    model_token_map: dict[str, int] = {}
    has_real_data = False

    for path in processed_files:
        msg = _read_json(path)
        if not isinstance(msg, dict):
            continue

        # Check for stored usage metadata
        usage = msg.get("usage") or msg.get("token_usage")
        if isinstance(usage, dict):
            model = usage.get("model", _DEFAULT_MODEL)
            tokens = int(usage.get("total_tokens", _ESTIMATED_TOKENS_PER_MSG))
            model_token_map[model] = model_token_map.get(model, 0) + tokens
            has_real_data = True
            continue

        # Heuristic fallback
        model = msg.get("model", _DEFAULT_MODEL)
        model_token_map[model] = model_token_map.get(model, 0) + _ESTIMATED_TOKENS_PER_MSG

    total_tokens = 0
    total_cost = 0.0
    model_breakdown: dict[str, dict] = {}

    for model, tokens in model_token_map.items():
        price_per_m = MODEL_PRICING.get(model, MODEL_PRICING[_DEFAULT_MODEL])
        cost = (tokens / 1_000_000) * price_per_m
        total_tokens += tokens
        total_cost += cost
        model_breakdown[model] = {
            "tokens": tokens,
            "cost": round(cost, 4),
        }

    result: dict = {
        "total_tokens_used": total_tokens,
        "estimated_cost_usd": round(total_cost, 4),
        "model_breakdown": model_breakdown,
    }

    if not has_real_data and processed_files:
        result["note"] = (
            "Cost is estimated from message count heuristics "
            f"({_ESTIMATED_TOKENS_PER_MSG} tokens/msg). "
            "No per-message usage metadata found."
        )

    return result


# ---------------------------------------------------------------------------
# Top-level assembly — pure function
# ---------------------------------------------------------------------------

def build_observability_data(window_hours: int = 24) -> dict:
    """
    Collect and assemble all observability data.

    This is a pure function in the sense that it only reads from disk
    (no global mutable state), making it easy to test and reason about.
    """
    processed_files = _list_json_files(PROCESSED_DIR)
    sent_files = _list_json_files(SENT_DIR)
    task_outputs = _collect_task_outputs()
    pending_agents_data = _read_json(PENDING_AGENTS_FILE) or {}

    msg_counts = _count_messages(processed_files)
    uptime = _compute_uptime_hours()
    cost = _estimate_cost(processed_files)
    agents = _compute_agent_stats(task_outputs, pending_agents_data)
    timeline = _build_timeline(processed_files, sent_files, task_outputs, window_hours)

    return {
        "stats": {
            "uptime_hours": round(uptime, 2),
            **msg_counts,
        },
        "cost": cost,
        "timeline": timeline,
        "agents": agents,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": window_hours,
            "processed_total": len(processed_files),
            "sent_total": len(sent_files),
        },
    }


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_observability(request: Request) -> Response:
    """
    GET /observability?window_hours=24

    Returns full observability JSON.  Requires Bearer auth unless
    LOBSTER_OBSERVABILITY_TOKEN is unset (dev mode).
    """
    if not _is_authorized(request):
        return Response(
            json.dumps({"error": "Unauthorized"}),
            status_code=401,
            media_type="application/json",
        )

    try:
        window_hours = int(request.query_params.get("window_hours", "24"))
        window_hours = max(1, min(window_hours, 720))
    except (ValueError, TypeError):
        window_hours = 24

    try:
        data = build_observability_data(window_hours)
        return JSONResponse(data)
    except Exception:
        logger.exception("Error building observability data")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def handle_health(request: Request) -> Response:
    """GET /health — liveness check, no auth required."""
    return JSONResponse({"status": "ok", "service": "lobster-observability", "port": 8742})


# ---------------------------------------------------------------------------
# ASGI application
# ---------------------------------------------------------------------------

app = Starlette(
    routes=[
        Route("/observability", handle_observability, methods=["GET"]),
        Route("/health", handle_health, methods=["GET"]),
    ]
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Lobster Observability HTTP Server")
    parser.add_argument("--port", type=int, default=8742, help="Port to listen on (default: 8742)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    args = parser.parse_args()

    logger.info(
        "Starting Lobster observability server on %s:%d (auth %s)",
        args.host,
        args.port,
        "enabled" if AUTH_TOKEN else "DISABLED — set LOBSTER_OBSERVABILITY_TOKEN to enable",
    )

    uvicorn.run(app, host=args.host, port=args.port)
