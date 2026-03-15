#!/usr/bin/env python3
"""
Lobster Wire Protocol Server

Serves agent session data to the lobster-watcher frontend over:
  - GET /health       — Health check (no auth required)
  - GET /api/sessions — JSON snapshot (polling fallback)
  - GET /stream       — Server-Sent Events (SSE), real-time push

Wire protocol format (sent as SSE data lines):

  On connect, sends a full snapshot:
    data: {"type": "snapshot", "sessions": [...], "timestamp": "..."}

  Then streams diffs every LOBSTER_WIRE_POLL_INTERVAL seconds:
    data: {"type": "session_start",  "session": {...}, "timestamp": "..."}
    data: {"type": "session_update", "session": {...}, "timestamp": "..."}
    data: {"type": "session_end",    "session": {...}, "timestamp": "..."}

Session object shape matches the AgentSession TypeScript interface in types.ts.

PII note:
  If LOBSTER_WIRE_REDACT_PII=true, the fields description, input_summary, and
  result_summary are replaced with "[redacted]" before emitting. These fields
  are NEVER logged regardless of the redact setting.

Configuration (all via environment variables, all optional):
  LOBSTER_WIRE_PORT           8765         Server port
  LOBSTER_WIRE_POLL_INTERVAL  0.5          DB poll interval in seconds (fallback safety net)
  LOBSTER_WIRE_CORS_ORIGINS   *            Comma-separated allowed CORS origins
  LOBSTER_WIRE_AUTH_TOKEN     (unset)      Bearer token; if set, required on all
                                            non-health endpoints
  LOBSTER_WIRE_REDACT_PII     false        Strip PII fields from all events
  LOBSTER_WIRE_HISTORY_HOURS  24           Hours of completed session history to serve
  LOBSTER_DB_PATH             ~/messages/config/agent_sessions.db

Production checklist:
  - Set LOBSTER_WIRE_CORS_ORIGINS to specific dashboard origin (not "*")
  - Set LOBSTER_WIRE_AUTH_TOKEN to a strong random token
  - Put TLS termination (nginx/caddy) in front for HTTPS
  - Consider LOBSTER_WIRE_REDACT_PII=true for external logging pipelines
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Configuration (all via env vars — no hardcoded values)
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("LOBSTER_WIRE_PORT", "8765"))
POLL_INTERVAL = float(os.environ.get("LOBSTER_WIRE_POLL_INTERVAL", "0.5"))
CORS_ORIGINS_RAW = os.environ.get("LOBSTER_WIRE_CORS_ORIGINS", "*")
CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS_RAW.split(",") if o.strip()]
AUTH_TOKEN = os.environ.get("LOBSTER_WIRE_AUTH_TOKEN", "").strip()
REDACT_PII = os.environ.get("LOBSTER_WIRE_REDACT_PII", "false").lower() == "true"
HISTORY_HOURS = float(os.environ.get("LOBSTER_WIRE_HISTORY_HOURS", "24"))
DB_PATH = Path(
    os.environ.get(
        "LOBSTER_DB_PATH",
        str(Path.home() / "messages" / "config" / "agent_sessions.db"),
    )
).expanduser()

# PII fields that must never appear in logs and are redacted when REDACT_PII=true
_PII_FIELDS = frozenset({"description", "input_summary", "result_summary", "trigger_snippet"})

# ---------------------------------------------------------------------------
# Event-driven push: broadcast to all SSE generators via subscriber list
# ---------------------------------------------------------------------------

# Each active SSE generator subscribes its own asyncio.Event here.
# When /notify is called, all subscriber events are set simultaneously,
# waking every generator immediately — no race with _change_event.clear().
_sse_subscribers: list[asyncio.Event] = []
_sse_subscribers_lock = threading.Lock()


def _subscribe_sse() -> asyncio.Event:
    """Register a new SSE generator and return its dedicated wakeup event."""
    ev = asyncio.Event()
    with _sse_subscribers_lock:
        _sse_subscribers.append(ev)
    return ev


def _unsubscribe_sse(ev: asyncio.Event) -> None:
    """Unregister an SSE generator's wakeup event on disconnect."""
    with _sse_subscribers_lock:
        try:
            _sse_subscribers.remove(ev)
        except ValueError:
            pass


def _broadcast_change() -> None:
    """Set all subscriber events, waking every active SSE generator."""
    with _sse_subscribers_lock:
        for ev in _sse_subscribers:
            ev.set()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent DB connection (Issue 3: single connection shared across all polls)
# ---------------------------------------------------------------------------

_db_conn: sqlite3.Connection | None = None
_db_lock = threading.Lock()


def _get_db_conn(path: Path) -> sqlite3.Connection | None:
    """
    Return the module-level persistent read-only SQLite connection.
    Opens it on first call; returns None if the DB does not exist yet.
    Thread-safe via _db_lock.
    """
    global _db_conn
    if not path.exists():
        return None
    with _db_lock:
        if _db_conn is None:
            conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            _db_conn = conn
        return _db_conn


# ---------------------------------------------------------------------------
# SQLite helpers — standalone, no lobster-internal imports
# ---------------------------------------------------------------------------

def _query_sessions(path: Path) -> list[dict]:
    """
    Return active sessions plus sessions completed/failed within HISTORY_HOURS.
    Uses the persistent read-only connection so the wire server never writes to the DB.
    """
    conn = _get_db_conn(path)
    if conn is None:
        return []
    history_interval = f"-{HISTORY_HOURS} hours"
    try:
        with _db_lock:
            cursor = conn.execute(
                """
                SELECT
                    id, task_id, agent_type, description, chat_id, source,
                    status, output_file, timeout_minutes, input_summary,
                    result_summary, parent_id, spawned_at, completed_at,
                    last_seen_at,
                    trigger_message_id, reply_message_ids,
                    notified_at, trigger_snippet
                FROM agent_sessions
                WHERE
                    status = 'running'
                    OR (completed_at > datetime('now', ?))
                ORDER BY spawned_at ASC
                """,
                (history_interval,),
            )
            rows = cursor.fetchall()

        result = []
        for row in rows:
            d = dict(row)
            # elapsed_seconds removed (Issue 8: unused by frontend, wastes bandwidth)
            result.append(d)
        return result
    except sqlite3.OperationalError:
        # DB may not have been initialised yet — return empty list
        return []
    except Exception as exc:
        # Log exception type/message but NOT any field values (PII safety)
        logger.warning("DB query error: %s: %s", type(exc).__name__, exc)
        return []


# ---------------------------------------------------------------------------
# PII and auth helpers
# ---------------------------------------------------------------------------

def _redact(session: dict) -> dict:
    """Replace PII fields with '[redacted]' when REDACT_PII is enabled."""
    if not REDACT_PII:
        return session
    return {k: ("[redacted]" if k in _PII_FIELDS else v) for k, v in session.items()}


def _check_auth(request: Request) -> bool:
    """
    Return True if request is authorized (or auth not configured).
    Accepts auth via Authorization header OR ?token= query param (Issue 1: EventSource
    cannot send Authorization headers, so SSE clients pass the token as a query param).
    """
    if not AUTH_TOKEN:
        return True
    # Check Authorization header (used by polling fetch())
    auth_header = request.headers.get("Authorization", "")
    if auth_header == f"Bearer {AUTH_TOKEN}":
        return True
    # Check ?token= query param (used by EventSource which cannot set headers)
    token_param = request.query_params.get("token", "")
    return token_param == AUTH_TOKEN


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def _snapshot_sessions() -> list[dict]:
    """Return redacted session list for the wire payload."""
    return [_redact(s) for s in _query_sessions(DB_PATH)]


def _session_state_key(session: dict) -> tuple:
    """Fingerprint a session for change detection."""
    return (
        session.get("status"),
        session.get("completed_at"),
        session.get("last_seen_at"),
        session.get("result_summary") if not REDACT_PII else None,
    )


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

async def health_endpoint(request: Request) -> Response:
    """Health check — always returns 200, no auth required."""
    try:
        sessions = _query_sessions(DB_PATH)
        sessions_count = len(sessions)
    except Exception:
        sessions_count = -1
    return JSONResponse({"status": "ok", "sessions_count": sessions_count})


async def sessions_endpoint(request: Request) -> Response:
    """Polling fallback — returns full snapshot as JSON."""
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        sessions = _snapshot_sessions()
        return JSONResponse({"sessions": sessions, "timestamp": _now_iso()})
    except Exception as exc:
        logger.error("sessions_endpoint error: %s: %s", type(exc).__name__, exc)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def notify_endpoint(request: Request) -> Response:
    """Change notification endpoint — called by MCP server after every session write.

    Sets all subscriber events so every active SSE generator wakes up immediately
    instead of waiting for the next POLL_INTERVAL tick.
    No auth required: localhost-only, no sensitive data in request/response.
    """
    _broadcast_change()
    return JSONResponse({"ok": True})


async def sse_stream(request: Request) -> Response:
    """SSE stream endpoint — sends snapshot on connect, then diffs."""
    if not _check_auth(request):
        return Response("Unauthorized", status_code=401)

    async def event_generator():
        # Subscribe this generator to change broadcasts
        wakeup = _subscribe_sse()
        try:
            # --- Initial snapshot ---
            sessions = _snapshot_sessions()
            snapshot_payload = json.dumps(
                {"type": "snapshot", "sessions": sessions, "timestamp": _now_iso()},
                default=str,
            )
            yield f"data: {snapshot_payload}\n\n"

            # Build initial state map for change detection
            last_state: dict[str, tuple] = {s["id"]: _session_state_key(s) for s in sessions}
            # Track the full set of session IDs from the last poll (Issue 2: tombstones)
            last_ids: set[str] = set(last_state.keys())

            # --- Diff loop ---
            while True:
                # Check disconnect BEFORE waiting so we exit as fast as possible
                if await request.is_disconnected():
                    break

                # Event-driven wait: wake immediately on /notify broadcast, fall back to POLL_INTERVAL.
                # Each generator has its own wakeup event so _broadcast_change() wakes all of them
                # simultaneously — no race condition with shared event clearing.
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=POLL_INTERVAL)
                    wakeup.clear()
                except asyncio.TimeoutError:
                    pass  # POLL_INTERVAL elapsed without a notify — check DB anyway

                if await request.is_disconnected():
                    return  # generator return exits cleanly

                try:
                    current = _snapshot_sessions()
                except Exception:
                    continue

                current_map: dict[str, dict] = {s["id"]: s for s in current}
                current_state: dict[str, tuple] = {
                    sid: _session_state_key(s) for sid, s in current_map.items()
                }
                current_ids: set[str] = set(current_map.keys())
                ts = _now_iso()

                # Issue 2: emit synthetic session_end for sessions that disappeared from
                # the query window (they aged out of the HISTORY_HOURS completed_at window).
                vanished_ids = last_ids - current_ids
                for sid in vanished_ids:
                    # We only have the state key, not the full session object.
                    # Emit a minimal session_end event so the frontend can clean up.
                    ev = json.dumps(
                        {
                            "type": "session_end",
                            "session": {
                                "id": sid,
                                "status": "completed",
                                "completed_at": ts,
                            },
                            "timestamp": ts,
                        },
                        default=str,
                    )
                    yield f"data: {ev}\n\n"

                for sid, session in current_map.items():
                    prev = last_state.get(sid)
                    curr = current_state[sid]

                    if prev is None:
                        # New session appeared
                        ev = json.dumps(
                            {"type": "session_start", "session": session, "timestamp": ts},
                            default=str,
                        )
                        yield f"data: {ev}\n\n"
                    elif prev != curr:
                        status = session.get("status", "running")
                        if status in ("completed", "failed", "dead"):
                            event_type = "session_end"
                        else:
                            event_type = "session_update"
                        ev = json.dumps(
                            {"type": event_type, "session": session, "timestamp": ts},
                            default=str,
                        )
                        yield f"data: {ev}\n\n"

                last_state = current_state
                last_ids = current_ids

        finally:
            _unsubscribe_sse(wakeup)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

_routes = [
    Route("/health", health_endpoint, methods=["GET"]),
    Route("/api/sessions", sessions_endpoint, methods=["GET"]),
    Route("/stream", sse_stream, methods=["GET"]),
    Route("/notify", notify_endpoint, methods=["POST"]),
]

_starlette_app = Starlette(routes=_routes)

app = CORSMiddleware(
    _starlette_app,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization"],
)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting Lobster wire server on port %d "
        "(CORS: %s, auth: %s, redact_pii: %s, history: %gh, db: %s)",
        PORT,
        CORS_ORIGINS_RAW,
        "enabled" if AUTH_TOKEN else "disabled",
        REDACT_PII,
        HISTORY_HOURS,
        # Log DB path without expanding user home — avoids leaking system username
        os.environ.get("LOBSTER_DB_PATH", "~/messages/config/agent_sessions.db"),
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)
