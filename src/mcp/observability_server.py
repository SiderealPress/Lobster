#!/usr/bin/env python3
"""
Lobster Observability HTTP Server

Exposes a lightweight JSON endpoint at GET /observability that returns
deep telemetry from the local Lobster instance:
  - Stats: uptime, message counts, voice/image counts
  - Cost: token usage and estimated USD cost by model
  - Timeline: recent events (messages in/out, agent spawns)
  - Agents: spawn counts by type, avg duration

Usage:
    python observability_server.py [--port 8742]

Authentication:
    Bearer token via MCP_HTTP_TOKEN env var or config/mcp-http-auth.env
    Same token as the MCP HTTP bridge.

Port: 8742 (separate from MCP bridge on 8741)
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
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory constants — mirrors inbox_server.py
# ---------------------------------------------------------------------------
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
INBOX_DIR = _MESSAGES / "inbox"
OUTBOX_DIR = _MESSAGES / "outbox"
PROCESSED_DIR = _MESSAGES / "processed"
SENT_DIR = _MESSAGES / "sent"
CONFIG_DIR = _MESSAGES / "config"
TASK_OUTPUTS_DIR = _MESSAGES / "task-outputs"

# Lobster install start: use config/lobster-state.json if available, else
# fall back to the age of the oldest processed message.
STATE_FILE = CONFIG_DIR / "lobster-state.json"
PENDING_AGENTS_FILE = CONFIG_DIR / "pending-agents.json"  # Legacy fallback
SESSION_DB_PATH = CONFIG_DIR / "agent_sessions.db"

# ---------------------------------------------------------------------------
# Agent session store — SQLite-backed, optional (graceful fallback to JSON)
# ---------------------------------------------------------------------------
_LOBSTER_SRC = Path(os.environ.get("LOBSTER_SRC", Path.home() / "lobster")) / "src"
if str(_LOBSTER_SRC) not in sys.path:
    sys.path.insert(0, str(_LOBSTER_SRC))
try:
    from agents import session_store as _session_store
    _SESSION_STORE_AVAILABLE = True
except ImportError:
    _session_store = None  # type: ignore[assignment]
    _SESSION_STORE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_TOKEN = os.environ.get("MCP_HTTP_TOKEN", "")
if not AUTH_TOKEN:
    auth_file = Path(__file__).parent.parent.parent / "config" / "mcp-http-auth.env"
    if auth_file.exists():
        for line in auth_file.read_text().splitlines():
            if line.strip().startswith("MCP_HTTP_TOKEN="):
                AUTH_TOKEN = line.split("=", 1)[1].strip()
                break


# ---------------------------------------------------------------------------
# Model pricing (per 1M tokens, as of early 2026)
# Input token cost only for a conservative estimate.
# ---------------------------------------------------------------------------
MODEL_PRICING: dict[str, float] = {
    "claude-opus-4-6": 15.00,       # $15/1M input tokens
    "claude-opus-4-5": 15.00,
    "claude-opus-3-5": 15.00,
    "claude-sonnet-4-6": 3.00,      # $3/1M input tokens
    "claude-sonnet-4-5": 3.00,
    "claude-sonnet-3-7": 3.00,
    "claude-haiku-4-5": 0.80,       # $0.80/1M input tokens
    "claude-haiku-3-5": 0.80,
    "claude-haiku-3": 0.25,
}

# Model family aliases for display
def _model_family(model_id: str) -> str:
    """Map a model ID to a display family name."""
    model_lower = model_id.lower()
    if "opus" in model_lower:
        return "opus"
    if "sonnet" in model_lower:
        return "sonnet"
    if "haiku" in model_lower:
        return "haiku"
    return model_lower


# ---------------------------------------------------------------------------
# Data collection helpers — all pure functions returning dicts
# ---------------------------------------------------------------------------

def _read_json_file(path: Path) -> dict | list | None:
    """Read and parse a JSON file, returning None on failure."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _list_json_files(directory: Path) -> list[Path]:
    """Return all .json files in a directory, sorted by name (≈ chronological)."""
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def _compute_uptime_hours() -> float:
    """
    Estimate uptime in hours.
    Uses lobster-state.json if available, otherwise oldest processed message.
    """
    # Try state file first
    if STATE_FILE.exists():
        state = _read_json_file(STATE_FILE)
        if isinstance(state, dict):
            started_at = state.get("started_at")
            if started_at:
                try:
                    ts = datetime.fromisoformat(started_at)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    delta = datetime.now(timezone.utc) - ts
                    return delta.total_seconds() / 3600
                except Exception:
                    pass

    # Fall back: oldest processed message mtime
    processed_files = _list_json_files(PROCESSED_DIR)
    if processed_files:
        oldest_mtime = processed_files[0].stat().st_mtime
        delta = time.time() - oldest_mtime
        return delta / 3600

    return 0.0


def _count_messages() -> dict[str, int]:
    """
    Count messages by type from the processed, sent, and inbox directories.
    Returns: {received, sent, voice, images}
    """
    received = 0
    voice = 0
    images = 0

    for path in _list_json_files(PROCESSED_DIR):
        msg = _read_json_file(path)
        if not isinstance(msg, dict):
            continue
        received += 1
        msg_type = msg.get("type", "")
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
    """
    Read task-output JSON files to extract agent spawn records.
    Each task output has: job_name, timestamp, status, output
    """
    outputs = []
    for path in _list_json_files(TASK_OUTPUTS_DIR):
        rec = _read_json_file(path)
        if isinstance(rec, dict):
            outputs.append(rec)
    return outputs


def _parse_agent_type_from_output(output_text: str) -> str | None:
    """
    Heuristically extract agent type from task output text.
    Returns canonical agent type string or None.
    """
    if not output_text:
        return None
    text_lower = output_text.lower()
    # Order matters: check most specific first
    for agent_name in [
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
    ]:
        if agent_name in text_lower:
            return agent_name
    return "general-purpose"


def _compute_agent_stats(task_outputs: list[dict], pending_agents: dict) -> dict:
    """Compute agent usage statistics from session history and task outputs.

    Queries the SQLite session store when available (accurate history with
    agent_type, duration, and status). Falls back to task output heuristics
    and the legacy pending-agents dict when the store is not available.
    """
    by_type: dict[str, int] = {}
    total = 0
    currently_active = 0
    avg_duration_ms: int = 45000  # placeholder default

    if _SESSION_STORE_AVAILABLE and SESSION_DB_PATH.is_file():
        try:
            _session_store.init_db(SESSION_DB_PATH)
            active_sessions = _session_store.get_active_sessions(path=SESSION_DB_PATH)
            history = _session_store.get_session_history(limit=200, path=SESSION_DB_PATH)
            currently_active = len(active_sessions)

            # Aggregate by agent_type from full history
            durations_ms: list[int] = []
            for record in history:
                agent_type = record.get("agent_type") or _parse_agent_type_from_output(
                    record.get("result_summary") or ""
                )
                if agent_type:
                    by_type[agent_type] = by_type.get(agent_type, 0) + 1
                    total += 1

                # Compute duration if both timestamps present
                spawned_at = record.get("spawned_at")
                completed_at = record.get("completed_at")
                if spawned_at and completed_at:
                    try:
                        s = datetime.fromisoformat(spawned_at)
                        e = datetime.fromisoformat(completed_at)
                        if s.tzinfo is None:
                            s = s.replace(tzinfo=timezone.utc)
                        if e.tzinfo is None:
                            e = e.replace(tzinfo=timezone.utc)
                        ms = int((e - s).total_seconds() * 1000)
                        if ms > 0:
                            durations_ms.append(ms)
                    except (ValueError, TypeError):
                        pass

            if durations_ms:
                avg_duration_ms = sum(durations_ms) // len(durations_ms)

            return {
                "total_spawned": total,
                "by_type": by_type,
                "avg_duration_ms": avg_duration_ms,
                "currently_active": currently_active,
            }
        except Exception:
            pass  # Fall through to legacy path

    # Legacy path: heuristics from task outputs + pending-agents.json
    for output in task_outputs:
        agent_type = _parse_agent_type_from_output(output.get("output", ""))
        if agent_type:
            by_type[agent_type] = by_type.get(agent_type, 0) + 1
            total += 1

    agents_list = pending_agents.get("agents", []) if isinstance(pending_agents, dict) else []
    currently_active = len(agents_list)

    return {
        "total_spawned": total,
        "by_type": by_type,
        "avg_duration_ms": avg_duration_ms,
        "currently_active": currently_active,
    }


def _build_timeline(
    processed_files: list[Path],
    sent_files: list[Path],
    task_outputs: list[dict],
    window_hours: int = 24,
) -> list[dict]:
    """
    Build a chronological timeline of events for the requested window.
    Events: message_in, message_out, agent_spawn
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    events: list[dict] = []

    # Inbound messages
    for path in processed_files:
        msg = _read_json_file(path)
        if not isinstance(msg, dict):
            continue
        ts_str = msg.get("timestamp")
        if not ts_str:
            # Fall back to file mtime
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        else:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue

        if ts < cutoff:
            continue

        msg_type = msg.get("type", "message")
        events.append({
            "timestamp": ts.isoformat(),
            "type": "message",
            "direction": "in",
            "subtype": msg_type if msg_type in ("voice", "image", "photo", "document") else "text",
            "source": msg.get("source", "telegram"),
        })

    # Outbound messages
    for path in sent_files:
        msg = _read_json_file(path)
        if not isinstance(msg, dict):
            continue
        ts_str = msg.get("timestamp")
        if not ts_str:
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        else:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue

        if ts < cutoff:
            continue

        events.append({
            "timestamp": ts.isoformat(),
            "type": "message",
            "direction": "out",
            "subtype": "text",
            "source": msg.get("source", "telegram"),
        })

    # Agent spawns from task outputs
    for output in task_outputs:
        ts_str = output.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if ts < cutoff:
            continue

        agent_type = _parse_agent_type_from_output(output.get("output", ""))
        events.append({
            "timestamp": ts.isoformat(),
            "type": "agent_spawn",
            "direction": None,
            "agent_type": agent_type,
            "status": output.get("status", "success"),
        })

    # Sort chronologically
    events.sort(key=lambda e: e["timestamp"])

    # Limit to most recent 200 events for performance
    return events[-200:]


def _estimate_cost_from_messages(processed_files: list[Path]) -> dict:
    """
    Estimate token usage and cost from processed message files.

    Real usage would require actual API response metadata stored per-message.
    This implementation uses heuristics:
    - Average reply is ~800 tokens output, ~400 tokens input context
    - Model distribution follows typical Lobster usage patterns
    """
    # Count messages per model if model info is stored in message files
    model_token_map: dict[str, int] = {}

    for path in processed_files:
        msg = _read_json_file(path)
        if not isinstance(msg, dict):
            continue
        # Check if any model/token info is stored in the message
        usage = msg.get("usage") or msg.get("token_usage")
        if isinstance(usage, dict):
            model = usage.get("model", "claude-sonnet-4-6")
            tokens = usage.get("total_tokens", 1200)
            model_token_map[model] = model_token_map.get(model, 0) + tokens
            continue

        # No stored usage data — use heuristic
        # Default: assume sonnet handles most messages
        model = msg.get("model", "claude-sonnet-4-6")
        # Estimate ~1200 tokens per processed message (input + output)
        model_token_map[model] = model_token_map.get(model, 0) + 1200

    # Compute cost per model
    total_tokens = 0
    total_cost = 0.0
    model_breakdown: dict[str, dict] = {}

    for model, tokens in model_token_map.items():
        price_per_m = MODEL_PRICING.get(model, 3.00)
        cost = (tokens / 1_000_000) * price_per_m
        total_tokens += tokens
        total_cost += cost
        model_breakdown[model] = {
            "tokens": tokens,
            "cost": round(cost, 4),
        }

    return {
        "total_tokens_used": total_tokens,
        "estimated_cost_usd": round(total_cost, 4),
        "model_breakdown": model_breakdown,
    }


# ---------------------------------------------------------------------------
# Main data assembly
# ---------------------------------------------------------------------------

def _build_observability_data(window_hours: int = 24) -> dict:
    """
    Assemble all observability data. Pure function that reads from disk
    and returns a structured dict.
    """
    # Load all data sources
    processed_files = _list_json_files(PROCESSED_DIR)
    sent_files = _list_json_files(SENT_DIR)
    task_outputs = _collect_task_outputs()
    pending_agents = _read_json_file(PENDING_AGENTS_FILE) or {}

    # Compute each section
    msg_counts = _count_messages()
    uptime = _compute_uptime_hours()
    cost = _estimate_cost_from_messages(processed_files)
    agents = _compute_agent_stats(task_outputs, pending_agents)
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
# HTTP endpoints
# ---------------------------------------------------------------------------

async def observability_endpoint(request: Request) -> Response:
    """Return observability data as JSON. Requires Bearer auth."""
    # Auth check
    auth_header = request.headers.get("authorization", "")
    if AUTH_TOKEN and (
        not auth_header.startswith("Bearer ")
        or auth_header[7:] != AUTH_TOKEN
    ):
        return Response("Unauthorized", status_code=401)

    # Parse window_hours query param (default 24, max 720 = 30 days)
    try:
        window_hours = int(request.query_params.get("window_hours", "24"))
        window_hours = max(1, min(window_hours, 720))
    except (ValueError, TypeError):
        window_hours = 24

    try:
        data = _build_observability_data(window_hours)
        return JSONResponse(data)
    except Exception as exc:
        logger.exception("Error building observability data")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def health_endpoint(request: Request) -> Response:
    """Simple health check — no auth required."""
    return JSONResponse({"status": "ok", "service": "lobster-observability"})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Starlette(
    routes=[
        Route("/observability", observability_endpoint, methods=["GET"]),
        Route("/health", health_endpoint, methods=["GET"]),
    ]
)


if __name__ == "__main__":
    port = 8742
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    logger.info(
        "Starting Lobster observability server on port %d "
        "(auth %s)",
        port,
        "enabled" if AUTH_TOKEN else "DISABLED",
    )
    uvicorn.run(app, host="0.0.0.0", port=port)
