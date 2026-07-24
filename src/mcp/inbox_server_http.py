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
import hashlib
import hmac
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
from typing import Any, Callable, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

# Import the existing server's tool handlers.
# Set a flag BEFORE importing so inbox_server knows it is being imported as a
# library by the HTTP bridge rather than launched as the live dispatcher.  This
# prevents _reset_state_on_startup() from overwriting the hibernate state file
# every time the HTTP service restarts (see RCA for crash-loop fix).
os.environ.setdefault("LOBSTER_MCP_HTTP_IMPORT", "1")
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
_WORKSPACE_TOKEN_DIR: Path = _MESSAGES_DIR / "config" / "workspace-tokens"
_TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR

_INTERNAL_SECRET: str = os.environ.get("LOBSTER_INTERNAL_SECRET", "").strip()

# LOBSTER_IMPORT_TOKEN is used by the intake webhook endpoint.
# In the Google Apps Script that sends intake data, this is stored as LOBSTER_SECRET.
_IMPORT_TOKEN: str = os.environ.get("LOBSTER_IMPORT_TOKEN", os.environ.get("AWP_IMPORT_TOKEN", "")).strip()

_INBOX_DIR: Path = _MESSAGES_DIR / "inbox"

# ---------------------------------------------------------------------------
# BIS-727 Slice 1 — per-transaction HMAC signing, calendar push path only
# ---------------------------------------------------------------------------
# push_calendar_token_endpoint additionally verifies a signed, single-use,
# 30-minute-TTL session token (HMAC-SHA256 over
# instance_id|chat_id|scope|nonce|exp) bound to the specific consent
# transaction, alongside the existing static-secret bearer check above. This
# closes BIS-728's forged-push vulnerability: an attacker holding only the
# leaked LOBSTER_INTERNAL_SECRET (the bearer token) cannot forge a valid
# signature without also knowing this SECOND, independent secret, which is
# shared only with myownlobster.ai's callback route (never with any client
# that merely holds the bearer secret).
#
# _INSTANCE_URL, if configured, additionally binds the session to THIS VPS
# instance specifically (defense in depth beyond this single-tenant
# deployment's immediate needs).
_INSTANCE_URL: str = os.environ.get("LOBSTER_INSTANCE_URL", "").strip()
_SESSION_HMAC_SECRET: str = os.environ.get("CONSENT_SESSION_HMAC_SECRET", "").strip()
_SESSION_TOKEN_TTL_SECONDS: int = 30 * 60  # 30 minutes, per BIS-729

# --- Warn-then-enforce rollout (BIS-729) ------------------------------------
# Ships with enforce=False (warn-only) by default: this VPS-side change and
# myownlobster.ai's callback-route change deploy independently, so a VPS that
# hard-rejects on day one (before the broker side is emitting signed
# sessions) would break every calendar consent flow. Rollout sequence for
# whoever flips this in production:
#   1. Deploy this VPS change first. It runs in warn mode: pushes missing or
#      carrying an invalid signed session are still ACCEPTED, but each one
#      logs a warning (see _verify_calendar_session_token call site below).
#   2. Deploy myownlobster.ai's matching change (calendar callback starts
#      attaching signed sessions to every push).
#   3. Watch the logs for ~48h. Once no more "missing/invalid signed
#      session" warnings appear for legitimate traffic, set
#      CALENDAR_PUSH_SIGNED_SESSION_ENFORCE=true (env var) and restart the
#      HTTP bridge (`~/lobster/scripts/restart-mcp.sh`) to start hard-
#      rejecting (401) any calendar push lacking a valid signed session.
_ENFORCE_SIGNED_SESSION: bool = os.environ.get(
    "CALENDAR_PUSH_SIGNED_SESSION_ENFORCE", "false"
).strip().lower() in ("1", "true", "yes")

# Single-use enforcement for the session token's nonce, at THIS process's
# scope. markConsumed() on the myownlobster.ai side already guarantees a
# given ConsentToken's nonce is fetched at most once; this in-memory set is
# additional defense against replaying a captured signed session (e.g. from
# a log line or MITM'd request) more than once within its 30-minute TTL.
# Process-lifetime only (not persisted across restarts) — acceptable given
# this bridge runs as a single uvicorn worker and the window is short.
_seen_calendar_session_nonces: dict[str, float] = {}


def _consume_calendar_session_nonce(nonce: str, exp: float) -> bool:
    """Atomically-within-this-process claim a nonce. False if already seen."""
    now = time.time()
    expired = [n for n, e in _seen_calendar_session_nonces.items() if e < now]
    for n in expired:
        del _seen_calendar_session_nonces[n]
    if nonce in _seen_calendar_session_nonces:
        return False
    _seen_calendar_session_nonces[nonce] = exp
    return True


def _verify_calendar_session_token(body: dict, chat_id: str, scope_str: str) -> tuple[bool, str]:
    """Verify the calendar push's signed, single-use, 30-min-TTL session token.

    Returns ``(ok, reason)``. ``reason`` is a short machine-readable string
    for logging only — it never includes secret material or token values.
    """
    session = body.get("session_token")
    if not isinstance(session, dict):
        return False, "missing_session_token"

    if not _SESSION_HMAC_SECRET:
        return False, "hmac_secret_not_configured"

    instance_id = session.get("instance_id")
    session_chat_id = session.get("chat_id")
    session_scope = session.get("scope")
    nonce = session.get("nonce")
    sig = session.get("sig")
    exp = session.get("exp")

    if not (
        isinstance(instance_id, str)
        and instance_id
        and isinstance(session_chat_id, str)
        and session_chat_id
        and isinstance(session_scope, str)
        and session_scope
        and isinstance(nonce, str)
        and nonce
        and isinstance(sig, str)
        and sig
        and isinstance(exp, (int, float))
        and not isinstance(exp, bool)
    ):
        return False, "malformed_session_token"

    # Bind the session to THIS transaction: the signed chat_id/scope must
    # match the request body's chat_id/scope exactly (an attacker cannot
    # reuse a session issued for one chat_id/scope on another).
    if session_chat_id != chat_id:
        return False, "chat_id_mismatch"
    if session_scope != scope_str:
        return False, "scope_mismatch"
    if _INSTANCE_URL and instance_id != _INSTANCE_URL:
        return False, "instance_id_mismatch"

    message = f"{instance_id}|{session_chat_id}|{session_scope}|{nonce}|{int(exp)}"
    expected_sig = hmac.new(
        _SESSION_HMAC_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return False, "bad_signature"

    if exp < time.time():
        return False, "expired"

    if not _consume_calendar_session_nonce(nonce, float(exp)):
        return False, "nonce_already_used"

    return True, "ok"


def _is_authorized_internal(request: Request) -> bool:
    """Return True if the request carries a valid LOBSTER_INTERNAL_SECRET."""
    if not _INTERNAL_SECRET:
        logger.error("LOBSTER_INTERNAL_SECRET not configured — push-calendar-token endpoint disabled")
        return False
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    return auth_header[7:].strip() == _INTERNAL_SECRET


def _is_authorized_intake(request: Request) -> bool:
    """Return True if the request carries a valid LOBSTER_IMPORT_TOKEN.

    The Apps Script on the intake side stores this token as LOBSTER_SECRET and
    sends it as ``Authorization: Bearer <LOBSTER_IMPORT_TOKEN>``.
    """
    if not _IMPORT_TOKEN:
        logger.error("LOBSTER_IMPORT_TOKEN not configured — intake endpoint disabled")
        return False
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    return auth_header[7:].strip() == _IMPORT_TOKEN


# ---------------------------------------------------------------------------
# BIS-744 — shared post-push confirmation helper (calendar, gmail, workspace)
# ---------------------------------------------------------------------------
# BIS-743 copy-pasted an identical confirmation-on-push block into all three
# endpoints (deliberately, per this file's established "prove it 3x before
# abstracting" convention — see the HMAC-signing blocks above). Now that the
# shape has been hand-written three times, this extracts the shared logic,
# and upgrades it with three things none of the three copies had:
#
#   1. A live-data preview point (next calendar event / latest email subject
#      / most recent Drive file) instead of static "token saved" text, via
#      the ``fetch_preview`` callable each call site supplies.
#   2. Failure visibility: the pre-existing workspace confirmation block had
#      a bare ``except Exception: log.warning(...)`` with ZERO user-visible
#      fallback -- if the confirmation failed, the user was left in total
#      silence with no way to know anything went wrong. This version (a)
#      logs at ERROR (not WARNING) with exc_info, so it can't be missed in
#      logs/alerting, and (b) if the live-data fetch fails, still queues an
#      honest degraded confirmation ("connected, but couldn't fetch a
#      preview") rather than skipping the confirmation entirely. If the
#      notify step itself (the outbox write) fails, per-user delivery is
#      genuinely impossible via that channel -- but this is never silently
#      swallowed: a system-level alert is written to the inbox (chat_id=0)
#      as a fallback so a human (or the dispatcher) still finds out.
#   3. A de-dupe guard so a double-clicked consent link or a duplicate
#      webhook delivery from myownlobster.ai doesn't send the user two
#      confirmations for one grant.
#
# Uses ``_MESSAGES_DIR`` (module-level, already configurable via the
# LOBSTER_MESSAGES env var and already used for the token directories above)
# instead of a fresh ``Path(os.path.expanduser("~/messages"))`` call. The
# three BIS-743 copies each independently re-resolved "~/messages" via
# os.path.expanduser, which is NOT overridden by LOBSTER_MESSAGES and is not
# patched by any test fixture other than the ones that explicitly mock
# ``os.path.expanduser`` -- this caused every pre-existing push-endpoint
# characterization test (which never needed to touch the outbox before
# BIS-743 added a confirmation step) to silently write real files into this
# machine's real ``~/messages/outbox`` on every test run. Referencing
# ``_MESSAGES_DIR`` here instead means the existing, single, already-tested
# patch point works for the confirmation path too.
_CONFIRM_DEDUPE_TTL_SECONDS: float = 300.0  # 5 minutes

# Process-lifetime only (not persisted across restarts), same tradeoff as the
# nonce-replay sets above -- acceptable given this bridge runs as a single
# uvicorn worker and the window is short.
_seen_push_confirmations: dict[str, float] = {}


def _purge_expired_confirmations(now: float) -> None:
    expired = [k for k, exp in _seen_push_confirmations.items() if exp < now]
    for k in expired:
        del _seen_push_confirmations[k]


def _confirmation_already_sent(chat_id: str, scope: str) -> bool:
    """De-dupe guard (read-only check): True if a confirmation was already
    *successfully delivered* for this chat_id+scope within the last
    ``_CONFIRM_DEDUPE_TTL_SECONDS``.

    Deliberately does NOT claim the slot as a side effect -- claiming happens
    only in ``_mark_confirmation_sent``, and only after the outbox write has
    actually succeeded. Claiming eagerly (before knowing whether delivery
    succeeded) would mean a transient failure -- exactly the case this
    module tries hardest to make visible and recoverable -- permanently
    blocks any legitimate retry (e.g. myownlobster.ai retrying a webhook
    delivery) for the rest of the TTL window, silently defeating the whole
    point of the failure-visibility work in this file.
    """
    now = time.time()
    _purge_expired_confirmations(now)
    return f"{scope}:{chat_id}" in _seen_push_confirmations


def _mark_confirmation_sent(chat_id: str, scope: str) -> None:
    """Claim the (chat_id, scope) de-dupe slot for the TTL window.

    Call this ONLY after the confirmation has been successfully written to
    the outbox -- see the docstring on ``_confirmation_already_sent``.
    """
    now = time.time()
    _purge_expired_confirmations(now)
    key = f"{scope}:{chat_id}"
    _seen_push_confirmations[key] = now + _CONFIRM_DEDUPE_TTL_SECONDS


def _write_system_alert(text: str) -> None:
    """Best-effort system-level alert for when a per-user confirmation
    cannot be delivered at all (the outbox write itself failed).

    An earlier version of this fallback wrote a ``chat_id=0`` /
    ``type="system_alert"`` message to the inbox. An independent (Fable)
    review pass before merge pointed out that nothing in this codebase
    actually consumes that combination -- ``inbox_server.py`` explicitly
    excludes ``chat_id == 0`` from ``USER_FACING_TYPES`` handling, so that
    message would have sat in the inbox, unread by anyone, making the
    "fallback" cosmetic rather than real.

    Fixed: route the alert through the SAME outbox mechanism already proven
    to deliver real Telegram messages (the one this whole file's
    confirmations use), targeted at ``LOBSTER_ADMIN_CHAT_ID`` -- the
    established env var already used by other alerting code in this repo
    (e.g. ``src/transcription/worker.py::notify_dispatcher_dead_letter``).
    A secondary copy is still written to the inbox for the audit trail, but
    the outbox message is what actually reaches a human.

    Never raises further: if even this fails, the ERROR log call at the
    call site (in ``_queue_push_confirmation``) is the final line of
    defense.
    """
    admin_chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "").strip()

    if admin_chat_id:
        try:
            outbox_dir = _MESSAGES_DIR / "outbox"
            outbox_dir.mkdir(parents=True, exist_ok=True)
            alert_id = f"{int(time.time() * 1000)}_confirm_failure_admin_alert"
            alert_reply = {
                "id": alert_id,
                "source": "telegram",
                "chat_id": admin_chat_id,
                "text": f"[system alert] {text}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            alert_path = outbox_dir / f"{alert_id}.json"
            tmp_path = alert_path.with_suffix(".json.tmp")
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(alert_reply, indent=2))
            os.rename(str(tmp_path), str(alert_path))
        except Exception:  # noqa: BLE001
            logger.error(
                "push-token confirmation: admin-outbox alert ALSO failed "
                "-- this failure is now visible only in this log line: %s",
                text,
                exc_info=True,
            )
    else:
        logger.error(
            "push-token confirmation: LOBSTER_ADMIN_CHAT_ID not configured "
            "-- cannot deliver system alert to any human. Failure is only "
            "visible in this log line: %s",
            text,
        )

    # Secondary record in the inbox (chat_id=0, type="system_alert") for
    # audit/history purposes. Not relied upon for actual human notification
    # (see docstring above) -- best-effort, failure here is non-fatal.
    try:
        _INBOX_DIR.mkdir(parents=True, exist_ok=True)
        alert_id = f"{int(time.time() * 1000)}_confirm_failure_alert"
        alert_data = {
            "id": alert_id,
            "type": "system_alert",
            "source": "system",
            "chat_id": 0,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        alert_path = _INBOX_DIR / f"{alert_id}.json"
        tmp_path = alert_path.with_suffix(".json.tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(alert_data, indent=2))
        os.rename(str(tmp_path), str(alert_path))
    except Exception:  # noqa: BLE001
        logger.error(
            "push-token confirmation: inbox audit-record write ALSO failed: %s",
            text,
            exc_info=True,
        )


def _queue_push_confirmation(
    *,
    chat_id: str,
    scope: str,
    connected_text: str,
    fetch_preview: Callable[[], Optional[str]],
) -> None:
    """Queue a post-push confirmation to the outbox. Never raises.

    Args:
        chat_id:        Telegram chat_id (already sanitised by the caller).
        scope:           One of "calendar", "gmail", "workspace" -- used for
                         the de-dupe key and the outbox filename suffix.
        connected_text:  Static "X connected" lead-in text for this scope.
        fetch_preview:   Zero-arg callable returning a short live-data
                         preview line (or None/"" for "nothing to show"), or
                         raising on failure. Called synchronously; any
                         exception is caught here, never propagated.
    """
    if _confirmation_already_sent(chat_id, scope):
        logger.info(
            "Skipping duplicate push confirmation for scope=%r chat_id=%r "
            "(one was already queued within the last %.0fs)",
            scope, chat_id, _CONFIRM_DEDUPE_TTL_SECONDS,
        )
        return

    try:
        preview_line = fetch_preview()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "push-token confirmation: live-data preview fetch failed for "
            "scope=%r chat_id=%r: %s",
            scope, chat_id, exc, exc_info=True,
        )
        preview_line = None

    if preview_line:
        text = f"{connected_text}\n\n{preview_line}"
    else:
        text = (
            f"{connected_text}\n\n"
            "(Connected, but I couldn't fetch a live preview just now -- "
            "try asking me directly.)"
        )

    try:
        outbox_dir = _MESSAGES_DIR / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)

        # Include chat_id in the filename, not just scope+timestamp: two
        # rapid confirmations for the same scope but different chat_ids can
        # otherwise land on the same millisecond and silently overwrite each
        # other's outbox file (caught by BIS-744's own test suite).
        reply_id = f"{int(time.time() * 1000)}_{scope}_{chat_id}_auth"
        reply_data = {
            "id": reply_id,
            "source": "telegram",
            "chat_id": chat_id,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        reply_file = outbox_dir / f"{reply_id}.json"
        tmp_reply = reply_file.with_suffix(".json.tmp")
        tmp_fd = os.open(str(tmp_reply), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(tmp_fd, "w") as rf:
            rf.write(json.dumps(reply_data, indent=2))
        os.rename(str(tmp_reply), str(reply_file))
        logger.info(
            "%s-connected confirmation queued for chat_id=%r", scope.capitalize(), chat_id
        )
        # Only claim the de-dupe slot now that delivery has actually
        # succeeded -- see _confirmation_already_sent's docstring for why
        # claiming any earlier would silence legitimate retries after a
        # transient failure.
        _mark_confirmation_sent(chat_id, scope)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "push-token confirmation: FAILED to queue outbox confirmation for "
            "scope=%r chat_id=%r -- user will NOT be notified via Telegram: %s",
            scope, chat_id, exc, exc_info=True,
        )
        _write_system_alert(
            f"Push-token confirmation failed to deliver for scope={scope!r} "
            f"chat_id={chat_id!r}: {exc}. The token itself was saved "
            f"successfully -- only the user-facing confirmation failed."
        )


def _fetch_calendar_preview(chat_id: str) -> Optional[str]:
    """Best-effort: return a one-line preview of the user's next event."""
    from integrations.google_calendar.client import get_upcoming_events

    events = get_upcoming_events(user_id=chat_id, days=7)
    if not events:
        return "No upcoming events in the next 7 days."
    next_event = events[0]
    time_str = next_event.start.strftime("%a %b %-d, %-I:%M %p UTC")
    return f"Next up: {next_event.title} — {time_str}"


def _fetch_gmail_preview(chat_id: str) -> Optional[str]:
    """Best-effort: return a one-line preview of the user's latest email."""
    from integrations.gmail.client import get_recent_emails

    emails = get_recent_emails(user_id=chat_id, max_results=1)
    if not emails:
        return "Your inbox has no recent messages (or none I could read yet)."
    latest = emails[0]
    subject = latest.subject or "(no subject)"
    return f"Latest email: \"{subject}\" from {latest.sender}"


def _fetch_workspace_preview(chat_id: str) -> Optional[str]:
    """Best-effort: return a one-line preview of a recent Drive file.

    ``gdrive_list`` only queries the root ("My Drive") folder, non-recursively
    -- it does not search subfolders. The wording below is deliberately
    scoped to match ("in My Drive"), not a blanket "most recent file in your
    Drive" claim that would overstate what was actually checked.
    """
    from integrations.google_workspace.drive_client import gdrive_list

    files = gdrive_list(user_id=chat_id, max_results=1)
    if not files:
        return "No files found in your My Drive root folder yet."
    latest = files[0]
    return f"Recently modified in My Drive: {latest.name}"


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

    Authentication: ``Authorization: Bearer <LOBSTER_INTERNAL_SECRET>``, PLUS
    (BIS-727 Slice 1) a signed, single-use, 30-minute-TTL ``session_token``
    object bound to the specific consent transaction — see
    ``_verify_calendar_session_token`` and the warn-then-enforce rollout
    comment near ``_ENFORCE_SIGNED_SESSION`` above. During the warn window
    (default), a missing/invalid session_token is logged but still accepted;
    once ``CALENDAR_PUSH_SIGNED_SESSION_ENFORCE=true``, it is hard-rejected.

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

    # --- BIS-727 Slice 1: per-transaction signed session, calendar path only ---
    session_ok, session_reason = _verify_calendar_session_token(body, chat_id, scope_str)
    if not session_ok:
        if _ENFORCE_SIGNED_SESSION:
            logger.warning(
                "Rejecting calendar push: invalid/missing signed session "
                "(reason=%s) chat_id=%r",
                session_reason,
                chat_id,
            )
            response = JSONResponse(
                {"error": "Unauthorized: missing or invalid signed session"},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        logger.warning(
            "Calendar push missing/invalid signed session (reason=%s) chat_id=%r "
            "— accepting during BIS-729 warn-then-enforce window. Set "
            "CALENDAR_PUSH_SIGNED_SESSION_ENFORCE=true once myownlobster.ai is "
            "confirmed emitting valid sessions for all calendar consent flows.",
            session_reason,
            chat_id,
        )
    else:
        logger.info("Calendar push signed session verified for chat_id=%r", chat_id)

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

    # BIS-744: shared post-push confirmation (live-data preview, failure
    # visibility, de-dupe) -- see _queue_push_confirmation above. Best-effort:
    # the token is already saved, so a confirmation failure here must never
    # turn a successful push into an error response for the caller.
    _queue_push_confirmation(
        chat_id=safe_chat_id,
        scope="calendar",
        connected_text=(
            "Google Calendar connected. "
            "I can now read and create events on your calendar."
        ),
        fetch_preview=lambda: _fetch_calendar_preview(safe_chat_id),
    )

    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


# ---------------------------------------------------------------------------
# BIS-727 Slice 2 — per-transaction HMAC signing, gmail push path
# ---------------------------------------------------------------------------
# Identical treatment to push_calendar_token_endpoint (BIS-727 Slice 1) above
# — deliberately copy-pasted rather than shared (per the BIS-730 plan:
# premature abstraction here is what Slice 4 is for). Verifies a signed,
# single-use, 30-minute-TTL session token (HMAC-SHA256 over
# instance_id|chat_id|scope|nonce|exp) bound to the specific consent
# transaction, alongside the existing static-secret bearer check. Reuses the
# SAME _SESSION_HMAC_SECRET / _INSTANCE_URL module-level config as calendar
# (one shared secret across scopes, matching myownlobster.ai's single
# CONSENT_SESSION_HMAC_SECRET env var), but has its OWN enforce flag and
# nonce-replay set, so gmail's warn-then-enforce rollout can be flipped
# independently of calendar's and workspace's.
_ENFORCE_GMAIL_SIGNED_SESSION: bool = os.environ.get(
    "GMAIL_PUSH_SIGNED_SESSION_ENFORCE", "false"
).strip().lower() in ("1", "true", "yes")

_seen_gmail_session_nonces: dict[str, float] = {}


def _consume_gmail_session_nonce(nonce: str, exp: float) -> bool:
    """Atomically-within-this-process claim a nonce. False if already seen."""
    now = time.time()
    expired = [n for n, e in _seen_gmail_session_nonces.items() if e < now]
    for n in expired:
        del _seen_gmail_session_nonces[n]
    if nonce in _seen_gmail_session_nonces:
        return False
    _seen_gmail_session_nonces[nonce] = exp
    return True


def _verify_gmail_session_token(body: dict, chat_id: str, scope_str: str) -> tuple[bool, str]:
    """Verify the gmail push's signed, single-use, 30-min-TTL session token.

    Copy-pasted from ``_verify_calendar_session_token`` (BIS-730: deliberately
    not shared code yet). Returns ``(ok, reason)``; ``reason`` is a short
    machine-readable string for logging only — never secret material.
    """
    session = body.get("session_token")
    if not isinstance(session, dict):
        return False, "missing_session_token"

    if not _SESSION_HMAC_SECRET:
        return False, "hmac_secret_not_configured"

    instance_id = session.get("instance_id")
    session_chat_id = session.get("chat_id")
    session_scope = session.get("scope")
    nonce = session.get("nonce")
    sig = session.get("sig")
    exp = session.get("exp")

    if not (
        isinstance(instance_id, str)
        and instance_id
        and isinstance(session_chat_id, str)
        and session_chat_id
        and isinstance(session_scope, str)
        and session_scope
        and isinstance(nonce, str)
        and nonce
        and isinstance(sig, str)
        and sig
        and isinstance(exp, (int, float))
        and not isinstance(exp, bool)
    ):
        return False, "malformed_session_token"

    if session_chat_id != chat_id:
        return False, "chat_id_mismatch"
    if session_scope != scope_str:
        return False, "scope_mismatch"
    if _INSTANCE_URL and instance_id != _INSTANCE_URL:
        return False, "instance_id_mismatch"

    message = f"{instance_id}|{session_chat_id}|{session_scope}|{nonce}|{int(exp)}"
    expected_sig = hmac.new(
        _SESSION_HMAC_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return False, "bad_signature"

    if exp < time.time():
        return False, "expired"

    if not _consume_gmail_session_nonce(nonce, float(exp)):
        return False, "nonce_already_used"

    return True, "ok"


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

    Authentication: ``Authorization: Bearer <LOBSTER_INTERNAL_SECRET>``, PLUS
    (BIS-727 Slice 2) a signed, single-use, 30-minute-TTL ``session_token``
    object bound to the specific consent transaction — see
    ``_verify_gmail_session_token`` and the warn-then-enforce rollout comment
    near ``_ENFORCE_GMAIL_SIGNED_SESSION`` above. During the warn window
    (default), a missing/invalid session_token is logged but still accepted;
    once ``GMAIL_PUSH_SIGNED_SESSION_ENFORCE=true``, it is hard-rejected.

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

    # --- BIS-727 Slice 2: per-transaction signed session, gmail path ---
    session_ok, session_reason = _verify_gmail_session_token(body, chat_id, scope_str)
    if not session_ok:
        if _ENFORCE_GMAIL_SIGNED_SESSION:
            logger.warning(
                "Rejecting gmail push: invalid/missing signed session "
                "(reason=%s) chat_id=%r",
                session_reason,
                chat_id,
            )
            response = JSONResponse(
                {"error": "Unauthorized: missing or invalid signed session"},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        logger.warning(
            "Gmail push missing/invalid signed session (reason=%s) chat_id=%r "
            "— accepting during BIS-729 warn-then-enforce window. Set "
            "GMAIL_PUSH_SIGNED_SESSION_ENFORCE=true once myownlobster.ai is "
            "confirmed emitting valid sessions for all gmail consent flows.",
            session_reason,
            chat_id,
        )
    else:
        logger.info("Gmail push signed session verified for chat_id=%r", chat_id)

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

    # BIS-744: shared post-push confirmation (live-data preview, failure
    # visibility, de-dupe) -- see _queue_push_confirmation above. Best-effort:
    # the token is already saved, so a confirmation failure here must never
    # turn a successful push into an error response for the caller.
    _queue_push_confirmation(
        chat_id=safe_chat_id,
        scope="gmail",
        connected_text=(
            "Gmail connected. "
            "I can now read and search your emails."
        ),
        fetch_preview=lambda: _fetch_gmail_preview(safe_chat_id),
    )

    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


_ENRICHMENT_RUNS_DIR: Path = Path.home() / "lobster-workspace" / "enrichment-runs"
_ENRICHMENT_SCRIPT: Path = (
    Path.home()
    / "lobster"
    / "lobster-shop"
    / "prospect-enrichment"
    / "pipeline"
    / "single_contact_enrichment.py"
)


def _is_authorized_internal_secret(request: Request) -> bool:
    """Check X-Lobster-Secret header against LOBSTER_INTERNAL_SECRET."""
    if not _INTERNAL_SECRET:
        return False
    return request.headers.get("x-lobster-secret", "") == _INTERNAL_SECRET


async def enrich_contact_endpoint(scope, receive, send):
    """POST /enrich_contact — spawn single-contact enrichment pipeline.

    Called by the bisque /api/contacts/[id]/enrich route (production path).
    Spawns single_contact_enrichment.py as a detached subprocess, returns
    immediately with the run_id.

    Auth: X-Lobster-Secret header.

    Body JSON:
        contact_id: str
        run_id: str          (pre-assigned UUID from the caller)
        dry_run: bool
        kissinger_endpoint: str
        kissinger_token: str
    """
    request = Request(scope, receive)

    if not _is_authorized_internal_secret(request):
        response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        await response(scope, receive, send)
        return

    try:
        body = await request.json()
    except Exception:
        response = JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        await response(scope, receive, send)
        return

    contact_id = (body.get("contact_id") or "").strip()
    run_id = (body.get("run_id") or "").strip()
    dry_run = body.get("dry_run") is True
    kissinger_endpoint = body.get("kissinger_endpoint") or "http://localhost:8080/graphql"
    kissinger_token = body.get("kissinger_token") or ""

    if not contact_id:
        response = JSONResponse({"error": "Missing contact_id"}, status_code=400)
        await response(scope, receive, send)
        return

    if not run_id or not all(c in "0123456789abcdefABCDEF-" for c in run_id) or len(run_id) != 36:
        response = JSONResponse({"error": "Invalid run_id (must be UUID v4)"}, status_code=400)
        await response(scope, receive, send)
        return

    # Write "running" manifest immediately so status endpoint has something to return
    _ENRICHMENT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = _ENRICHMENT_RUNS_DIR / f"{run_id}.json"
    pending = {
        "run_id": run_id,
        "status": "running",
        "contact_id": contact_id,
        "dry_run": dry_run,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": None,
        "goals_attempted": ["work_history", "connections"],
        "sources_attempted": [],
        "sources_skipped": [],
        "entities_enriched": 0,
        "edges_inferred": 0,
        "skipped_fresh": 0,
        "errors": [],
    }
    try:
        manifest_path.write_text(json.dumps(pending, indent=2))
    except OSError as exc:
        logger.error("Failed to write pending enrichment manifest: %s", exc)

    # Spawn the enrichment script
    args = [
        sys.executable,
        str(_ENRICHMENT_SCRIPT),
        "--contact-id", contact_id,
        "--run-id", run_id,
        "--endpoint", kissinger_endpoint,
    ]
    if dry_run:
        args.append("--dry-run")

    env = os.environ.copy()
    env["KISSINGER_ENDPOINT"] = kissinger_endpoint
    env["KISSINGER_API_TOKEN"] = kissinger_token

    try:
        import subprocess as _subprocess
        proc = _subprocess.Popen(
            args,
            env=env,
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        logger.info(
            "Spawned enrichment subprocess pid=%d run_id=%s contact_id=%s",
            proc.pid, run_id, contact_id,
        )
    except Exception as exc:
        logger.error("Failed to spawn enrichment subprocess: %s", exc)
        # Mark manifest as failed
        pending["status"] = "failed"
        pending["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pending["errors"] = [f"Failed to launch: {exc}"]
        try:
            manifest_path.write_text(json.dumps(pending, indent=2))
        except OSError:
            pass
        response = JSONResponse({"error": "Failed to start enrichment"}, status_code=500)
        await response(scope, receive, send)
        return

    response = JSONResponse({
        "ok": True,
        "run_id": run_id,
        "contact_id": contact_id,
        "dry_run": dry_run,
    })
    await response(scope, receive, send)


async def enrichment_status_endpoint(scope, receive, send):
    """GET /enrichment_status?run_id=xxx — read run manifest.

    Called by the bisque /api/contacts/[id]/enrich/status route.
    Reads ~/lobster-workspace/enrichment-runs/{run_id}.json and returns it.

    Auth: X-Lobster-Secret header.
    Returns 404 if the file doesn't exist yet (subprocess still starting).
    """
    request = Request(scope, receive)

    if not _is_authorized_internal_secret(request):
        response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        await response(scope, receive, send)
        return

    run_id = request.query_params.get("run_id", "").strip()
    if not run_id:
        response = JSONResponse({"error": "Missing run_id"}, status_code=400)
        await response(scope, receive, send)
        return

    # Validate: UUID format only (prevent path traversal)
    if not all(c in "0123456789abcdefABCDEF-" for c in run_id) or len(run_id) != 36:
        response = JSONResponse({"error": "Invalid run_id"}, status_code=400)
        await response(scope, receive, send)
        return

    manifest_path = _ENRICHMENT_RUNS_DIR / f"{run_id}.json"
    if not manifest_path.exists():
        response = JSONResponse({"error": "Run not found"}, status_code=404)
        await response(scope, receive, send)
        return

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read enrichment manifest %s: %s", run_id, exc)
        response = JSONResponse({"error": "Could not read run manifest"}, status_code=500)
        await response(scope, receive, send)
        return

    response = JSONResponse(manifest)

async def awp_intake_endpoint(scope, receive, send):
    """POST /api/webhooks/intake — receive an intake form submission.

    Sent by a Google Apps Script attached to a form or data source. The Apps Script
    stores the auth token as ``LOBSTER_SECRET``; on this side it is ``LOBSTER_IMPORT_TOKEN``.

    Authentication: ``Authorization: Bearer <LOBSTER_IMPORT_TOKEN>``
    """
    request = Request(scope, receive)

    if not _is_authorized_intake(request):
        response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        await response(scope, receive, send)
        return

    try:
        body = await request.json()
    except Exception:
        response = JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        await response(scope, receive, send)
        return

    # Extract fields — all are optional so the message is written even if partial
    full_name = str(body.get("full_name", "")).strip()
    email = str(body.get("email", "")).strip()
    investable_capital = str(body.get("investable_capital", "")).strip()
    accreditation_status = str(body.get("accreditation_status", "")).strip()
    entity_type = str(body.get("entity_type", "")).strip()

    now = datetime.now(timezone.utc)
    timestamp_ms = int(now.timestamp() * 1000)
    message_id = f"{timestamp_ms}_intake"

    summary_text = (
        f"New intake: {full_name} ({email})\n"
        f"Capital: {investable_capital}\n"
        f"Accreditation: {accreditation_status}\n"
        f"Entity: {entity_type}"
    )

    message = {
        "id": message_id,
        "type": "intake",
        "source": "intake",
        "chat_id": 0,
        "text": summary_text,
        "payload": body,
        "timestamp": now.isoformat(),
    }

    try:
        _INBOX_DIR.mkdir(parents=True, exist_ok=True)
        inbox_path = _INBOX_DIR / f"{message_id}.json"
        tmp_path = inbox_path.with_suffix(".json.tmp")
        payload_str = json.dumps(message, indent=2)
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(payload_str)
        os.rename(str(tmp_path), str(inbox_path))
        logger.info("Intake message written: %s (from=%r)", message_id, email)
    except Exception as exc:
        logger.error("Failed to write AWP intake message: %s", exc)
        response = JSONResponse({"error": "Failed to write message"}, status_code=500)
        await response(scope, receive, send)
        return

    response = JSONResponse({"status": "ok", "message_id": message_id})
    await response(scope, receive, send)
    await response(scope, receive, send)


# ---------------------------------------------------------------------------
# BIS-727 Slice 2 — per-transaction HMAC signing, workspace push path
# ---------------------------------------------------------------------------
# Identical treatment to push_calendar_token_endpoint (BIS-727 Slice 1) and
# push_gmail_token_endpoint (BIS-727 Slice 2, above) — deliberately
# copy-pasted rather than shared (per the BIS-730 plan: premature
# abstraction here is what Slice 4 is for). Verifies a signed, single-use,
# 30-minute-TTL session token (HMAC-SHA256 over
# instance_id|chat_id|scope|nonce|exp) bound to the specific consent
# transaction, alongside the existing static-secret bearer check. Reuses the
# SAME _SESSION_HMAC_SECRET / _INSTANCE_URL module-level config as calendar
# and gmail, but has its OWN enforce flag and nonce-replay set, so
# workspace's warn-then-enforce rollout can be flipped independently.
_ENFORCE_WORKSPACE_SIGNED_SESSION: bool = os.environ.get(
    "WORKSPACE_PUSH_SIGNED_SESSION_ENFORCE", "false"
).strip().lower() in ("1", "true", "yes")

_seen_workspace_session_nonces: dict[str, float] = {}


def _consume_workspace_session_nonce(nonce: str, exp: float) -> bool:
    """Atomically-within-this-process claim a nonce. False if already seen."""
    now = time.time()
    expired = [n for n, e in _seen_workspace_session_nonces.items() if e < now]
    for n in expired:
        del _seen_workspace_session_nonces[n]
    if nonce in _seen_workspace_session_nonces:
        return False
    _seen_workspace_session_nonces[nonce] = exp
    return True


def _verify_workspace_session_token(body: dict, chat_id: str, scope_str: str) -> tuple[bool, str]:
    """Verify the workspace push's signed, single-use, 30-min-TTL session token.

    Copy-pasted from ``_verify_calendar_session_token`` (BIS-730: deliberately
    not shared code yet). Returns ``(ok, reason)``; ``reason`` is a short
    machine-readable string for logging only — never secret material.
    """
    session = body.get("session_token")
    if not isinstance(session, dict):
        return False, "missing_session_token"

    if not _SESSION_HMAC_SECRET:
        return False, "hmac_secret_not_configured"

    instance_id = session.get("instance_id")
    session_chat_id = session.get("chat_id")
    session_scope = session.get("scope")
    nonce = session.get("nonce")
    sig = session.get("sig")
    exp = session.get("exp")

    if not (
        isinstance(instance_id, str)
        and instance_id
        and isinstance(session_chat_id, str)
        and session_chat_id
        and isinstance(session_scope, str)
        and session_scope
        and isinstance(nonce, str)
        and nonce
        and isinstance(sig, str)
        and sig
        and isinstance(exp, (int, float))
        and not isinstance(exp, bool)
    ):
        return False, "malformed_session_token"

    if session_chat_id != chat_id:
        return False, "chat_id_mismatch"
    if session_scope != scope_str:
        return False, "scope_mismatch"
    if _INSTANCE_URL and instance_id != _INSTANCE_URL:
        return False, "instance_id_mismatch"

    message = f"{instance_id}|{session_chat_id}|{session_scope}|{nonce}|{int(exp)}"
    expected_sig = hmac.new(
        _SESSION_HMAC_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return False, "bad_signature"

    if exp < time.time():
        return False, "expired"

    if not _consume_workspace_session_nonce(nonce, float(exp)):
        return False, "nonce_already_used"

    return True, "ok"


async def push_workspace_token_endpoint(scope, receive, send):
    """POST /api/push-workspace-token — receive a Workspace token pushed by myownlobster.ai.

    Expected JSON body::

        {
          "chat_id":       "<telegram chat_id as string>",
          "access_token":  "<string>",
          "refresh_token": "<string>",
          "expires_at":    "<ISO 8601 UTC string>",
          "scope":         "<space-separated scopes>"
        }

    Authentication: ``Authorization: Bearer <LOBSTER_INTERNAL_SECRET>``, PLUS
    (BIS-727 Slice 2) a signed, single-use, 30-minute-TTL ``session_token``
    object bound to the specific consent transaction — see
    ``_verify_workspace_session_token`` and the warn-then-enforce rollout
    comment near ``_ENFORCE_WORKSPACE_SIGNED_SESSION`` above. During the warn
    window (default), a missing/invalid session_token is logged but still
    accepted; once ``WORKSPACE_PUSH_SIGNED_SESSION_ENFORCE=true``, it is
    hard-rejected.

    Writes the token to ``~/messages/config/workspace-tokens/{chat_id}.json``
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

    # --- BIS-727 Slice 2: per-transaction signed session, workspace path ---
    session_ok, session_reason = _verify_workspace_session_token(body, chat_id, scope_str)
    if not session_ok:
        if _ENFORCE_WORKSPACE_SIGNED_SESSION:
            logger.warning(
                "Rejecting workspace push: invalid/missing signed session "
                "(reason=%s) chat_id=%r",
                session_reason,
                chat_id,
            )
            response = JSONResponse(
                {"error": "Unauthorized: missing or invalid signed session"},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        logger.warning(
            "Workspace push missing/invalid signed session (reason=%s) chat_id=%r "
            "— accepting during BIS-729 warn-then-enforce window. Set "
            "WORKSPACE_PUSH_SIGNED_SESSION_ENFORCE=true once myownlobster.ai is "
            "confirmed emitting valid sessions for all workspace consent flows.",
            session_reason,
            chat_id,
        )
    else:
        logger.info("Workspace push signed session verified for chat_id=%r", chat_id)

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
        _WORKSPACE_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = _WORKSPACE_TOKEN_DIR / f"{safe_chat_id}.json"
        tmp_path = token_path.with_suffix(".json.tmp")
        payload = json.dumps(token_data, indent=2)
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.rename(str(tmp_path), str(token_path))
        logger.info("Workspace token pushed and saved for chat_id=%r", safe_chat_id)
    except Exception as exc:
        logger.error("Failed to write workspace token for chat_id=%r: %s", safe_chat_id, exc)
        response = JSONResponse({"error": "Failed to write token"}, status_code=500)
        await response(scope, receive, send)
        return

    # BIS-744: shared post-push confirmation (live-data preview, failure
    # visibility, de-dupe) -- see _queue_push_confirmation above. This
    # replaces the original bare `except Exception: log.warning(...)` block
    # (zero user-visible fallback on failure) with the hardened shared
    # helper. Best-effort: the token is already saved, so a confirmation
    # failure here must never turn a successful push into an error response.
    _queue_push_confirmation(
        chat_id=safe_chat_id,
        scope="workspace",
        connected_text=(
            "Google Workspace connected. "
            "You can now use /gdocs, /gdrive, and /gsheets."
        ),
        fetch_preview=lambda: _fetch_workspace_preview(safe_chat_id),
    )

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

    # Enrichment endpoints — authenticated by LOBSTER_INTERNAL_SECRET (X-Lobster-Secret header)
    if path == "/enrich_contact":
        await enrich_contact_endpoint(scope, receive, send)
        return

    if path == "/enrichment_status":
        await enrichment_status_endpoint(scope, receive, send)
        return

    # Intake webhook — authenticated by LOBSTER_IMPORT_TOKEN (Apps Script: LOBSTER_SECRET)
    if path in ("/api/webhooks/intake", "/api/webhooks/awp-intake"):
        await awp_intake_endpoint(scope, receive, send)
        return

    # Workspace token push — authenticated by LOBSTER_INTERNAL_SECRET
    if path == "/api/push-workspace-token":
        await push_workspace_token_endpoint(scope, receive, send)
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
