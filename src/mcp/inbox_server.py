#!/usr/bin/env python3
"""
Lobster Inbox MCP Server

Provides tools for Claude Code to interact with the message queue:
- check_inbox: Get new messages from all sources
- send_reply: Send a reply back to the original source
- list_sources: List available message sources
- get_message: Get a specific message by ID
- mark_processed: Mark a message as processed
"""

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import sys
import time
import threading
import uuid
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Ensure src/mcp/ is on sys.path so log_utils (a sibling module) can be
# imported when this script is run directly (same guard used by
# observability_server.py and wire_server.py).
_MCP_SRC_DIR = str(Path(__file__).resolve().parent)
if _MCP_SRC_DIR not in sys.path:
    sys.path.insert(0, _MCP_SRC_DIR)
from log_utils import JsonFormatter, configure_file_handler

# Early logger — same name as the main `log` object defined after all imports.
# Python's logging registry is global, so this resolves to the same Logger
# instance that gets a StreamHandler + RotatingFileHandler during setup_logging().
# Using it here (before those handlers are attached) sends records to the root
# logger fallback, which is acceptable for startup diagnostics.
_startup_log = logging.getLogger("lobster-mcp")

# Event bus — structured observability infrastructure (issue #890).
# Imported here so callsites can use _emit_event() throughout the module.
# The singleton is initialised later in main(); events emitted before init
# are silently dropped (bus has no listeners yet — safe, not a hard error).
try:
    from event_bus import get_event_bus, LobsterEvent
    _EVENT_BUS_AVAILABLE = True
except ImportError:
    _EVENT_BUS_AVAILABLE = False

# Ensure the parent src/ directory is on sys.path so that sibling packages
# (e.g. integrations, utils, bot) can be imported when this script is run
# directly via `python inbox_server.py` (which only adds src/mcp/ to sys.path).
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# IFTTT behavioral rules store
from utils.ifttt_rules import (
    load_rules as _ifttt_load_rules,
    save_rules as _ifttt_save_rules,
    add_rule as _ifttt_add_rule,
    remove_rule as _ifttt_remove_rule,
    find_rule as _ifttt_find_rule,
    get_enabled_rules as _ifttt_get_enabled_rules,
)
import uuid as _uuid_mod

# Reliability utilities (atomic writes, validation, audit logging)
from reliability import (
    atomic_write_json,
    validate_send_reply_args,
    validate_message_id,
    ValidationError,
    init_audit_log,
    audit_log,
)

# Self-update system
from update_manager import UpdateManager

# Pending agent tracker (thin adapter over session_store)
from agents.tracker import add_pending_agent as _add_pending_agent, remove_pending_agent as _remove_pending_agent

# Agent session store — SQLite-backed, used directly for new MCP tools
import agents.session_store as _session_store

# Atomic claim DB — SQLite INSERT OR FAIL claim gate (issue #1360)
from claims import AtomicClaimDB as _AtomicClaimDB

# Skill management system
from skill_manager import (
    list_available_skills as _list_available_skills,
    get_skill_context as _get_skill_context,
    activate_skill as _activate_skill,
    deactivate_skill as _deactivate_skill,
    get_skill_preferences as _get_skill_preferences,
    set_skill_preference as _set_skill_preference,
)
_update_manager = UpdateManager()

# DB read layer (BIS-164 Slice 3) — optional, graceful degradation
_db_get_conversation_history = None
_db_count_conversation_history = None
_db_get_message_by_telegram_id_fn = None
_db_get_message_stats = None
_db_open_messages_db = None
try:
    from db import reader as _db_reader_mod
    from db.connection import open_messages_db as _db_open_messages_db
    _db_get_conversation_history = _db_reader_mod.get_conversation_history
    _db_count_conversation_history = _db_reader_mod.count_conversation_history
    _db_get_message_by_telegram_id_fn = _db_reader_mod.get_message_by_telegram_id
    _db_get_message_stats = _db_reader_mod.get_message_stats
    _startup_log.info('[DB] db.reader loaded — DB reads available')
except Exception as _db_reader_import_err:
    _startup_log.warning('DB reader module unavailable: %s', _db_reader_import_err)

# BIS-167 Slice 6: Live DB write path — persist messages to messages.db
_db_persist_inbound = None
_db_persist_outbound = None
_db_persist_agent_event = None
try:
    from db.message_store import (
        persist_inbound as _db_persist_inbound,
        persist_outbound as _db_persist_outbound,
        persist_agent_event as _db_persist_agent_event,
    )
    _db_flag = os.environ.get("LOBSTER_USE_DB", "0")
    _db_status = "ENABLED" if _db_flag == "1" else "DISABLED (set LOBSTER_USE_DB=1 to enable)"
    _startup_log.info('[DB] message_store loaded — DB writes %s', _db_status)
    del _db_flag, _db_status
except Exception as _db_import_err:
    _startup_log.warning('messages.db write path unavailable: %s', _db_import_err)


# Memory system (optional — gracefully degrades to static file search)
_memory_provider = None
try:
    from memory import create_memory_provider, MemoryEvent
    _memory_provider = create_memory_provider(use_vector=True)
except Exception as _mem_err:
    # Memory system is optional; log and continue
    import traceback as _tb
    _startup_log.warning('Memory system unavailable: %s', _mem_err)

# User Model subsystem
_user_model = None
_user_model_tool_names: set[str] = set()
USER_MODEL_TOOL_DEFINITIONS: list = []
try:
    from user_model import create_user_model, USER_MODEL_TOOL_DEFINITIONS
    _user_model = create_user_model()
    _user_model_tool_names = _user_model.tool_names
    _startup_log.info('User Model subsystem initialized.')
except Exception as _um_err:
    import traceback as _um_tb
    _startup_log.warning('User Model subsystem unavailable: %s', _um_err)

# ---------------------------------------------------------------------------
# Background observation worker — fire-and-forget, zero main-thread blocking
# ---------------------------------------------------------------------------
import queue as _queue_mod

_observation_queue: _queue_mod.Queue = _queue_mod.Queue(maxsize=500)
_observation_thread: threading.Thread | None = None


def _observation_worker() -> None:
    """Daemon thread: drain observation queue, call user_model.observe()."""
    while True:
        try:
            item = _observation_queue.get(timeout=10)
        except _queue_mod.Empty:
            continue
        if item is None:  # shutdown sentinel
            break
        try:
            msg_text, msg_id, source, ts = item
            if _user_model is not None:
                obs_ids = _user_model.observe(msg_text, msg_id, context=source or "", message_ts=ts)
                # Debug: emit Tier 1 signal summary when LOBSTER_DEBUG=true.
                # _emit_event is a no-op when LOBSTER_DEBUG != true (bus filter).
                if obs_ids:
                    try:
                        from user_model.observation import extract_signals
                        signals = extract_signals(msg_text, msg_id, context=source or "")
                        if signals:
                            signal_parts = []
                            for sig in signals:
                                sig_type = (
                                    sig.signal_type.value
                                    if hasattr(sig.signal_type, "value")
                                    else str(sig.signal_type)
                                )
                                signal_parts.append(
                                    f"{sig_type}={sig.content[:30]!r} ({sig.confidence:.2f})"
                                )
                            summary = ", ".join(signal_parts[:6])  # cap at 6
                            short_id = msg_id[:20] if len(msg_id) > 20 else msg_id
                            _emit_event(
                                f"\U0001f50d [tier 1 fired] msg={short_id} "
                                f"extracted {len(signals)} signal(s): {summary}",
                                event_type="user_model.tier1",
                                severity="debug",
                                source="observation-worker",
                            )
                    except Exception:
                        pass  # never block observation on debug emit
        except Exception as _obs_exc:
            import traceback as _tb
            _emit_event(
                f"\U0001f50d [observation worker error] {type(_obs_exc).__name__}: {_obs_exc}\n"
                + _tb.format_exc()[-800:],
                event_type="system.error",
                severity="error",
                source="observation-worker",
            )
            # never crash the worker


def _ensure_observation_worker() -> None:
    """Start the background observation thread if not already running."""
    global _observation_thread
    if _user_model is None:
        return
    if _observation_thread is not None and _observation_thread.is_alive():
        return
    _observation_thread = threading.Thread(
        target=_observation_worker, daemon=True, name="um-observer"
    )
    _observation_thread.start()


def _queue_observation(msg_text: str, msg_id: str, source: str | None = None, ts: str | None = None) -> None:
    """Non-blocking: enqueue a message for background observation. Drops if full."""
    if _user_model is None:
        return
    try:
        _observation_queue.put_nowait((msg_text, msg_id, source, ts))
    except _queue_mod.Full:
        pass  # drop silently — observation is best-effort

# ---------------------------------------------------------------------------
# Debug observability — LOBSTER_DEBUG=true push notifications
#
# _emit_event() is a thin wrapper around the event bus singleton.
# _emit_debug_observation() is the direct-write function used by handlers
# that need to be patchable in unit tests (memory_store, memory_search,
# write_result).  It writes directly to OUTBOX_DIR when alerts are enabled,
# bypassing the event bus so that tests can mock it cleanly with patch.multiple.
#
# Module-level flags (_DEBUG_MODE, _DEBUG_ALERTS_ENABLED, _DEBUG_RESOLVED,
# _DEBUG_OWNER_CHAT_ID, _DEBUG_OWNER_SOURCE) are exposed so that tests can
# inject known state via patch.multiple without touching the event bus or
# environment variables.
# ---------------------------------------------------------------------------

# Module-level debug state — patchable by tests via patch.multiple
_DEBUG_MODE: bool = os.environ.get("LOBSTER_DEBUG", "").lower() in ("true", "1", "yes")
_DEBUG_ALERTS_ENABLED: bool = _DEBUG_MODE
_DEBUG_RESOLVED: bool = False  # True once _resolve_debug_config has run
_DEBUG_OWNER_CHAT_ID: int | str | None = None
_DEBUG_OWNER_SOURCE: str = "telegram"


def _emit_debug_observation(
    text: str,
    category: str = "system_context",
    visibility: str = "mcp-only",
    emitter: str | None = None,
) -> None:
    """Write a debug observation directly to OUTBOX_DIR.

    Gate: returns immediately when _DEBUG_ALERTS_ENABLED is False or
    _DEBUG_OWNER_CHAT_ID is None.  Handlers call this unconditionally —
    the gate lives here, not at the call site.

    Writes a JSON file to OUTBOX_DIR so the Telegram bot delivers the
    alert directly to the owner without touching the dispatcher inbox.

    Never raises — must be safe to call from any handler.
    """
    if not _DEBUG_ALERTS_ENABLED:
        return
    if _DEBUG_OWNER_CHAT_ID is None:
        return
    try:
        import time as _time_mod
        ts_ms = int(_time_mod.time() * 1000)
        safe_emitter = (emitter or "unknown").replace("/", "_")[:40]
        message_id = f"{ts_ms}_debug_{safe_emitter}"
        label = f"[debug|{visibility}]"
        if emitter:
            label += f" {emitter}"
        full_text = f"{label}\n{text}"
        message = {
            "id": message_id,
            "type": "debug_observation",
            "source": _DEBUG_OWNER_SOURCE,
            "chat_id": _DEBUG_OWNER_CHAT_ID,
            "text": full_text,
            "timestamp": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        }
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        outbox_file = OUTBOX_DIR / f"{message_id}.json"
        tmp_file = outbox_file.with_suffix(".tmp")
        tmp_file.write_text(__import__("json").dumps(message), encoding="utf-8")
        tmp_file.rename(outbox_file)
    except Exception:
        pass  # debug delivery must never crash production


def _emit_event(
    text: str,
    event_type: str = "debug.observation",
    severity: str = "debug",
    source: str = "inbox-server",
    emitter: str | None = None,
    task_id: str | None = None,
    chat_id: int | str | None = None,
) -> None:
    """
    Emit a structured event to the bus.

    Replaces _emit_debug_observation().  All filtering (debug-mode gate,
    system_context suppression) lives in the bus listeners registered at
    startup, so callers do not need to check LOBSTER_DEBUG themselves.

    Never raises — must be safe to call from any thread.
    """
    if not _EVENT_BUS_AVAILABLE:
        return
    try:
        bus = get_event_bus()
        event = LobsterEvent(
            event_type=event_type,
            severity=severity,
            source=source,
            payload={"text": text},
            task_id=task_id,
            chat_id=chat_id,
        )
        bus.emit_sync(event)
    except Exception:
        pass  # never block on observability


def _resolve_debug_config() -> None:
    """No-op — preserved for call compatibility. Config resolution moved to event bus."""
    pass


def _emit_mcp_event(
    event_type: str,
    payload: dict,
    severity: str = "info",
    chat_id: int | str | None = None,
) -> None:
    """
    Centralized EventBus emission for MCP handler audit-trail events (issue #1459).

    All 5 per-handler emit blocks (telegram.inbound, telegram.outbound,
    inbox.processed, inbox.failed, job.completed) were identical except for
    event_type, payload, severity, and chat_id.  This function extracts that
    boilerplate into one place.

    Callers pass only the semantically meaningful fields; this function handles
    availability guard, LobsterEvent construction, emit_sync(), and
    exception suppression so the main handler path is never blocked.

    Adding a new event type = one call to _emit_mcp_event(), no copy-pasted
    try/except block.
    """
    if not _EVENT_BUS_AVAILABLE:
        return
    try:
        event = LobsterEvent(
            event_type=event_type,
            severity=severity,
            source="inbox-server",
            payload=payload,
            chat_id=chat_id,
        )
        get_event_bus().emit_sync(event)
    except Exception:
        pass  # observability must never block the caller


# Populated from event_bus when available — used by handle_emit_event below.
try:
    from event_bus import VALID_SEVERITIES as _VALID_SEVERITIES
except ImportError:
    _VALID_SEVERITIES = frozenset({"debug", "info", "warn", "error", "critical"})  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# User model context injection heuristic
# ---------------------------------------------------------------------------
_USER_CONTEXT_TRIGGERS = re.compile(
    r'(?i)(?:'
    r'(?:what|where)\s+should\s+i'             # "what should I focus on"
    r'|priorit(?:y|ies|ize)'                    # priorities
    r'|what\s+(?:matters|do\s+i\s+care)'        # "what matters to me"
    r'|help\s+me\s+(?:decide|choose|think)'     # decision-making
    r'|(?:my|the)\s+(?:values?|principles?|preferences?|constraints?)' # explicit model refs
    r'|what.{0,8}(?:on\s+my\s+plate|next)'     # "what's on my plate"
    r'|big\s+picture'                           # stepping back
    r'|step(?:ping)?\s+back'                    # reflection
    r'|how\s+am\s+i\s+doing'                    # self-check
    r'|(?:over|under)whelm'                     # emotional state
    r'|burn.?out|stressed\s+(?:about|out)'      # stress
    r'|life\s+(?:situation|direction|goals?)'    # life-level
    r'|introspect|self.?reflect'                # introspection
    r'|what\s+(?:do\s+i|did\s+i)\s+think'      # recall preferences
    r'|remind\s+me\s+(?:what|why)\s+i'          # recall motivations
    r'|(?:deprioritize|reprioritize|reorder)'   # attention management
    r'|what.{0,8}(?:important|urgent)'          # importance queries
    r'|good\s+(?:morning|evening|night)'        # greetings that often start reflective exchanges
    r')',
)

def _should_inject_user_context(text: str) -> bool:
    """Fast heuristic: does this message benefit from user model context?"""
    if not text or len(text) < 8:
        return False
    return bool(_USER_CONTEXT_TRIGGERS.search(text))

# MCP SDK
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Directories
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_USER_CONFIG = Path(os.environ.get("LOBSTER_USER_CONFIG", Path.home() / "lobster-user-config"))
BASE_DIR = _MESSAGES
INBOX_DIR = BASE_DIR / "inbox"
OUTBOX_DIR = BASE_DIR / "outbox"
PROCESSED_DIR = BASE_DIR / "processed"
PROCESSING_DIR = BASE_DIR / "processing"
FAILED_DIR = BASE_DIR / "failed"
CONFIG_DIR = BASE_DIR / "config"
AUDIO_DIR = BASE_DIR / "audio"
SENT_DIR = BASE_DIR / "sent"
SENT_REPLIES_DIR = BASE_DIR / "sent-replies"
TASK_REPLIED_DIR = BASE_DIR / "task-replied"
TASKS_FILE = BASE_DIR / "tasks.json"
TASK_OUTPUTS_DIR = BASE_DIR / "task-outputs"
BISQUE_OUTBOX_DIR = BASE_DIR / "bisque-outbox"
MESSAGES_DB_PATH = Path(
    os.environ.get("LOBSTER_MESSAGES_DB", str(BASE_DIR / "messages.db"))
)
LOBSTER_TMUX_SESSION = os.environ.get("LOBSTER_TMUX_SESSION", "lobster")

# Instance identity for multi-instance deployments (BIS-85).
# Prefer an explicit observability token; fall back to hostname so reports are
# always attributed to the Lobster instance that filed them.
_INSTANCE_ID: str = os.environ.get("LOBSTER_OBSERVABILITY_TOKEN") or socket.gethostname()

# Reply tracking — records {chat_id_str: timestamp} when send_reply is called.
# Used by mark_processed to guard against dropping human messages without reply.
_recent_replies: dict[str, float] = {}
_REPLY_TRACK_MAX = 100


# ---------------------------------------------------------------------------
# Timezone utility — delegates to utils.timezone for all display/conversion
# ---------------------------------------------------------------------------
#
# The canonical implementation lives in utils/timezone.py.  Local wrappers
# are kept here for backwards-compatibility with existing call sites inside
# this file so that no other lines need to change.
#
# _format_ts_with_et has been renamed to _format_ts_for_user and now uses
# the owner's configured timezone instead of hardcoding Eastern Time.
# ---------------------------------------------------------------------------

try:
    from utils.timezone import (
        format_for_user as _tz_format_for_user,
        format_iso_for_user as _tz_format_iso_for_user,
        format_with_utc_and_local as _tz_format_with_utc_and_local,
        get_owner_zoneinfo as _tz_get_owner_zoneinfo,
    )
    _TZ_UTIL_AVAILABLE = True
except ImportError:
    _TZ_UTIL_AVAILABLE = False


def _get_display_tz():
    """Return the owner's local timezone (ZoneInfo) for display purposes."""
    if _TZ_UTIL_AVAILABLE:
        return _tz_get_owner_zoneinfo()
    import zoneinfo as _zoneinfo
    try:
        from user_model.owner import get_owner_timezone as _get_owner_tz
        return _zoneinfo.ZoneInfo(_get_owner_tz())
    except Exception:
        return _zoneinfo.ZoneInfo("UTC")


def _format_display_ts(dt: "datetime", fmt: str = "%Y-%m-%d %I:%M %p %Z") -> str:
    """Convert a datetime to the owner's local timezone and format it for display.

    Naive datetimes are assumed UTC.
    """
    if _TZ_UTIL_AVAILABLE:
        return _tz_format_for_user(dt, fmt=fmt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_get_display_tz()).strftime(fmt)


def _format_iso_for_display(iso_str: str, fmt: str = "%Y-%m-%d %I:%M %p %Z") -> str:
    """Parse an ISO 8601 string and format it in the owner's local timezone."""
    if _TZ_UTIL_AVAILABLE:
        return _tz_format_iso_for_user(iso_str, fmt=fmt)
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return _format_display_ts(dt, fmt)
    except Exception:
        return iso_str


def _format_ts_with_et(ts_str: str) -> str:
    """Format a timestamp as 'YYYY-MM-DDTHH:MM:SS UTC (H:MM AM/PM <owner-tz>)'.

    Previously hardcoded to Eastern Time; now uses the owner's configured
    timezone from owner.toml so display matches the owner's actual locale.
    Falls back to the raw string if parsing fails.
    """
    if _TZ_UTIL_AVAILABLE:
        return _tz_format_with_utc_and_local(ts_str)
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        utc_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
        local_dt = dt.astimezone(_get_display_tz())
        local_str = local_dt.strftime("%I:%M %p %Z").lstrip("0")
        return f"{utc_str} ({local_str})"
    except Exception:
        return ts_str


def _track_reply(chat_id: Any) -> None:
    """Record that a reply was sent to chat_id."""
    global _recent_replies
    key = str(chat_id)
    _recent_replies[key] = time.time()
    # Evict old entries if over limit
    if len(_recent_replies) > _REPLY_TRACK_MAX:
        cutoff = time.time() - 3600  # keep last hour
        _recent_replies = {k: v for k, v in _recent_replies.items() if v > cutoff}
        # If still over limit after time-based eviction, keep newest entries
        if len(_recent_replies) > _REPLY_TRACK_MAX:
            sorted_items = sorted(_recent_replies.items(), key=lambda x: x[1], reverse=True)
            _recent_replies = dict(sorted_items[:_REPLY_TRACK_MAX])

# Direct-send deduplication — tracks recent send_reply calls by (chat_id, text_hash).
# When write_result is called with sent_reply_to_user=False, we check whether an identical
# message was already delivered directly to the same chat within the dedup window.  If so,
# sent_reply_to_user is silently overridden to True to prevent the dispatcher from sending a
# duplicate to the user.
#
# State is stored as small files in SENT_REPLIES_DIR rather than an in-memory dict so
# that it survives MCP server restarts.  Each file is named after the dedup key and
# contains the Unix timestamp of the send.  Files older than the window are expired
# lazily on each record call.
_DIRECT_SEND_WINDOW_SECS = 60  # suppress duplicates within this window

def _direct_send_key(chat_id: Any, text: str) -> str:
    import hashlib
    return f"{chat_id}_{hashlib.sha256(text.encode()).hexdigest()[:16]}"

def _record_direct_send(chat_id: Any, text: str) -> None:
    """Record a direct send_reply call so write_result can detect duplicates."""
    key = _direct_send_key(chat_id, text)
    marker = SENT_REPLIES_DIR / key
    marker.write_text(str(time.time()))
    # Lazily evict files older than the window
    try:
        cutoff = time.time() - _DIRECT_SEND_WINDOW_SECS
        for f in SENT_REPLIES_DIR.iterdir():
            try:
                if float(f.read_text()) < cutoff:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass

def _was_sent_directly(chat_id: Any, text: str) -> bool:
    """Return True if an identical message was sent directly to chat_id recently."""
    key = _direct_send_key(chat_id, text)
    marker = SENT_REPLIES_DIR / key
    if not marker.exists():
        return False
    try:
        sent_at = float(marker.read_text())
    except Exception:
        return False
    if (time.time() - sent_at) >= _DIRECT_SEND_WINDOW_SECS:
        marker.unlink(missing_ok=True)
        return False
    return True

# Task-ID-based dedup registry — primary dedup mechanism for send_reply + write_result.
# When send_reply is called with a task_id, we record (task_id, chat_id) here.
# When write_result arrives with the same task_id, sent_reply_to_user is auto-set to True.
# This is more reliable than text-hash dedup because it works even when send_reply
# and write_result carry different texts (e.g. full reply vs short summary).
#
# State is stored as small files in TASK_REPLIED_DIR (same pattern as SENT_REPLIES_DIR)
# so it survives MCP server restarts within the dedup window.
_TASK_REPLIED_WINDOW_SECS = 300  # 5-minute window — generous enough to cover slow subagents


def _task_replied_key(task_id: str, chat_id: Any) -> str:
    """Return a filesystem-safe key for the (task_id, chat_id) pair."""
    import hashlib
    combined = f"{task_id}::{chat_id}"
    return f"tr_{hashlib.sha256(combined.encode()).hexdigest()[:24]}"


def _record_task_replied(task_id: str, chat_id: Any) -> None:
    """Record that send_reply was called with this task_id so write_result can suppress relay."""
    try:
        key = _task_replied_key(task_id, chat_id)
        marker = TASK_REPLIED_DIR / key
        marker.write_text(str(time.time()))
        # Lazily evict expired entries
        cutoff = time.time() - _TASK_REPLIED_WINDOW_SECS
        for f in TASK_REPLIED_DIR.iterdir():
            try:
                if float(f.read_text()) < cutoff:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _was_task_replied(task_id: str, chat_id: Any) -> bool:
    """Return True if send_reply was called with this task_id for this chat_id recently."""
    try:
        key = _task_replied_key(task_id, chat_id)
        marker = TASK_REPLIED_DIR / key
        if not marker.exists():
            return False
        sent_at = float(marker.read_text())
        if (time.time() - sent_at) >= _TASK_REPLIED_WINDOW_SECS:
            marker.unlink(missing_ok=True)
            return False
        return True
    except Exception:
        return False


# Sources that represent human users (not system/automated)
# NOTE: Do NOT use this to classify whether a message needs a reply — source is
# the routing destination, not the message type. A subagent_result has
# source="telegram" for routing but is NOT a direct user message.
_HUMAN_SOURCES = {"telegram", "sms", "signal", "slack", "whatsapp", "bisque"}

# ---------------------------------------------------------------------------
# Per-session user message counter (issue #1159 — session-note-appender trigger)
#
# Counts how many real user messages (type in USER_FACING_TYPES, source in
# _HUMAN_SOURCES) have been processed via mark_processing in this server
# process lifetime. Every 20 messages a `session_note_reminder` system
# message is injected into the inbox so the dispatcher can spawn
# session-note-appender without relying on working-context counting.
#
# Reset to 0 on process start — keyed to the process lifetime, which maps
# to a single dispatcher session (dispatcher restarts coincide with process
# restarts via systemd / hibernation).
# ---------------------------------------------------------------------------
_user_message_counter: int = 0
SESSION_NOTE_REMINDER_INTERVAL: int = 20


def _tick_user_message_counter(msg_type: str, msg_source: str) -> None:
    """Increment _user_message_counter for real user messages and inject a
    session_note_reminder into the inbox every SESSION_NOTE_REMINDER_INTERVAL
    messages.

    Only counts messages where msg_type is in USER_FACING_TYPES and msg_source
    is in _HUMAN_SOURCES.  System messages, subagent results, cron reminders,
    and other non-human traffic are excluded.

    Called from both handle_mark_processing and handle_claim_and_ack so that
    the counter ticks regardless of which claim path the dispatcher uses.
    """
    if msg_type not in USER_FACING_TYPES or msg_source not in _HUMAN_SOURCES:
        return

    # Developer mode: suppress session_note_reminder injections so the developer
    # isn't bothered while testing. Real user messages are never suppressed.
    if os.environ.get("LOBSTER_DEV_MODE", "").lower() in ("true", "1"):
        return

    global _user_message_counter
    _user_message_counter += 1
    if _user_message_counter % SESSION_NOTE_REMINDER_INTERVAL == 0:
        try:
            now_utc = datetime.now(timezone.utc)
            ts_ms = int(now_utc.timestamp() * 1000)
            reminder_id = f"{ts_ms}_session_note_reminder"
            reminder = {
                "id": reminder_id,
                "type": "session_note_reminder",
                "source": "system",
                "chat_id": 0,
                "task_origin": "internal",
                "text": (
                    f"session_note_reminder: {_user_message_counter} user messages "
                    f"processed this session. Spawn session-note-appender in the background "
                    f"to append a timestamped activity snapshot to the current session file."
                ),
                "user_message_count": _user_message_counter,
                "timestamp": now_utc.isoformat(),
            }
            inbox_file = INBOX_DIR / f"{reminder_id}.json"
            atomic_write_json(inbox_file, reminder)
            log.info(
                f"session_note_reminder injected after {_user_message_counter} user messages"
            )
        except Exception as _snr_exc:
            log.warning(f"session_note_reminder injection failed: {_snr_exc}")
            # Non-fatal: the session note will be written at compaction regardless.



# ---------------------------------------------------------------------------
# Formal message type taxonomy (issue #156)
# Definitions live in message_types.py (dependency-free, independently testable).
# Re-exported here so callers import from a single place.
# ---------------------------------------------------------------------------
# Explicit path guard: message_types.py lives in src/mcp/ alongside this file.
# When this script is run directly, Python adds src/mcp/ to sys.path automatically,
# but we make it explicit here so the import is not silently broken if the
# launch mechanism ever changes (e.g. subprocess, importlib, test harness).
_MCP_DIR = str(Path(__file__).resolve().parent)
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)
from message_types import (  # noqa: E402 — placed after path-setup at top of file
    INBOX_USER_TYPES,
    INBOX_SYSTEM_TYPES,
    INBOX_MESSAGE_TYPES,
    INBOX_MESSAGE_SOURCES,
    USER_FACING_TYPES,
)

# ---------------------------------------------------------------------------
# Message type normalization (issue #635)
# Aliases are resolved at ingest so the rest of the system only sees canonical
# names. Adding a new alias here is the only change needed to support a new
# producer that uses a legacy or non-standard type string.
# ---------------------------------------------------------------------------
TYPE_ALIASES: dict[str, str] = {
    "message": "text",
    "audio": "voice",
    "image": "photo",
    "cron_reminder": "scheduled_reminder",
    "task-output": "health_check",
    "system": "health_check",  # when type="system" from health check scripts
}


def normalize_message_type(msg: dict) -> dict:
    """Return msg with the type field normalized to its canonical name.

    Pure function: returns a new dict (immutable input contract); logs alias
    resolution at DEBUG level so normalization is traceable without being noisy.
    """
    t = msg.get("type", "text")
    if t in TYPE_ALIASES:
        log.debug("normalizing type %r -> %r", t, TYPE_ALIASES[t])
        msg = {**msg, "type": TYPE_ALIASES[t]}
    return msg


# Heartbeat file for health monitoring
HEARTBEAT_FILE = _WORKSPACE / "logs" / "claude-heartbeat"

# How often (in seconds) wait_for_messages touches heartbeats and refreshes
# WFM-active during the blocking wait.  Exposed as a module-level constant so
# tests can verify it matches the health-check's WFM_ACTIVE_STALE_SECONDS.
WAIT_HEARTBEAT_INTERVAL = 60

# WFM-active signal file (issue #1713 / #949): written with a Unix epoch
# timestamp when wait_for_messages begins blocking, refreshed every
# WAIT_HEARTBEAT_INTERVAL seconds, and deleted when WFM returns.
#
# The health check reads this file to distinguish "dispatcher alive, waiting
# for messages" from "dispatcher frozen/dead" — suppressing the
# heartbeat-stale RED that would otherwise fire after 20 minutes of quiet.
#
# Path: ~/lobster-workspace/logs/dispatcher-wfm-active
# Content: single Unix epoch integer (e.g. "1713456789\n"), same format as
#          dispatcher-heartbeat so health-check-v3.sh can parse it identically.
# Override: LOBSTER_WFM_ACTIVE_OVERRIDE env var (used in tests).
WFM_ACTIVE_FILE = Path(
    os.environ.get(
        "LOBSTER_WFM_ACTIVE_OVERRIDE",
        str(_WORKSPACE / "logs" / "dispatcher-wfm-active"),
    )
)

# Hibernation state file - tracks whether Lobster is active or hibernating
LOBSTER_STATE_FILE = CONFIG_DIR / "lobster-state.json"

# Dispatcher PID file — written by claude-persistent.sh when it exec-replaces
# itself with the claude binary.  The file contains the PID of the running
# claude process.  Used by _is_dispatcher_alive() to distinguish a true
# mid-session MCP reconnect (dispatcher alive) from a real session loss
# (dispatcher dead or absent).  See issue #1429.
DISPATCHER_PID_FILE = CONFIG_DIR / "dispatcher.pid"

# Reset state to "active" on startup — this is the fix for the critical bug where
# state was never reset after waking from hibernation.
# The bot issues systemctl restart → Claude starts → this module loads → state resets.
#
# Also resets transient states (starting, restarting, waking) to "active" because:
# - The MCP server is a subprocess of Claude; if we are loading, Claude is running.
# - A "starting" state from claude-persistent.sh's launch_claude() is superseded
#   once Claude is up and the MCP server has initialised.
# - This prevents the health-check from triggering a restart loop when the state
#   file is left in a transient mode (e.g. when using claude-wrapper.exp which
#   does not itself write the state file).
#
# Guard: skip the reset if state is already "active" and the file was written
# within the last 30 minutes.  That pattern indicates a mid-session MCP
# reconnect (e.g. Claude Code auto-updating), not a fresh boot.  Without this
# guard, the reconnect path re-writes booted_at/woke_at, confusing the health
# check about session age and causing unnecessary restarts (issue #910).
_TRANSIENT_MODES = {"hibernate", "starting", "restarting", "waking"}
_RECONNECT_GRACE_SECONDS = 30 * 60  # 30 minutes


def _is_dispatcher_alive() -> bool:
    """Return True if the dispatcher process recorded in dispatcher.pid is alive.

    Reads DISPATCHER_PID_FILE (written by claude-persistent.sh via exec-replace).
    Uses kill -0 to test liveness without sending a signal.

    Returns False when:
    - The file is absent (first boot, or cleaned up on clean exit)
    - The file is empty or contains a non-numeric value (corrupt)
    - The PID is 0 or negative (invalid)
    - kill -0 raises PermissionError or ProcessLookupError (dead process)

    A True return means the claude process is still alive → this MCP restart
    is a mid-session reconnect, NOT a real session loss.

    A False return means the dispatcher is dead → write the session-lost-reminder.
    """
    try:
        if not DISPATCHER_PID_FILE.exists():
            return False
        raw = DISPATCHER_PID_FILE.read_text().strip()
        if not raw:
            return False
        pid = int(raw)
        if pid <= 0:
            return False
        os.kill(pid, 0)  # kill -0: raises if process does not exist
        return True
    except (ValueError, ProcessLookupError, OSError):
        return False


def _reset_state_on_startup():
    try:
        if LOBSTER_STATE_FILE.exists():
            data = json.loads(LOBSTER_STATE_FILE.read_text())
            mode = data.get("mode", "active")

            # Skip reset for reconnects: if mode is already "active" and the
            # state file is fresh (< 30 min old), this is a mid-session MCP
            # restart, not a fresh dispatcher boot.  Rewriting booted_at/woke_at
            # would make the health check think this is a brand-new session and
            # apply incorrect freshness thresholds.
            if mode not in _TRANSIENT_MODES:
                try:
                    file_age = time.time() - LOBSTER_STATE_FILE.stat().st_mtime
                    if file_age < _RECONNECT_GRACE_SECONDS:
                        return  # Mid-session reconnect — leave state untouched
                except OSError:
                    pass  # Can't stat — fall through to normal reset

            if mode in _TRANSIENT_MODES:
                data["mode"] = "active"
                data["woke_at"] = datetime.now(timezone.utc).isoformat()
                tmp = LOBSTER_STATE_FILE.parent / f".lobster-state-{os.getpid()}.tmp"
                tmp.write_text(json.dumps(data, indent=2))
                tmp.rename(LOBSTER_STATE_FILE)
    except Exception:
        pass  # If we can't reset, _read_lobster_state defaults to "active" anyway


_reset_state_on_startup()

# Repo and config directories
_REPO_DIR = Path(os.environ.get("LOBSTER_INSTALL_DIR", Path.home() / "lobster"))
_CONFIG_DIR = Path(os.environ.get("LOBSTER_CONFIG_DIR", Path.home() / "lobster-config"))

# Structural guard: workspace must never be inside a git repo
from path_guard import assert_not_in_git_repo as _assert_not_in_git_repo
_assert_not_in_git_repo(_WORKSPACE)

# Scheduled Tasks Directories (task definitions live in workspace, not the repo)
SCHEDULED_JOBS_DIR = _WORKSPACE / "scheduled-jobs"
SCHEDULED_TASKS_TASKS_DIR = SCHEDULED_JOBS_DIR / "tasks"
# NOTE: SCHEDULED_JOBS_FILE is retained for the MCP tool handlers that still
# read/write jobs.json. It will be removed once PR #1105 (systemd backend) merges
# and the handlers are migrated to systemd_jobs.py.
SCHEDULED_JOBS_FILE = SCHEDULED_JOBS_DIR / "jobs.json"
SCHEDULED_TASKS_LOGS_DIR = SCHEDULED_JOBS_DIR / "logs"

# Canonical memory directory (user-config)
CANONICAL_DIR = _USER_CONFIG / "memory" / "canonical"

# Ensure directories exist
for d in [INBOX_DIR, OUTBOX_DIR, PROCESSED_DIR, PROCESSING_DIR, FAILED_DIR, SENT_DIR, SENT_REPLIES_DIR,
          TASK_REPLIED_DIR, CONFIG_DIR, AUDIO_DIR, TASK_OUTPUTS_DIR, BISQUE_OUTBOX_DIR,
          SCHEDULED_TASKS_TASKS_DIR, SCHEDULED_JOBS_DIR, SCHEDULED_TASKS_LOGS_DIR, CANONICAL_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Logging
LOG_DIR = _WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("lobster-mcp")
log.setLevel(logging.INFO)
# StreamHandler added unconditionally so log output is visible in tests and in
# production.  The RotatingFileHandler is added only when the server is started
# as __main__ (via setup_logging()), so importing this module in tests does NOT
# create or write to the production mcp-server.log file.
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(JsonFormatter("inbox_server"))
log.addHandler(_stream_handler)


def setup_logging() -> None:
    """Attach the production RotatingFileHandler to the lobster-mcp logger.

    Called only from main() so that importing inbox_server in tests does not
    create or write to the production log file at LOG_DIR / "mcp-server.log".
    Calling this function more than once is idempotent — configure_file_handler
    checks whether a RotatingFileHandler is already attached before adding
    another.
    """
    configure_file_handler(
        log,
        component="inbox_server",
        log_dir=LOG_DIR,
        filename="mcp-server.log",
    )

# Seed canonical templates on startup (idempotent — only copies missing files)
def _seed_canonical_templates():
    """Copy missing canonical template files from repo into workspace.

    Skips example-* files (they're reference templates only).
    Never overwrites existing files.
    """
    templates_dir = _REPO_DIR / "memory" / "canonical-templates"
    if not templates_dir.is_dir():
        return
    for src in templates_dir.rglob("*.md"):
        if src.name.startswith("example-"):
            continue
        rel = src.relative_to(templates_dir)
        dest = CANONICAL_DIR / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(str(src), str(dest))
            log.info(f"Seeded canonical template: {rel}")

_seed_canonical_templates()

# Initialize audit log for structured observability
init_audit_log(LOG_DIR)

# OpenAI configuration for Whisper transcription
# Try environment first, then fall back to config file
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    config_file = _CONFIG_DIR / "config.env"
    if config_file.exists():
        for line in config_file.read_text().splitlines():
            if line.strip().startswith("OPENAI_API_KEY="):
                OPENAI_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

# Initialize tasks file if needed
if not TASKS_FILE.exists():
    TASKS_FILE.write_text(json.dumps({"tasks": [], "next_id": 1}, indent=2))

# Record the moment this server process started. Used by stale-session cleanup
# to distinguish output files from the current run vs a previous (dead) run.
_SERVER_START_TIME = datetime.now(timezone.utc)

# ---------------------------------------------------------------------------
# Inbox Flood Detection (issue #1420)
# ---------------------------------------------------------------------------
#
# When the dispatcher starts (or reconnects after an MCP restart), the inbox
# can receive a large burst of stale messages — most commonly reconciler ghost
# entries: subagent_result messages with elapsed_seconds < 30 generated during
# the startup sweep. Each one requires a full WFM cycle to drain, burning
# tokens and delaying real user messages.
#
# Strategy: scan for known-safe flood patterns before returning messages to
# the dispatcher, bulk-mark-processed them on the server side, and report the
# drain count as a prefix so the dispatcher can log the event.
#
# Known-safe flood types (auto-drain without dispatcher deliberation):
#   1. Reconciler ghost sweep — subagent_result with elapsed_seconds < GHOST_ELAPSED_THRESHOLD
#      AND message arrived within STARTUP_WINDOW_SECONDS of server start.
#      These are stale completion notices for sessions that were dead before
#      the server restarted; the dispatcher can never act on them meaningfully.
#
# Unknown floods are NOT auto-drained — they pass through normally so the
# dispatcher can decide. The dispatcher bootup instructions handle alerting.
#
# Thresholds (named constants so the spec is traceable in tests):
GHOST_ELAPSED_THRESHOLD_SECONDS = 30   # subagent_result with <30s elapsed = ghost candidate
STARTUP_WINDOW_SECONDS = 60            # messages within 60s of server start = startup sweep


def _is_reconciler_ghost(msg: dict, server_start: datetime) -> bool:
    """Return True if this inbox message is a reconciler startup ghost.

    A ghost is a subagent_result that:
      - Has elapsed_seconds below GHOST_ELAPSED_THRESHOLD_SECONDS (30s), indicating
        the reconciler detected the session as dead in the same sweep that created
        the notification — no real work was done.
      - Was created within STARTUP_WINDOW_SECONDS (60s) of server start, indicating
        it is part of the startup sweep rather than real in-session activity.

    Pure function — reads msg and compares to server_start. No I/O.
    """
    if msg.get("type") != "subagent_result":
        return False

    # Check elapsed_seconds — ghosts have near-zero elapsed time
    try:
        elapsed = int(msg.get("elapsed_seconds", 0) or 0)
    except (TypeError, ValueError):
        elapsed = 0
    if elapsed >= GHOST_ELAPSED_THRESHOLD_SECONDS:
        return False  # Real work was done — not a ghost

    # Check timestamp — ghosts are generated during the startup sweep
    ts_str = msg.get("timestamp", "")
    if not ts_str:
        return False
    try:
        msg_time = datetime.fromisoformat(ts_str)
        if msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False

    age_seconds = (msg_time - server_start).total_seconds()
    return 0 <= age_seconds <= STARTUP_WINDOW_SECONDS


def _drain_reconciler_ghosts() -> int:
    """Scan inbox for reconciler startup ghosts and move them to processed/.

    Returns the count of messages drained. Side effect: moves matching inbox
    files from INBOX_DIR to PROCESSED_DIR and logs a single summary line.

    Called once at the start of each wait_for_messages call, so any ghost
    accumulation from the startup sweep is cleared before the dispatcher sees
    the inbox. Safe to call repeatedly — non-ghost messages are untouched.
    """
    drained = 0
    errors = 0

    for inbox_file in list(INBOX_DIR.glob("*.json")):
        try:
            msg = json.loads(inbox_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if not _is_reconciler_ghost(msg, _SERVER_START_TIME):
            continue

        # Move to processed/ — bypasses the user-reply guard because these are
        # internal system messages (chat_id=0 or no user) with nothing to relay.
        try:
            dest = PROCESSED_DIR / inbox_file.name
            inbox_file.rename(dest)
            drained += 1
        except OSError as exc:
            log.warning(
                "[flood-drain] Failed to move ghost %r to processed/: %s",
                inbox_file.name, exc,
            )
            errors += 1

    if drained > 0:
        log.info(
            "[flood-drain] Drained %d reconciler ghost(s) from startup sweep "
            "(elapsed<%ds within %ds of server start); %d error(s)",
            drained, GHOST_ELAPSED_THRESHOLD_SECONDS, STARTUP_WINDOW_SECONDS, errors,
        )

    return drained


# ---------------------------------------------------------------------------
# HTTP session identity — dispatcher session tagging (Options A and B)
# ---------------------------------------------------------------------------
#
# When running in HTTP transport mode, multiple Claude Code sessions can
# connect simultaneously (dispatcher + subagents). The server needs to know
# which session is the dispatcher so that:
#   - guarded tools (wait_for_messages, send_reply, etc.) are allowed only
#     for the dispatcher session
#   - health checks and the reconciler can verify the dispatcher is connected
#
# Session ID propagation:
#   Each HTTP request carries an "mcp-session-id" header (set by the MCP
#   client after the initial handshake).  The MCP library stores the raw
#   Starlette Request object in the per-request RequestContext
#   (request_ctx.get().request).  Tool handlers running inside the session
#   task can retrieve the session ID by reading that header:
#
#     _get_current_http_session_id()  →  request_ctx.get().request.headers[...]
#
#   This works because request_ctx is a ContextVar set in the session task
#   itself (by mcp.server.lowlevel.server._handle_request) before the tool
#   handler is called.
#
# Option A — tag-on-first-use:
#   The first call to wait_for_messages (dispatcher-exclusive) records the
#   current session ID as the dispatcher session.  On CC reconnect after a
#   restart, the tag is updated automatically so the new session is tracked.
#
# Option B — explicit declaration:
#   When a session calls session_start(agent_type="dispatcher", ...) the
#   current session ID is immediately recorded.  This is explicit and robust
#   — it fires before any guarded tool is needed.
#
# Both layers coexist; whichever fires first wins.  A subsequent call from
# either path updates the tag (CC restart recovery).
#
# _dispatcher_session_id: module-level str, set by Option A or B
# _http_session_manager: reference to the StreamableHTTPSessionManager
#   instance (set in main() when HTTP mode is active).  Non-None iff in HTTP
#   mode.  Used by /dispatcher-session endpoint and by _dispatch_tool to
#   select the correct guard path.
# ---------------------------------------------------------------------------

_dispatcher_session_id: str | None = None
_http_session_manager = None  # Set to StreamableHTTPSessionManager in main() when HTTP mode is active

# State file: the dispatcher HTTP session ID persisted to disk so hooks can
# read it without network calls or JSONL parsing.  Written atomically by
# _tag_dispatcher_session(); cleared at server startup.
_DISPATCHER_SESSION_STATE_FILE = _WORKSPACE / "data" / "dispatcher-session-id"

# Companion state file: the dispatcher *Claude* session UUID (format:
# xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx) written when the dispatcher calls
# session_start(agent_type='dispatcher', claude_session_id=<uuid>).  This is
# the ID that Claude Code SessionStart hooks receive in hook_input["session_id"],
# which is a different ID space from the HTTP transport session ID above.
# Having both files allows hooks to match whichever ID format they receive.
_DISPATCHER_CLAUDE_SESSION_FILE = _WORKSPACE / "data" / "dispatcher-claude-session-id"


def _write_dispatcher_state_file(session_id: str) -> None:
    """Atomically write session_id to the dispatcher state file.

    Uses a temp-file + os.rename() for atomicity so concurrent readers never
    see a partial write.  Silent on any failure — must never crash the caller.
    """
    try:
        _DISPATCHER_SESSION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DISPATCHER_SESSION_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(session_id.strip())
        tmp.replace(_DISPATCHER_SESSION_STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


def _write_dispatcher_claude_session_file(claude_session_id: str) -> None:
    """Atomically write the Claude session UUID to the dispatcher claude-session file.

    Called by handle_session_start when agent_type='dispatcher' and a
    claude_session_id is provided.  This file is read by SessionStart hooks
    which receive the Claude UUID (not the HTTP session ID) in hook_input.

    Uses a temp-file + os.rename() for atomicity.  Silent on any failure.
    """
    try:
        _DISPATCHER_CLAUDE_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DISPATCHER_CLAUDE_SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(claude_session_id.strip())
        tmp.replace(_DISPATCHER_CLAUDE_SESSION_FILE)
        log.info(f"[session-tag] Wrote dispatcher Claude session UUID: {claude_session_id!r}")
    except Exception:  # noqa: BLE001
        pass


def _clear_dispatcher_state_file() -> None:
    """Remove the dispatcher state files on server startup.

    Prevents stale session IDs from a previous run from being mistaken for
    the current dispatcher session.  Silent on any failure.
    """
    try:
        if _DISPATCHER_SESSION_STATE_FILE.exists():
            _DISPATCHER_SESSION_STATE_FILE.unlink()
            log.info("[session-tag] Cleared stale dispatcher-session-id state file on startup")
    except Exception:  # noqa: BLE001
        pass
    try:
        if _DISPATCHER_CLAUDE_SESSION_FILE.exists():
            _DISPATCHER_CLAUDE_SESSION_FILE.unlink()
            log.info("[session-tag] Cleared stale dispatcher-claude-session-id state file on startup")
    except Exception:  # noqa: BLE001
        pass


def _write_session_lost_reminder() -> None:
    """Write a compact-reminder to the inbox on MCP server startup.

    When the MCP server restarts (e.g. via systemctl restart lobster-mcp-local),
    any dispatcher blocked in wait_for_messages receives a "Session not found"
    error (-32600) at the protocol level with no recovery guidance.  This
    function writes a synthetic inbox message so that when the dispatcher
    reconnects and calls wait_for_messages, it immediately receives a prompt
    to re-orient and resume the main loop.

    Guard: skipped when the dispatcher PID (from dispatcher.pid) is alive.  A
    live PID means Claude Code auto-updated or the HTTP transport briefly
    cycled — the dispatcher process is still running and does not need to
    re-orient.  A dead or absent PID means real session loss.

    Previous guard (issue #1429 — removed): checked lobster-state.json mtime
    < 30 min.  This produced false negatives when cron jobs touched the state
    file seconds before an MCP restart, making a real session loss look like a
    reconnect.

    Idempotent: writing an extra compact-reminder during a real restart is
    harmless; the dispatcher processes it as a routine self-check message.

    Developer mode: when LOBSTER_DEV_MODE=true (or 1), the session-lost
    reminder is suppressed so the developer isn't interrupted during testing.
    """
    # Developer mode: suppress session-lost reminders during development.
    if os.environ.get("LOBSTER_DEV_MODE", "").lower() in ("true", "1"):
        log.info("[session-lost] LOBSTER_DEV_MODE active — skipping session-lost-reminder")
        return
    try:
        # PID-based reconnect guard (issue #1429).
        #
        # Previous guard (broken): checked lobster-state.json mtime < 30 min.
        # Problem: cron jobs can touch lobster-state.json moments before an MCP
        # restart, making the file look fresh even when the dispatcher is dead.
        #
        # New guard: check whether the dispatcher PID (from dispatcher.pid) is
        # still alive via kill -0.  A live PID means Claude Code auto-updated
        # its transport layer — the dispatcher is still running.  A dead or
        # absent PID means the dispatcher process is gone — real session loss.
        if _is_dispatcher_alive():
            log.info(
                "[session-lost] Skipping session-lost-reminder: dispatcher PID is alive "
                f"(pid_file={DISPATCHER_PID_FILE})"
            )
            return

        now_utc = datetime.now(timezone.utc)
        reminder_id = f"session-lost-{int(now_utc.timestamp())}"
        reminder = {
            "id": reminder_id,
            "source": "system",
            "type": "compact-reminder",
            "chat_id": 0,
            "task_origin": "internal",
            "text": (
                "SESSION LOST — The MCP server restarted and your previous session was "
                "invalidated. Re-orient now: read sys.dispatcher.bootup.md and resume "
                "the main loop."
            ),
            "timestamp": now_utc.isoformat(),
        }
        inbox_file = INBOX_DIR / f"{reminder_id}.json"
        atomic_write_json(inbox_file, reminder)
        log.info(f"[session-lost] Wrote session-lost-reminder to inbox: {reminder_id}")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[session-lost] Failed to write session-lost-reminder: {exc}")


def _get_current_http_session_id() -> str | None:
    """Return the MCP session ID for the current tool call request.

    Reads the 'mcp-session-id' header from the Starlette Request object
    stored in the MCP request context.  Returns None if:
      - not running in HTTP mode
      - called outside of an active MCP request (e.g. during startup)
      - the header is absent (first initialise request has no session ID)
    """
    try:
        from mcp.server.lowlevel.server import request_ctx
        req_ctx = request_ctx.get()
        raw_request = req_ctx.request  # Starlette Request or None
        if raw_request is None:
            return None
        return raw_request.headers.get("mcp-session-id")
    except LookupError:
        # request_ctx not set — called outside a request context
        return None
    except Exception:
        return None


def _tag_dispatcher_session(session_id: str) -> None:
    """Record session_id as the privileged dispatcher session.

    Called by Option A (handle_wait_for_messages), Option B
    (handle_session_start with agent_type="dispatcher"), and Option C
    (auto-tag on first guarded tool call after server restart).  Whichever
    fires first wins; both paths update on reconnect.

    Also writes the session_id to the dispatcher state file
    (_DISPATCHER_SESSION_STATE_FILE) so hooks can read the current dispatcher
    session without network calls or JSONL parsing.
    """
    global _dispatcher_session_id
    if session_id and session_id != _dispatcher_session_id:
        _dispatcher_session_id = session_id
        log.info(f"[session-tag] Dispatcher session tagged: {session_id}")
        _write_dispatcher_state_file(session_id)


def _is_main_http_session() -> bool:
    """Return True if the current HTTP session is the tagged dispatcher session.

    Only meaningful when running in HTTP transport mode (_http_session_manager
    is not None).  Fails closed: returns False if no session is tagged or if
    the current session ID cannot be determined.
    """
    if _dispatcher_session_id is None:
        return False
    current = _get_current_http_session_id()
    return current is not None and current == _dispatcher_session_id

# Initialize SQLite agent session store (idempotent, runs JSON migration on first boot)
try:
    _session_store.init_db()
    log.info("Agent session store initialized (SQLite WAL mode)")
except Exception as _ss_err:
    log.warning(f"Agent session store init failed (non-fatal): {_ss_err}")

# Initialize atomic claim DB — creates message_claims and dispatcher_lock tables
# in the existing agent_sessions.db. Idempotent.
try:
    _claims_db = _AtomicClaimDB()
    log.info("Atomic claim DB initialized (message_claims + dispatcher_lock tables)")
except Exception as _claims_err:
    # Degrade gracefully: create a no-op stub so the rest of the module
    # continues to work even if the DB cannot be opened.
    log.warning(f"Atomic claim DB init failed — degrading to filesystem-only claims: {_claims_err}")
    class _NoOpClaimsDB:  # type: ignore[no-redef]
        def claim(self, *a, **kw) -> bool: return True
        def release(self, *a, **kw) -> None: pass
        def update_status(self, *a, **kw) -> None: pass
        def is_claimed(self, *a, **kw) -> bool: return False
        def acquire_dispatcher_lock(self, *a, **kw) -> bool: return True
        def get_dispatcher_lock(self, *a, **kw): return None
        def release_dispatcher_lock(self, *a, **kw) -> None: pass
        def force_replace_dispatcher_lock(self, *a, **kw) -> None: pass
    _claims_db = _NoOpClaimsDB()

# NOTE: Startup cleanup (cleanup_stale_running_sessions) is intentionally NOT
# called here at module level. This module is imported by inbox_server_http.py
# (the HTTP bridge) which may be run as a separate process. Running the cleanup
# at import time would incorrectly mark running agents as dead every time the
# HTTP server restarts. The cleanup runs inside main() so it only fires when
# inbox_server.py is the actual stdio MCP server entry point.

# ---------------------------------------------------------------------------
# Wire server notification — event-driven SSE push (<40ms latency)
# ---------------------------------------------------------------------------

_WIRE_SERVER_NOTIFY_URL = os.environ.get(
    "LOBSTER_WIRE_NOTIFY_URL",
    f"http://localhost:{os.environ.get('LOBSTER_WIRE_PORT', '8765')}/notify",
)


async def _notify_wire_server() -> None:
    """Fire-and-forget POST to wire server /notify endpoint.

    Called after every session write so the wire server wakes its SSE generators
    immediately instead of waiting for the next poll interval.  Silently swallowed
    on any error — the wire server will still catch the change on its next fallback
    poll (LOBSTER_WIRE_POLL_INTERVAL, default 0.5s).
    """
    try:
        async with httpx.AsyncClient() as client:
            await client.post(_WIRE_SERVER_NOTIFY_URL, timeout=0.15)
    except Exception:
        pass  # wire server may be down or not yet started — not critical


# Source configurations
SOURCES = {
    "telegram": {
        "name": "Telegram",
        "enabled": True,
    },
    "slack": {
        "name": "Slack",
        "enabled": True,
    },
    "sms": {
        "name": "SMS",
        "enabled": True,
    },
    "whatsapp": {
        "name": "WhatsApp",
        "enabled": True,
    },
    "bisque": {
        "name": "Bisque",
        "enabled": True,
    },
}

server = Server("lobster-inbox")


def touch_heartbeat():
    """Touch heartbeat file to signal Claude is alive and processing."""
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.touch()
    except Exception:
        pass  # Don't fail on heartbeat errors


def _write_wfm_active_signal() -> None:
    """Write the WFM-active heartbeat signal atomically (issue #1713 / #949).

    Called at the start of the wait_for_messages blocking loop and refreshed
    on every WAIT_HEARTBEAT_INTERVAL tick so the health check sees a fresh
    signal throughout the entire WFM blocking period.

    Content: single Unix epoch integer — same format as dispatcher-heartbeat —
    so health-check-v3.sh can parse it with the same integer comparison logic.

    Atomic write (tmp -> rename) prevents partial reads by the health check.
    Silently swallowed on failure: health check degrades gracefully when absent.
    """
    try:
        WFM_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WFM_ACTIVE_FILE.with_name(f".wfm-active-{os.getpid()}.tmp")
        tmp.write_text(str(int(time.time())) + "\n")
        os.rename(str(tmp), str(WFM_ACTIVE_FILE))
    except Exception:
        pass  # Never block wait_for_messages on a heartbeat write failure


def _wfm_heartbeat_thread_fn(stop_event: threading.Event, interval: int) -> None:
    """Daemon thread that refreshes the WFM-active heartbeat on wall-clock time.

    Runs independently of the asyncio event loop so that heartbeat writes
    continue even when the loop is stalled (e.g. during Claude API streaming).
    Fires every `interval` seconds until stop_event is set.

    Exceptions are swallowed so the thread never crashes the process.
    daemon=True on the spawning site guarantees this thread dies with the
    process — no fake heartbeats survive a crash or unexpected exit.
    """
    while not stop_event.wait(timeout=interval):
        try:
            touch_heartbeat()
            _write_wfm_active_signal()
        except Exception:
            pass  # Never crash the heartbeat thread


# Tombstone value written to WFM_ACTIVE_FILE when WFM exits (Fix 2, issue #1730).
# Using a non-integer string means:
#   - The file is NEVER absent between the health check's -f gate and its cat read
#     (closing the TOCTOU race that caused the 2026-04-22 00:00Z false restart).
#   - The health check's existing integer guard ([[ "$ts" =~ ^[0-9]+$ ]]) rejects
#     this value and treats it as "WFM not active" — the correct semantic.
WFM_ACTIVE_TOMBSTONE = "exited"


def _clear_wfm_active_signal() -> None:
    """Write a tombstone to WFM_ACTIVE_FILE instead of deleting it (issue #1730).

    Replaces the previous WFM_ACTIVE_FILE.unlink() in the WFM finally block.
    Writing a non-integer tombstone ("exited") instead of deleting ensures the
    file is never transiently absent — closing the TOCTOU race between:
      1. The health check's existence test  (if [[ -f ... ]])
      2. The health check's content read    (cat ...)
    If the file disappears between those two operations, cat returns empty and
    the health check falls through to RED, triggering a false-positive restart.

    The tombstone is parsed by the health check's integer regex guard and treated
    as absent (WFM not active), which is the correct semantic.

    Silently swallowed on failure: this runs in a finally block and must never
    mask the original exception that caused WFM to exit.
    """
    try:
        WFM_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WFM_ACTIVE_FILE.write_text(WFM_ACTIVE_TOMBSTONE + "\n")
    except Exception:
        pass  # Never propagate errors from a finally-block cleanup


# ---------------------------------------------------------------------------
# Session Guard
#
# Only the designated tmux "lobster" session is permitted to monitor the inbox
# or write to the outbox. Interactive SSH Claude sessions must be blocked from
# calling these tools to prevent dual-processing of messages.
#
# Detection strategy (two-layer):
#   Primary: tmux ancestry walk — verifies the MCP server is a descendant of a
#     pane in the "lobster" tmux session. This is unforgeable: process ancestry
#     cannot leak across sessions. Works whether the session was started via
#     claude-persistent.sh or claude-wrapper.exp (both end up as direct children
#     of the tmux pane process — expect's spawn() creates no intermediate PID).
#   Fallback: LOBSTER_MAIN_SESSION=1 env var — set by both claude-persistent.sh
#     and claude-wrapper.exp before spawning Claude. Covers edge cases where the
#     tmux session name differs or pane_pid lookup fails.
#
# Tools that are BLOCKED for non-main sessions:
#   wait_for_messages, check_inbox, send_reply, send_whatsapp_reply,
#   send_sms_reply, mark_processed, mark_processing, mark_failed
#
# Read-only / utility tools (get_stats, list_sources, etc.) are always allowed.
# ---------------------------------------------------------------------------

_SESSION_GUARDED_TOOLS = frozenset({
    "wait_for_messages",
    "check_inbox",
    "send_reply",
    "send_whatsapp_reply",
    "send_sms_reply",
    "mark_processed",
    "mark_processing",
    "mark_failed",
    "claim_and_ack",
})


_main_session_cache: bool | None = None


def _check_tmux_ancestry() -> bool:
    """Walk the process tree to check if this MCP server is a descendant of
    a pane in the 'lobster' tmux session. This is unforgeable — unlike env
    vars, process ancestry cannot leak across sessions."""
    try:
        import subprocess
        result = subprocess.run(
            ["tmux", "-L", LOBSTER_TMUX_SESSION, "list-panes", "-t", LOBSTER_TMUX_SESSION,
             "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        tmux_pids = set(result.stdout.strip().split("\n"))
        pid = os.getpid()
        for _ in range(10):
            if str(pid) in tmux_pids:
                return True
            try:
                with open(f"/proc/{pid}/stat") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            except (FileNotFoundError, ValueError, IndexError):
                break
            if ppid <= 1:
                break
            pid = ppid
    except Exception:
        pass
    return False  # Fail closed — not the main session


def _is_main_session() -> bool:
    """Return True if this MCP server instance is running inside the designated
    main Lobster tmux session.

    Primary check: walks the process tree to verify ancestry in the lobster
    tmux session. The result is cached — process ancestry never changes.

    Fails closed: if the tmux check fails for any reason, returns False.
    """
    global _main_session_cache
    if _main_session_cache is not None:
        return _main_session_cache
    # Primary: tmux ancestry (unforgeable)
    if _check_tmux_ancestry():
        _main_session_cache = True
        log.info("Session guard: confirmed main session via tmux ancestry")
        return True
    # Fallback: env var (set by claude-wrapper.exp)
    result = os.environ.get("LOBSTER_MAIN_SESSION") == "1"
    _main_session_cache = result
    if result:
        log.info("Session guard: confirmed main session via LOBSTER_MAIN_SESSION env var")
    else:
        log.info("Session guard: NOT main session (tmux ancestry check failed, env var not set)")
    return result


def _session_guard_error(tool_name: str) -> list[TextContent]:
    """Return a clear error message when a guarded tool is called from a
    non-main session."""
    return [TextContent(
        type="text",
        text=(
            f"SESSION GUARD: '{tool_name}' is blocked in this session.\n\n"
            "Inbox monitoring and outbox writes are restricted to the main "
            "Lobster tmux session (started by claude-persistent.sh or claude-wrapper.exp).\n\n"
            "This Claude process is not a descendant of the lobster tmux "
            "session, so it is treated as an interactive/ad-hoc session.\n\n"
            "Read-only tools (get_stats, list_sources, memory_search, etc.) "
            "are still available."
        ),
    )]


def _read_lobster_state(state_file: Path = None) -> str:
    """Read the current Lobster state from state file.

    Returns 'active' or 'hibernate'. Defaults to 'active' if the file is
    missing, corrupt, or contains an unrecognised mode value.
    """
    if state_file is None:
        state_file = LOBSTER_STATE_FILE
    try:
        if not state_file.exists():
            return "active"
        data = json.loads(state_file.read_text())
        mode = data.get("mode", "active")
        return mode if mode in ("active", "hibernate") else "active"
    except Exception:
        return "active"


def _write_lobster_state(state_file: Path = None, mode: str = "active") -> None:
    """Atomically write Lobster state to state file.

    Reads existing state first and merges new fields in so that caller-supplied
    fields (compacted_at, booted_at, last_restart_at, catchup_started_at, etc.)
    are never clobbered — the bug tracked in #825.  Only ``mode`` and
    ``updated_at`` are unconditionally overwritten; all other fields survive.

    Uses write-to-temp-then-rename so readers never see a partial file.
    """
    if state_file is None:
        state_file = LOBSTER_STATE_FILE
    # Read existing state so we preserve fields we are not updating (#825).
    existing: dict = {}
    try:
        existing = json.loads(state_file.read_text())
    except Exception:
        pass
    existing.update({
        "mode": mode,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    content = json.dumps(existing, indent=2)
    tmp = state_file.parent / f".lobster-state-{os.getpid()}.tmp"
    try:
        tmp.write_text(content)
        tmp.rename(state_file)
    except Exception as e:
        log.error(f"Failed to write lobster state: {e}")
        try:
            tmp.unlink()
        except Exception:
            pass


def _update_lobster_state_fields(fields: dict, state_file: Path = None) -> None:
    """Atomically merge one or more fields into lobster-state.json.

    Reads the current state, updates the given fields, and writes back via
    temp-then-rename so readers always see a consistent snapshot.  Other
    fields (mode, compacted_at, catchup_started_at, etc.) are preserved.

    This is a non-destructive merge — callers supply only the keys they want
    to update.  Unlike _write_lobster_state, which replaces the entire file,
    this function is safe to call from any context that must not clobber
    concurrently-written lifecycle fields.

    Failures are logged but not raised — the caller (mark_processed) must
    not fail just because the heartbeat write failed.
    """
    if state_file is None:
        state_file = LOBSTER_STATE_FILE
    try:
        existing: dict = {}
        if state_file.exists():
            try:
                existing = json.loads(state_file.read_text())
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing.update(fields)
        content = json.dumps(existing, indent=2) + "\n"
        tmp = state_file.parent / f".lobster-state-{os.getpid()}.tmp"
        try:
            tmp.write_text(content)
            tmp.rename(state_file)
        except Exception as e:
            log.error(f"Failed to update lobster state fields {list(fields)}: {e}")
            try:
                tmp.unlink()
            except Exception:
                pass
    except Exception as e:
        log.error(f"_update_lobster_state_fields unexpected error: {e}")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="wait_for_messages",
            description="Block and wait for new messages to arrive. This is the core tool for the always-on loop. Returns immediately if messages exist, otherwise waits until a message arrives or timeout. Use this in your main loop: wait_for_messages -> process -> repeat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "integer",
                        "description": "Maximum seconds to wait. Default 72000 (20 hours). After timeout, returns with a prompt to call again.",
                        "default": 72000,
                    },
                    "hibernate_on_timeout": {
                        "type": "boolean",
                        "description": "If true, write hibernate state and signal graceful exit when timeout expires with no messages. Default false.",
                        "default": False,
                    },
                },
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        Tool(
            name="check_inbox",
            description="Check for new messages in the inbox from all sources (Telegram, SMS, Signal, etc.). Returns unprocessed messages. For the always-on loop, prefer wait_for_messages which blocks until messages arrive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Filter by source (telegram, sms, signal). Leave empty for all sources.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to return. Default 10.",
                        "default": 10,
                    },
                    "since_ts": {
                        "type": "string",
                        "description": (
                            "ISO 8601 UTC timestamp (e.g. '2026-01-01T12:00:00Z'). "
                            "When provided, only messages with a timestamp >= since_ts are returned. "
                            "Applies to processed/ and inbox/ directories. "
                            "Useful for catch-up agents that need to scan history after compaction."
                        ),
                    },
                },
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        Tool(
            name="send_reply",
            description="Send a reply to a message. The reply will be routed back to the original source (Telegram, Slack, SMS, etc.). Supports optional inline keyboard buttons for Telegram and thread replies for Slack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"}
                        ],
                        "description": "The chat/channel ID to reply to (from the original message). Integer for Telegram, string for Slack.",
                    },
                    "text": {
                        "type": "string",
                        "description": "The reply text to send.",
                    },
                    "source": {
                        "type": "string",
                        "description": "The source to reply via (telegram, slack, sms, signal, whatsapp, bisque). Default: telegram.",
                        "default": "telegram",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "Slack thread timestamp. If provided, reply will be sent as a thread reply. Get this from the original message's thread_ts or slack_ts field.",
                    },
                    "buttons": {
                        "type": "array",
                        "description": "Optional inline keyboard buttons (Telegram only). Format: [[\"Btn1\", \"Btn2\"], [\"Btn3\"]] for simple buttons (text=callback_data), or [[{\"text\": \"Label\", \"callback_data\": \"value\"}]] for explicit callback data.",
                        "items": {
                            "type": "array",
                            "description": "A row of buttons",
                            "items": {
                                "oneOf": [
                                    {"type": "string", "description": "Simple button (text is also callback_data)"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "text": {"type": "string", "description": "Button label"},
                                            "callback_data": {"type": "string", "description": "Data sent when pressed"}
                                        },
                                        "required": ["text"]
                                    }
                                ]
                            }
                        }
                    },
                    "reply_to_message_id": {
                        "type": "integer",
                        "description": "Telegram message ID to reply to (Telegram only). If provided, threads the reply against that specific message. If omitted, the reply is sent standalone (no threading).",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "If provided, atomically marks this message as processed after sending the reply. Combines send_reply + mark_processed into one call.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Subagent task identifier. When provided, the server records that this task has "
                            "already delivered a reply directly. If write_result is later called with the same "
                            "task_id, sent_reply_to_user is automatically set to True — preventing duplicate messages "
                            "even if the subagent forgets to pass sent_reply_to_user=True."
                        ),
                    },
                },
                "required": ["chat_id", "text"],
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        Tool(
            name="send_whatsapp_reply",
            description="Send a WhatsApp message via the Baileys bridge. Use this to reply to WhatsApp messages (source='whatsapp'). Requires lobster-whatsapp-bridge and lobster-whatsapp-adapter services to be running. No Twilio credentials needed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient phone number in E.164 format (e.g. <REDACTED_PHONE>). The 'whatsapp:' prefix will be added automatically.",
                    },
                    "text": {
                        "type": "string",
                        "description": "The message text to send.",
                    },
                },
                "required": ["to", "text"],
            },
        ),
        Tool(
            name="send_sms_reply",
            description="Send an SMS message via Twilio. Use this to reply to SMS messages (source='sms'). Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_SMS_NUMBER to be configured.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient phone number in E.164 format (e.g. <REDACTED_PHONE>).",
                    },
                    "text": {
                        "type": "string",
                        "description": "The message text to send.",
                    },
                },
                "required": ["to", "text"],
            },
        ),
        Tool(
            name="mark_processed",
            description="Mark a message as processed and move it out of the inbox. Checks processing/ first, then inbox/ as fallback.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID to mark as processed.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "If true, skip the reply-sent check and mark processed even if no reply was sent. Default false.",
                        "default": False,
                    },
                },
                "required": ["message_id"],
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        Tool(
            name="mark_processing",
            description="Claim a message for processing by moving it from inbox/ to processing/. Call this before starting work on a message to prevent reprocessing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID to claim for processing.",
                    },
                },
                "required": ["message_id"],
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        Tool(
            name="mark_failed",
            description="Mark a message as failed with optional retry. Messages are retried with exponential backoff (60s, 120s, 240s) up to max_retries times. After max retries, the message is permanently failed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID to mark as failed.",
                    },
                    "error": {
                        "type": "string",
                        "description": "Error description. Default: 'Unknown error'.",
                    },
                    "max_retries": {
                        "type": "integer",
                        "description": "Maximum number of retries before permanent failure. Default: 3.",
                        "default": 3,
                    },
                },
                "required": ["message_id"],
            },
        ),
        Tool(
            name="claim_and_ack",
            description=(
                "Atomically claim a message for processing and send an acknowledgement reply. "
                "Preferred over separate mark_processing + send_reply calls for long-running tasks: "
                "the user is notified in the same operation as the claim, so a crash after this call "
                "means the user already received the ack. "
                "If the message is not found in inbox/ (already claimed or missing), returns an error "
                "without sending the ack. If the ack send fails after claiming, the message stays in "
                "processing/ and stale recovery handles it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID to claim (must be in inbox/).",
                    },
                    "ack_text": {
                        "type": "string",
                        "description": "The acknowledgement text to send to the user (e.g. 'On it.').",
                    },
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "The chat/channel ID to send the ack to (from the original message).",
                    },
                    "source": {
                        "type": "string",
                        "description": "The source to reply via (telegram, slack, etc.). Default: telegram.",
                        "default": "telegram",
                    },
                    "reply_to_message_id": {
                        "type": "integer",
                        "description": "Telegram message ID to thread the ack against (Telegram only).",
                    },
                    "buttons": {
                        "type": "array",
                        "description": "Optional inline keyboard buttons (Telegram only). Same format as send_reply.",
                        "items": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "text": {"type": "string"},
                                            "callback_data": {"type": "string"},
                                        },
                                        "required": ["text"],
                                    },
                                ]
                            },
                        },
                    },
                },
                "required": ["message_id", "ack_text", "chat_id"],
            },
        ),
        Tool(
            name="list_sources",
            description="List all available message sources and their status.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_stats",
            description="Get inbox statistics: message counts, sources, etc.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # Conversation History Tool
        Tool(
            name="get_conversation_history",
            description="Retrieve past messages from conversation history - both received messages and sent replies. Supports pagination, filtering by chat_id, text search, and sender_type to isolate real conversation from system/cron noise. Use this to scroll back through previous conversations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"}
                        ],
                        "description": "Filter by chat ID to see conversation with a specific user. Leave empty for all conversations.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Search text to filter messages (case-insensitive). Searches in message text content.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to return. Default 20, max 100.",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of messages to skip (for pagination). Default 0. Messages are returned newest-first, so offset=0 gives the most recent messages.",
                        "default": 0,
                    },
                    "direction": {
                        "type": "string",
                        "description": "Filter by direction: 'received' for incoming messages only, 'sent' for outgoing replies only, or 'all' for both. Default 'all'. Ignored when sender_type is set.",
                        "default": "all",
                    },
                    "source": {
                        "type": "string",
                        "description": "Filter by source (telegram, slack, etc.). Leave empty for all sources.",
                    },
                    "sender_type": {
                        "type": "string",
                        "description": (
                            "Filter by who sent the message. "
                            "'user' — only messages from the user (inbound Telegram/Slack, excludes system/cron). "
                            "'lobster' — only messages sent by Lobster/subagents to the user (outbound). "
                            "'conversation' — both user and lobster messages (real conversation only, no cron/system noise). "
                            "Omit for all messages including system and cron (current default behaviour). "
                            "When sender_type is set, the direction parameter is ignored."
                        ),
                        "enum": ["user", "lobster", "conversation"],
                    },
                },
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        # Telegram Message Lookup Tool
        Tool(
            name="get_message_by_telegram_id",
            description="Look up a specific message by its Telegram message ID (the integer ID like 12824). Searches across all message directories (processed, inbox, processing, failed) and returns the full message object. Useful when the dispatcher needs to retrieve original message content for a known Telegram message ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "telegram_message_id": {
                        "type": "integer",
                        "description": "The Telegram message ID (integer) to look up.",
                    },
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "Optional: filter by chat ID to disambiguate when the same Telegram message ID might appear in multiple chats.",
                    },
                },
                "required": ["telegram_message_id"],
            },
        ),
        # Task Management Tools
        Tool(
            name="list_tasks",
            description="List all tasks with their status. Tasks are shared across all Lobster sessions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: pending, in_progress, completed, or all (default).",
                        "default": "all",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Optional: filter to tasks owned by this chat_id (legacy tasks without chat_id are always included).",
                    },
                },
            },
        ),
        Tool(
            name="create_task",
            description="Create a new task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Brief title for the task.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what needs to be done.",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Optional: chat_id of the user who owns this task.",
                    },
                },
                "required": ["subject"],
            },
        ),
        Tool(
            name="update_task",
            description="Update a task's status or details.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The task ID to update.",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status: pending, in_progress, or completed.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "New subject (optional).",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional).",
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="get_task",
            description="Get details of a specific task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The task ID to retrieve.",
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="delete_task",
            description="Delete a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The task ID to delete.",
                    },
                },
                "required": ["task_id"],
            },
        ),
        # IFTTT Behavioral Rules Tools
        Tool(
            name="list_rules",
            description="List all IFTTT-style behavioral rules from the rules store. Returns id, condition, action_ref, and enabled for each rule. Pass resolve=true to also include the memory DB content for each action_ref.",
            inputSchema={
                "type": "object",
                "properties": {
                    "enabled_only": {
                        "type": "boolean",
                        "description": "If true, return only enabled rules. Default false (returns all rules).",
                        "default": False,
                    },
                    "resolve": {
                        "type": "boolean",
                        "description": "If true, fetch and include the memory DB content for each action_ref. Default false.",
                        "default": False,
                    },
                },
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        Tool(
            name="add_rule",
            description="Add a new IFTTT-style behavioral rule. The condition is the IF clause in plain English. The action_content is the THEN clause (plain-English behavioral instruction) — it is stored to the memory DB automatically and the resulting DB entry ID is used as action_ref in the YAML index. Returns the new rule's ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "description": "Natural-language IF clause (one sentence, e.g. 'The user asks about a meeting or scheduling').",
                    },
                    "action_content": {
                        "type": "string",
                        "description": "Plain-English THEN clause (behavioral instruction). Stored to memory DB; the assigned DB entry ID becomes the action_ref.",
                    },
                },
                "required": ["condition", "action_content"],
            },
        ),
        Tool(
            name="delete_rule",
            description="Delete an IFTTT behavioral rule by ID. Returns true if deleted, false if not found. Pass delete_memory=true to also delete the memory DB entry for action_ref.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "description": "The ID of the rule to delete.",
                    },
                    "delete_memory": {
                        "type": "boolean",
                        "description": "If true, also delete the memory DB entry referenced by action_ref. Default false.",
                        "default": False,
                    },
                },
                "required": ["rule_id"],
            },
        ),
        Tool(
            name="get_rule",
            description="Get a single IFTTT behavioral rule by ID. Returns null if not found. Pass resolve=true to also include the memory DB content for action_ref.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "description": "The ID of the rule to retrieve.",
                    },
                    "resolve": {
                        "type": "boolean",
                        "description": "If true, fetch and include the memory DB content for action_ref. Default false.",
                        "default": False,
                    },
                },
                "required": ["rule_id"],
            },
        ),
        Tool(
            name="update_rule",
            description="Soft-disable or re-enable an IFTTT behavioral rule by ID. Returns the updated rule, or null if not found.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "description": "The ID of the rule to update.",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Set to false to soft-disable the rule (kept in store, never applied). Set to true to re-enable.",
                    },
                },
                "required": ["rule_id", "enabled"],
            },
        ),
        Tool(
            name="transcribe_audio",
            description="Transcribe a voice message to text using local whisper.cpp (small model). Use this for messages with type='voice'. Runs entirely locally using whisper.cpp - no cloud API or API key needed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message ID of the voice message to transcribe.",
                    },
                },
                "required": ["message_id"],
            },
        ),
        # TTS Voice Note Tool
        Tool(
            name="send_voice_note",
            description=(
                "Synthesize text to speech and send as a Telegram voice note using local piper TTS. "
                "Runs entirely locally — no cloud API or API key needed. "
                "Requires piper binary and lessac-medium voice model (installed by install.sh). "
                "Falls back to send_reply with text if TTS is unavailable. "
                "Use for responses that benefit from an audio/voice delivery."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The chat ID to send the voice note to (Telegram chat ID).",
                    },
                    "text": {
                        "type": "string",
                        "description": "The text to synthesize into a voice note. Keep under ~500 words for best results.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Message source channel. Defaults to 'telegram'. Voice notes are only sent for Telegram; other sources fall back to text.",
                        "default": "telegram",
                    },
                },
                "required": ["chat_id", "text"],
            },
        ),
        # Headless Browser Fetch Tool
        Tool(
            name="fetch_page",
            description="Fetch a web page using a headless browser (Playwright/Chromium). Renders JavaScript fully before extracting text content. Ideal for Twitter/X links, SPAs, and other JS-heavy pages. Returns cleaned text content, not raw HTML.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch. Will be loaded in a headless Chromium browser.",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Extra seconds to wait after page load for JS rendering. Default 3. Increase for slow-loading pages.",
                        "default": 3,
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Maximum seconds before giving up. Default 30.",
                        "default": 30,
                    },
                },
                "required": ["url"],
            },
        ),
        # Scheduled Jobs Tools (systemd timer backend)
        Tool(
            name="create_scheduled_job",
            description="Create a new scheduled job backed by a systemd timer unit. The command must be an absolute path to an executable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique name for the job (lowercase alphanumeric + hyphens, e.g., 'morning-weather').",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "Schedule for the job. Accepts systemd OnCalendar expressions (e.g., '*-*-* 09:00:00' for 9am daily, '*:0/30:00' for every 30 mins, 'daily', 'hourly') or standard 5-field cron expressions (e.g., '0 9 * * *') which are auto-converted to systemd format.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Absolute path to the executable to run (e.g., '/home/lobster/lobster/scheduled-tasks/my-job.sh').",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional human-readable description for the systemd unit.",
                    },
                },
                "required": ["name", "schedule", "command"],
            },
        ),
        Tool(
            name="list_scheduled_jobs",
            description="List all lobster-managed systemd timer jobs with their schedule, last run, next run, and active status.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_scheduled_job",
            description="Get detailed information about a specific lobster-managed systemd timer job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The job name to retrieve.",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="update_scheduled_job",
            description="Update an existing lobster-managed job's schedule, command, or enabled state. Rewrites the unit files and restarts the timer when schedule/command change.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The job name to update.",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "New schedule (systemd OnCalendar or cron expression — auto-converted). Optional.",
                    },
                    "command": {
                        "type": "string",
                        "description": "New absolute path command (optional).",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Set to false to pause/disable the timer without deleting it. Set to true to re-enable it. Optional.",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="delete_scheduled_job",
            description="Stop, disable, and remove the systemd unit files for a lobster-managed job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The job name to delete.",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="get_job_scaffold",
            description="Return a starter script template for writing a lobster scheduled job. Use this as a starting point before creating a new job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Template kind: 'poller' (default). Returns the contents of the matching template file, or a minimal inline template.",
                        "default": "poller",
                    },
                },
            },
        ),
        Tool(
            name="check_task_outputs",
            description="Check recent outputs from scheduled tasks. Use this to review what your scheduled jobs have done.",
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "Only show outputs since this ISO timestamp (optional).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of outputs to return. Default 10.",
                        "default": 10,
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Filter by job name (optional).",
                    },
                },
            },
        ),
        Tool(
            name="write_task_output",
            description="Write output from a scheduled task. Used by task instances to record their results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": "The name of the job writing output.",
                    },
                    "output": {
                        "type": "string",
                        "description": "The output/result to record.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Status: 'success' or 'failed'. Default 'success'.",
                        "default": "success",
                    },
                },
                "required": ["job_name", "output"],
            },
        ),
        # Subagent Result Relay
        Tool(
            name="write_result",
            description=(
                "Write a result from a background subagent back to the main message queue. "
                "Subagents should call send_reply directly first (crash-safe delivery), then call "
                "this with sent_reply_to_user=True so the dispatcher marks the message processed "
                "without re-sending. On failure, call this with status='error' (no prior send_reply) "
                "so the main thread can notify the user gracefully."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Identifier for the task that produced this result (e.g. 'brain-dump-42', 'pr-review-7'). Used for deduplication and logging.",
                    },
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "The chat/channel ID to deliver the result to. Pass the same chat_id received in the original message.",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Dispatcher-internal summary of this result. "
                            "Kept short (ideally under ~500 words) so the dispatcher's context does not grow. "
                            "Not shown to the user when reply_text is provided. "
                            "For large outputs — reports, diffs, full analysis — write the content "
                            "to ~/lobster-workspace/reports/<task_id>.md and pass the path in "
                            "`artifacts` instead."
                        ),
                    },
                    "reply_text": {
                        "type": "string",
                        "description": (
                            "Optional user-facing reply text. When provided and "
                            "sent_reply_to_user is False, the dispatcher sends this to the user "
                            "instead of text. Use this to keep text as a terse internal summary "
                            "while sending a richer or differently-phrased message to the user. "
                            "Omit if text is already the right user-facing message. "
                            "Do not include raw file paths — use relative paths or descriptions instead."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": "The messaging source to reply via (telegram, slack, etc.). Default: telegram.",
                        "default": "telegram",
                    },
                    "status": {
                        "type": "string",
                        "description": "Result status: 'success' (default) or 'error'. Use 'error' when the subagent failed so the main thread can signal failure to the user.",
                        "default": "success",
                        "enum": ["success", "error"],
                    },
                    "artifacts": {
                        "type": "array",
                        "description": "Optional list of file paths produced by the subagent that the main thread may reference or include in its reply.",
                        "items": {"type": "string"},
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "Slack thread timestamp. If provided, the reply will be sent as a thread reply.",
                    },
                    "sent_reply_to_user": {
                        "type": "boolean",
                        "description": (
                            "Whether the subagent already called send_reply to deliver the result. "
                            "Default false. Set to True if you already called send_reply — the "
                            "dispatcher will mark processed without relaying. Set to False (or omit) "
                            "if you did NOT call send_reply and want the dispatcher to relay this "
                            "result to the user."
                        ),
                        "default": False,
                    },
                },
                "required": ["task_id", "chat_id", "text"],
            },
        ),
        # Subagent Observation
        Tool(
            name="write_observation",
            description=(
                "Write an observation from a background subagent into the inbox. "
                "Use this to surface things you noticed that are separate from your primary result — "
                "user context worth remembering, system issues you spotted, or errors to log. "
                "The dispatcher routes each observation based on its category. "
                "Don't swallow observations — surface them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "The chat/channel ID this observation relates to (same as write_result chat_id).",
                    },
                    "text": {
                        "type": "string",
                        "description": "The observation content.",
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Category of observation. "
                            "'user_context': something about the user worth remembering or forwarding. "
                            "'system_context': internal state or info the system should store silently. "
                            "'system_error': an error or anomaly to log."
                        ),
                        "enum": ["user_context", "system_context", "system_error"],
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional identifier for the originating task.",
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Messaging source the observation originated from "
                            "('telegram', 'slack', etc.). Defaults to 'telegram'."
                        ),
                    },
                },
                "required": ["chat_id", "text", "category"],
            },
        ),
        # Event Bus Tool (issue #1665)
        Tool(
            name="emit_event",
            description=(
                "Emit a structured event into the Lobster event bus. "
                "Use this to record lifecycle events, errors, or observations from "
                "dispatchers and subagents. Events are written to events.jsonl and "
                "forwarded to listeners (including CriticalAlertListener for critical-level events). "
                "This is a fire-and-forget call — it never blocks and never raises."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": (
                            "Dot-separated event type string. "
                            "Examples: 'agent.spawn', 'agent.complete', 'memory.write', "
                            "'system.error', 'debug.observation'."
                        ),
                    },
                    "level": {
                        "type": "string",
                        "description": "Severity level. One of: debug, info, warn, error, critical.",
                        "enum": ["debug", "info", "warn", "error", "critical"],
                    },
                    "msg": {
                        "type": "string",
                        "description": "Human-readable message describing the event.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Optional additional structured data for the event.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional task identifier to associate with this event.",
                    },
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "Optional chat/channel ID to associate with this event.",
                    },
                },
                "required": ["event_type", "level", "msg"],
            },
        ),
        # Pending Agent Tracker Tools
        Tool(
            name="register_agent",
            description=(
                "Record a newly-spawned background agent in the pending-agents tracker. "
                "Call this BEFORE spawning a Task (pre-registration), passing the agent_id "
                "extracted from the Task tool result text and a human-readable description. "
                "This creates a durable record that survives dispatcher restarts and compactions, "
                "so in-flight agents are never silently lost. Pass output_file to enable liveness "
                "detection — the path to the agent's Claude Code output file in /tmp."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Unique identifier for the agent task. Extract from the Task tool "
                            "result text (looks like 'agentId: abc123...'). If the ID cannot be "
                            "parsed, use a synthetic ID derived from task context."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Human-readable summary of what the agent is doing. Include enough "
                            "context so Lobster can relay results correctly after a restart "
                            "(e.g. 'Implement feature X on issue #42 for chat 1234567890')."
                        ),
                    },
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "Telegram/Slack chat_id to notify when the agent completes.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Logical task identifier — the same value the subagent will pass "
                            "as task_id to write_result. Used for auto-unregister matching. "
                            "If omitted, agent_id is used for matching."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": "Messaging platform ('telegram', 'slack', etc.). Default: 'telegram'.",
                    },
                    "output_file": {
                        "type": "string",
                        "description": (
                            "Full path to the Claude Code agent output file "
                            "(e.g. /tmp/claude-1000/-home-lobster-lobster-workspace/{session}/{agentId}.output). "
                            "Used for liveness detection: stat the mtime to determine if the agent is still active. "
                            "Extract from the Task tool result text."
                        ),
                    },
                    "timeout_minutes": {
                        "type": "integer",
                        "description": (
                            "Expected maximum runtime in minutes. Agents older than this without "
                            "recent output file activity can be presumed dead. Default: 30."
                        ),
                    },
                    "idempotency": {
                        "type": "string",
                        "description": (
                            "Re-run safety classification for orphan recovery. "
                            "'safe' — read-only/idempotent task, safe to re-run automatically after restart. "
                            "'unsafe' — task has side effects (sends messages, modifies files, posts comments); "
                            "requires explicit user approval to re-run. "
                            "'unknown' — caller did not classify (default; treated as unsafe for recovery)."
                        ),
                        "enum": ["safe", "unsafe", "unknown"],
                    },
                    "task_origin": {
                        "type": "string",
                        "description": (
                            "Origin of this task: 'user' | 'scheduled' | 'internal'. "
                            "'user' — triggered by a real user message. "
                            "'scheduled' — triggered by a scheduled job or cron. "
                            "'internal' — system-initiated, no user involved. "
                            "Defaults to 'user'."
                        ),
                        "enum": ["user", "scheduled", "internal"],
                    },
                },
                "required": ["agent_id", "description", "chat_id"],
            },
        ),
        Tool(
            name="unregister_agent",
            description=(
                "Remove a completed or failed agent from the pending-agents tracker. "
                "Call this when a write_result arrives from a background agent to mark it done. "
                "Idempotent: removing a non-existent ID is a no-op."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent_id that was passed to register_agent when the task was spawned.",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        # Agent Session Store Tools (SQLite-backed, supersede register/unregister_agent long-term)
        Tool(
            name="session_start",
            description=(
                "Record a newly-spawned background agent session in the SQLite session store. "
                "Equivalent to register_agent but with richer metadata. Sessions survive restarts, "
                "accumulate history, and are queryable via get_active_sessions / get_session_history. "
                "Use this for new code; register_agent remains a working alias."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Unique identifier for the agent (uuid or synthetic slug).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable summary of what the agent is doing.",
                    },
                    "chat_id": {
                        "oneOf": [{"type": "integer"}, {"type": "string"}],
                        "description": "Chat/channel to notify when the agent completes.",
                    },
                    "agent_type": {
                        "type": "string",
                        "description": "Agent subtype: 'functional-engineer', 'general-purpose', etc.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Logical task identifier for auto-unregister matching via write_result.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Messaging platform ('telegram', 'slack', etc.). Default: 'telegram'.",
                    },
                    "output_file": {
                        "type": "string",
                        "description": "Full path to /tmp/.../*.output for liveness detection.",
                    },
                    "timeout_minutes": {
                        "type": "integer",
                        "description": "Expected maximum runtime in minutes.",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": "Parent session ID for nested agents (NULL = top-level).",
                    },
                    "input_summary": {
                        "type": "string",
                        "description": "First ~200 chars of task prompt (optional context).",
                    },
                    "trigger_message_id": {
                        "type": "string",
                        "description": (
                            "Inbox message_id that caused this agent to be spawned "
                            "(e.g. '1773541796785_6036'). Records causality — which user "
                            "message triggered this task."
                        ),
                    },
                    "trigger_snippet": {
                        "type": "string",
                        "description": (
                            "First 200 chars of the triggering message text. PII — stored "
                            "only in this private repo's SQLite DB, not forwarded to wire "
                            "server unless LOBSTER_WIRE_REDACT_PII=false."
                        ),
                    },
                    "idempotency": {
                        "type": "string",
                        "description": (
                            "Re-run safety classification for orphan recovery. "
                            "'safe' — read-only/idempotent task, safe to re-run automatically after restart. "
                            "'unsafe' — task has side effects (sends messages, modifies files, posts comments); "
                            "requires explicit user approval to re-run. "
                            "'unknown' — caller did not classify (default; treated as unsafe for recovery)."
                        ),
                        "enum": ["safe", "unsafe", "unknown"],
                    },
                    "task_origin": {
                        "type": "string",
                        "description": (
                            "Origin of this task: 'user' | 'scheduled' | 'internal'. "
                            "'user' — triggered by a real user message (Telegram, Slack, etc.). "
                            "'scheduled' — triggered by a scheduled job or cron task. "
                            "'internal' — system-initiated, no user involved (reconciler, "
                            "health check, session management, etc.). "
                            "Defaults to 'user' when not specified. "
                            "Code that previously checked chat_id==0 to detect system tasks "
                            "should check task_origin=='internal' instead — the two conditions "
                            "are equivalent but task_origin makes intent explicit."
                        ),
                        "enum": ["user", "scheduled", "internal"],
                    },
                    "claude_session_id": {
                        "type": "string",
                        "description": (
                            "The Claude Code session UUID for this session "
                            "(hook_input['session_id'], format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). "
                            "Required when agent_type='dispatcher': written to a dedicated state file "
                            "so SessionStart hooks can correctly identify the dispatcher session. "
                            "This is a different ID space from the MCP HTTP transport session ID."
                        ),
                    },
                },
                "required": ["agent_id", "description", "chat_id"],
            },
            _meta={"anthropic/alwaysLoad": True},
        ),
        Tool(
            name="session_end",
            description=(
                "Mark an agent session as completed or failed in the SQLite session store. "
                "Equivalent to unregister_agent but records final status and result summary. "
                "Matches on either agent_id or task_id. Idempotent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent_id (or task_id) to end.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Final status: 'completed' | 'failed' | 'dead'.",
                        "enum": ["completed", "failed", "dead"],
                    },
                    "result_summary": {
                        "type": "string",
                        "description": "Optional short summary of the outcome.",
                    },
                    "stop_reason": {
                        "type": "string",
                        "description": (
                            "Why the agent stopped. Known values: 'end_turn', 'tool_use', "
                            "'max_turns', 'error', 'killed'. NULL for legacy rows."
                        ),
                    },
                },
                "required": ["agent_id", "status"],
            },
        ),
        Tool(
            name="get_active_sessions",
            description=(
                "Return all currently running agent sessions with elapsed time. "
                "Queries the SQLite session store — fast local read, always accurate, "
                "survives restarts. Use this to answer 'what agents are running?'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Optional: filter to sessions for this chat_id only.",
                    },
                },
            },
        ),
        Tool(
            name="get_session_history",
            description=(
                "Return historical agent session records from the SQLite store, newest first. "
                "Includes completed, failed, and dead sessions. Useful for auditing recent work."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of records to return. Default: 20.",
                        "default": 20,
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status: 'running' | 'completed' | 'failed' | 'dead'. Omit for all.",
                    },
                },
            },
        ),
        Tool(
            name="record_reply",
            description=(
                "Record that an outbound reply message was sent in connection with a background agent task. "
                "Call this immediately after send_reply when you have an active agent task, passing the "
                "agent_id and the reply message_id returned by send_reply. This maintains a causal chain: "
                "trigger_message → agent task → outbound replies. "
                "Idempotent: calling multiple times with the same message_id is safe (list deduplicated at query time)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent_id (or task_id) of the session to update.",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "The outbound message_id returned by send_reply.",
                    },
                },
                "required": ["agent_id", "message_id"],
            },
        ),
        # Brain Dump Triage Tools
        Tool(
            name="triage_brain_dump",
            description="Mark a brain dump issue as triaged. Adds 'triaged' label, removes 'raw' label, and adds a triage comment listing extracted action items. Use this after analyzing a brain dump and identifying action items.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "issue_number": {
                        "type": "integer",
                        "description": "The brain dump issue number to triage.",
                    },
                    "action_items": {
                        "type": "array",
                        "description": "List of action items extracted from the brain dump. Each item should have 'title' and optional 'description'.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Short title for the action item.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Optional longer description.",
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    "triage_notes": {
                        "type": "string",
                        "description": "Optional notes about the triage (e.g., context matches, patterns noticed).",
                    },
                },
                "required": ["owner", "repo", "issue_number", "action_items"],
            },
        ),
        Tool(
            name="create_action_item",
            description="Create a new GitHub issue as an action item linked to a brain dump. The action item will reference the parent brain dump issue. Returns the created issue number.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "brain_dump_issue": {
                        "type": "integer",
                        "description": "The parent brain dump issue number this action comes from.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the action item issue.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Body/description for the action item. Should include context from brain dump.",
                    },
                    "labels": {
                        "type": "array",
                        "description": "Optional labels to apply (e.g., ['urgent', 'project:xyz']).",
                        "items": {"type": "string"},
                    },
                },
                "required": ["owner", "repo", "brain_dump_issue", "title"],
            },
        ),
        Tool(
            name="link_action_to_brain_dump",
            description="Add a comment to the brain dump issue linking to an action item issue. Use this after creating action items to maintain traceability.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "brain_dump_issue": {
                        "type": "integer",
                        "description": "The brain dump issue number.",
                    },
                    "action_issue": {
                        "type": "integer",
                        "description": "The action item issue number to link.",
                    },
                    "action_title": {
                        "type": "string",
                        "description": "Title of the action item (for the link comment).",
                    },
                },
                "required": ["owner", "repo", "brain_dump_issue", "action_issue", "action_title"],
            },
        ),
        Tool(
            name="close_brain_dump",
            description="Close a brain dump issue after all action items are created. Adds 'actioned' label, removes 'triaged' label, adds a summary comment, and closes the issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "issue_number": {
                        "type": "integer",
                        "description": "The brain dump issue number to close.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Summary of what was done with this brain dump.",
                    },
                    "action_issues": {
                        "type": "array",
                        "description": "List of action item issue numbers created from this brain dump.",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["owner", "repo", "issue_number", "summary"],
            },
        ),
        Tool(
            name="get_brain_dump_status",
            description="Get the current triage status of a brain dump issue. Returns the issue state, labels, and any linked action items found in comments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Repository owner (GitHub username or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g., 'brain-dumps').",
                    },
                    "issue_number": {
                        "type": "integer",
                        "description": "The brain dump issue number to check.",
                    },
                },
                "required": ["owner", "repo", "issue_number"],
            },
        ),
        # Memory System Tools
        Tool(
            name="memory_store",
            description="Store an event in Lobster's memory. Events can be messages, tasks, decisions, notes, or links. Each event is embedded and indexed for fast hybrid search.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The content/text of the event to remember.",
                    },
                    "type": {
                        "type": "string",
                        "description": "Event type: message, task, decision, note, or link. Default: note.",
                        "default": "note",
                    },
                    "source": {
                        "type": "string",
                        "description": "Where the event came from: telegram, github, internal. Default: internal.",
                        "default": "internal",
                    },
                    "project": {
                        "type": "string",
                        "description": "Optional project name this event relates to.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorization.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional subagent task identifier. Included in debug alerts when LOBSTER_DEBUG=true so the caller is visible in the memory write notification.",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Optional: chat_id of the user storing this memory. Embedded as source_chat_id in metadata for attribution.",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="memory_search",
            description="Search Lobster's memory using hybrid vector + keyword search. Returns the most relevant events matching the query. Falls back to keyword search if vector search is unavailable.",
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
                    "task_id": {
                        "type": "string",
                        "description": "Optional subagent task identifier. Included in debug alerts when LOBSTER_DEBUG=true so the caller is visible in the memory search notification.",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Optional: filter results to memories attributed to this chat_id (plus unattributed legacy memories).",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_recent",
            description="Get recent events from Lobster's memory. Returns events from the last N hours, newest first.",
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
                    "chat_id": {
                        "type": "string",
                        "description": "Optional: filter results to memories attributed to this chat_id (plus unattributed legacy memories).",
                    },
                },
            },
        ),
        Tool(
            name="get_handoff",
            description="Read the current handoff document - a complete briefing for a new Lobster session. Contains identity, architecture, current state, and pending items.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="mark_consolidated",
            description="Mark memory events as consolidated (processed by nightly consolidation). Pass a list of event IDs that have been reviewed and synthesized into canonical files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of event IDs to mark as consolidated.",
                    },
                },
                "required": ["event_ids"],
            },
        ),
        # Self-Update Tools
        Tool(
            name="check_updates",
            description="Check if Lobster updates are available on origin/main. Returns commit count, commit log, and whether updates exist. Lightweight check.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_upgrade_plan",
            description="Generate a full upgrade plan including changelog, compatibility analysis (breaking changes, dependency changes, local conflicts), and recommended steps. Use this before executing an update.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="execute_update",
            description="Execute a safe auto-update. Only proceeds if compatibility check passes (no breaking changes, no local conflicts). Pulls latest from origin/main, installs deps, and provides rollback command. Returns error if manual intervention is needed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true to proceed with the update. Safety confirmation.",
                    },
                },
                "required": ["confirm"],
            },
        ),
        # Convenience Tools (canonical memory readers)
        Tool(
            name="get_priorities",
            description="Fetch Lobster's current priority stack. Returns the canonical priorities.md file, updated nightly by the consolidation process. Shows what Lobster considers most important right now, ranked and annotated.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_project_context",
            description="Fetch status and context for a specific project. Returns project status, recent decisions, pending items, and blockers from the canonical project file.",
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
            name="get_person_context",
            description="Fetch context for a specific person. Returns role, contact info, and interaction history from the canonical people file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person": {
                        "type": "string",
                        "description": "Person name matching the file stem in people/ (e.g., 'Alice', 'Bob')",
                    },
                },
                "required": ["person"],
            },
        ),
        Tool(
            name="list_people",
            description="List all people tracked in Lobster's canonical memory. Returns person names for use with get_person_context().",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # Local Sync Awareness Tools
        Tool(
            name="check_local_sync",
            description="Check lobster-sync branches on registered repos to see the latest local work-in-progress. Returns last commit timestamp, commit message, diff summary vs main.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Optional: filter to a specific repo (owner/name format). Leave empty for all.",
                    },
                },
            },
        ),
        # bisque-computer Connection Tools
        Tool(
            name="get_bisque_connection_url",
            description="Get the WebSocket connection URL for bisque-computer to connect to this Lobster dashboard server. Returns the full URL including the auth token, e.g. ws://IP:9100?token=UUID. Use this when the user asks to 'connect bisque-computer' or 'give me the bisque connection URL'.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="generate_bisque_login_token",
            description="Generate a login token for the bisque-chat PWA. The token encodes the relay WebSocket URL and a one-time bootstrap token. Users paste this token into the bisque app login screen to authenticate. Use this when the user asks for a 'login token', 'bisque token', or 'connect to bisque'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Email address to associate with the token. Used to identify the user in the chat session.",
                    },
                },
                "required": ["email"],
            },
        ),
        # Skill Management Tools
        Tool(
            name="get_skill_context",
            description="Get assembled context from all active skills. Returns markdown with behavior instructions, domain context, and preferences for each active skill. Call this at message processing start when skills are enabled.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_skills",
            description="List available skills in the Lobster Shop. Shows install/active status for each skill.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: all, installed, active, available. Default: all.",
                        "default": "all",
                    },
                },
            },
        ),
        Tool(
            name="activate_skill",
            description="Activate an installed skill. Active skills inject their behavior, context, and preferences into Lobster's runtime.",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to activate.",
                    },
                    "mode": {
                        "type": "string",
                        "description": "Activation mode: always (always active), triggered (activated by /commands or keywords), contextual (activated when context matches). Default: always.",
                        "default": "always",
                    },
                },
                "required": ["skill_name"],
            },
        ),
        Tool(
            name="deactivate_skill",
            description="Deactivate a skill. Its context will no longer be injected at runtime.",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to deactivate.",
                    },
                },
                "required": ["skill_name"],
            },
        ),
        Tool(
            name="get_skill_preferences",
            description="Get merged preferences (defaults + user overrides) for a skill.",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill.",
                    },
                },
                "required": ["skill_name"],
            },
        ),
        Tool(
            name="set_skill_preference",
            description="Set a preference value for a skill. Validates against the skill's schema if available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Preference key to set.",
                    },
                    "value": {
                        "description": "Value to set (string, number, or boolean).",
                    },
                },
                "required": ["skill_name", "key", "value"],
            },
        ),
        # Google Calendar Tools
        Tool(
            name="create_calendar_event",
            description="Create a new event on a user's primary Google Calendar.",
            inputSchema={
                "type": "object",
                "properties": {
                    "telegram_chat_id": {
                        "oneOf": [{"type": "integer"}, {"type": "string"}],
                        "description": "The Telegram chat_id of the user whose calendar to write to.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Event title / summary.",
                    },
                    "start_datetime": {
                        "type": "string",
                        "description": "Event start time in ISO 8601 format (e.g. 2026-03-07T19:00:00).",
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": "Event end time in ISO 8601 format.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone name for the event (e.g. America/Los_Angeles). Default: America/Los_Angeles.",
                        "default": "America/Los_Angeles",
                    },
                    "location": {
                        "type": "string",
                        "description": "Optional event location.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional event description / notes.",
                    },
                },
                "required": ["telegram_chat_id", "title", "start_datetime", "end_datetime"],
            },
        ),
        Tool(
            name="list_calendar_events",
            description="List upcoming events from a user's primary Google Calendar.",
            inputSchema={
                "type": "object",
                "properties": {
                    "telegram_chat_id": {
                        "oneOf": [{"type": "integer"}, {"type": "string"}],
                        "description": "The Telegram chat_id of the user.",
                    },
                    "time_min": {
                        "type": "string",
                        "description": "Start of time range (ISO 8601). Defaults to now.",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "End of time range (ISO 8601). Defaults to 7 days from now.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of events to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": ["telegram_chat_id"],
            },
        ),
        # /report Slash Command Tool
        Tool(
            name="create_report",
            description=(
                "File a user report triggered by the /report slash command. "
                "Captures a point-in-time snapshot: the user's description, the last 10 "
                "messages for context, and the IDs of any active agent sessions. "
                "Stores the record in the reports table of agent_sessions.db and returns "
                "a unique report ID (e.g. RPT-001). "
                "Call this when the user sends '/report <description>' — the response "
                "should be sent back to the user with the report ID as confirmation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "The problem or feedback description from the user (everything after '/report ').",
                    },
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "The chat_id of the user filing the report.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Messaging source (telegram, slack, etc.). Default: telegram.",
                        "default": "telegram",
                    },
                },
                "required": ["description", "chat_id"],
            },
        ),
        Tool(
            name="list_reports",
            description=(
                "List filed /report records from the reports table, newest first. "
                "Optionally filter by chat_id or status. Useful for reviewing open "
                "user-reported issues or checking what has already been triaged."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "oneOf": [
                            {"type": "integer"},
                            {"type": "string"},
                        ],
                        "description": "If provided, restrict results to reports from this chat.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by report status (e.g. 'open', 'closed'). Default: 'open'.",
                        "default": "open",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Default: 20.",
                        "default": 20,
                    },
                },
                "required": [],
            },
        ),
        # Session File Management Tools
        Tool(
            name="create_session_file",
            description=(
                "Create a new session file for today. Copies the session template, "
                "substitutes the date/sequence/timestamps, writes the file to "
                "~/lobster-user-config/memory/canonical/sessions/YYYYMMDD-NNN.md, "
                "and records the path in /tmp/lobster-current-session-file. "
                "Returns {path, session_id}."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="get_session_file",
            description=(
                "Read a session file. If session_id is 'current' (the default), "
                "reads the pointer at /tmp/lobster-current-session-file, or finds "
                "today's latest session. Otherwise looks up the file matching the "
                "given YYYYMMDD-NNN session_id. "
                "Returns {path, content, session_id}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (e.g. '20260329-001') or 'current'. Defaults to 'current'.",
                        "default": "current",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="update_session_file",
            description=(
                "Update a named H2 section (e.g. '## Summary', '## Open Threads') "
                "inside a session file. Replaces only the targeted section; all other "
                "sections and the file header are preserved verbatim. Write is atomic "
                "(temp-file + rename). "
                "Returns {path, section, updated: true}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "Section name without the '## ' prefix, e.g. 'Summary' or 'Open Threads'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content for the section (replaces everything between the section header and the next H2 or EOF).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (e.g. '20260329-001') or 'current'. Defaults to 'current'.",
                        "default": "current",
                    },
                },
                "required": ["section", "content"],
            },
        ),
        Tool(
            name="list_session_files",
            description=(
                "List session files, optionally filtered to a single date. "
                "Returns a sorted list of {session_id, path, has_content} objects. "
                "has_content is true when the Summary section contains more than 50 "
                "characters of non-boilerplate text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Optional YYYYMMDD date string to filter results.",
                    },
                },
                "required": [],
            },
        ),
    ] + (
        # User Model Tools (only registered when feature flag is enabled)
        [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in USER_MODEL_TOOL_DEFINITIONS
        ]
        if _user_model is not None
        else []
    )


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls with structured audit logging."""
    log.info(f"Tool called: {name}")
    start_time = time.time()
    try:
        result = await _dispatch_tool(name, arguments)
        elapsed_ms = int((time.time() - start_time) * 1000)
        # Audit log all tool calls (except wait_for_messages which is too noisy)
        if name != "wait_for_messages":
            audit_log(tool=name, args=arguments, result="ok", duration_ms=elapsed_ms)
        return result
    except ValidationError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        audit_log(tool=name, args=arguments, error=str(e), duration_ms=elapsed_ms)
        return [TextContent(type="text", text=f"Validation error: {e}")]
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        audit_log(tool=name, args=arguments, error=str(e), duration_ms=elapsed_ms)
        log.error(f"Tool {name} failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error in {name}: {str(e)}")]


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls to handlers."""
    # Session guard: block inbox-monitoring and outbox-write tools for
    # any session that is not the designated dispatcher session.
    #
    # In HTTP transport mode: use per-session tagging (_is_main_http_session).
    #   - A session becomes the dispatcher by calling wait_for_messages (Option A),
    #     session_start(agent_type="dispatcher") (Option B), or any guarded tool
    #     when no dispatcher is currently tagged (Option C — restart recovery).
    #   - All other sessions are blocked from guarded tools.
    #
    # Option C — restart recovery (no-dispatcher-tagged auto-tag):
    #   When _dispatcher_session_id is None the MCP server has just restarted and
    #   has no record of any dispatcher session.  The first session to call any
    #   guarded tool must be the new dispatcher — subagents cannot be running yet
    #   because they require the dispatcher to spawn them.  Auto-tagging here closes
    #   the window between server start and the dispatcher's first WFM or
    #   session_start call, which is when backlog send_reply calls would otherwise
    #   be blocked.
    #
    # In stdio transport mode: use the legacy tmux ancestry / env-var check
    #   (_is_main_session).  This path is unchanged.
    if name in _SESSION_GUARDED_TOOLS:
        if _http_session_manager is not None:
            # HTTP mode: per-session guard.  wait_for_messages is always exempted
            # (Option A tagging).  When no dispatcher is tagged yet (Option C),
            # auto-tag the calling session before the guard check so the call
            # proceeds — this handles the restart race where the dispatcher calls
            # send_reply or check_inbox before its first WFM/session_start fires.
            if _dispatcher_session_id is None:
                session_id = _get_current_http_session_id()
                if session_id is not None:
                    log.info(
                        f"[session-tag] Option C: no dispatcher tagged, auto-tagging "
                        f"on '{name}' call — session {session_id!r}"
                    )
                    _tag_dispatcher_session(session_id)
            if name != "wait_for_messages" and not _is_main_http_session():
                log.warning(
                    f"Session guard blocked '{name}' — HTTP session "
                    f"{_get_current_http_session_id()!r} is not the dispatcher "
                    f"(dispatcher={_dispatcher_session_id!r})"
                )
                return _session_guard_error(name)
        elif not _is_main_session():
            log.warning(f"Session guard blocked '{name}' — not in lobster tmux session")
            return _session_guard_error(name)

    if name == "wait_for_messages":
        return await handle_wait_for_messages(arguments)
    elif name == "check_inbox":
        return await handle_check_inbox(arguments)
    elif name == "send_reply":
        return await handle_send_reply(arguments)
    elif name == "send_whatsapp_reply":
        return await handle_send_whatsapp_reply(arguments)
    elif name == "send_sms_reply":
        return await handle_send_sms_reply(arguments)
    elif name == "mark_processed":
        return await handle_mark_processed(arguments)
    elif name == "mark_processing":
        return await handle_mark_processing(arguments)
    elif name == "claim_and_ack":
        return await handle_claim_and_ack(arguments)
    elif name == "mark_failed":
        return await handle_mark_failed(arguments)
    elif name == "list_sources":
        return await handle_list_sources(arguments)
    elif name == "get_stats":
        return await handle_get_stats(arguments)
    elif name == "get_conversation_history":
        return await handle_get_conversation_history(arguments)
    elif name == "get_message_by_telegram_id":
        return await handle_get_message_by_telegram_id(arguments)
    elif name == "list_tasks":
        return await handle_list_tasks(arguments)
    elif name == "create_task":
        return await handle_create_task(arguments)
    elif name == "update_task":
        return await handle_update_task(arguments)
    elif name == "get_task":
        return await handle_get_task(arguments)
    elif name == "delete_task":
        return await handle_delete_task(arguments)
    # IFTTT Behavioral Rules Tools
    elif name == "list_rules":
        return await handle_list_rules(arguments)
    elif name == "add_rule":
        return await handle_add_rule(arguments)
    elif name == "delete_rule":
        return await handle_delete_rule(arguments)
    elif name == "get_rule":
        return await handle_get_rule(arguments)
    elif name == "update_rule":
        return await handle_update_rule(arguments)
    elif name == "transcribe_audio":
        return await handle_transcribe_audio(arguments)
    elif name == "send_voice_note":
        return await handle_send_voice_note(arguments)
    # Headless Browser Fetch
    elif name == "fetch_page":
        return await handle_fetch_page(arguments)
    # Scheduled Jobs Tools
    elif name == "create_scheduled_job":
        return await handle_create_scheduled_job(arguments)
    elif name == "list_scheduled_jobs":
        return await handle_list_scheduled_jobs(arguments)
    elif name == "get_scheduled_job":
        return await handle_get_scheduled_job(arguments)
    elif name == "update_scheduled_job":
        return await handle_update_scheduled_job(arguments)
    elif name == "delete_scheduled_job":
        return await handle_delete_scheduled_job(arguments)
    elif name == "get_job_scaffold":
        return await handle_get_job_scaffold(arguments)
    elif name == "check_task_outputs":
        return await handle_check_task_outputs(arguments)
    elif name == "write_task_output":
        return await handle_write_task_output(arguments)
    elif name == "write_result":
        return await handle_write_result(arguments)
    elif name == "write_observation":
        return await handle_write_observation(arguments)
    elif name == "emit_event":
        return await handle_emit_event(arguments)
    # Pending Agent Tracker Tools (register/unregister kept as aliases)
    elif name == "register_agent":
        return await handle_register_agent(arguments)
    elif name == "unregister_agent":
        return await handle_unregister_agent(arguments)
    # Agent Session Store Tools (SQLite-backed)
    elif name == "session_start":
        return await handle_session_start(arguments)
    elif name == "session_end":
        return await handle_session_end(arguments)
    elif name == "get_active_sessions":
        return await handle_get_active_sessions(arguments)
    elif name == "get_session_history":
        return await handle_get_session_history(arguments)
    elif name == "record_reply":
        return await handle_record_reply(arguments)
    # Brain Dump Triage Tools
    elif name == "triage_brain_dump":
        return await handle_triage_brain_dump(arguments)
    elif name == "create_action_item":
        return await handle_create_action_item(arguments)
    elif name == "link_action_to_brain_dump":
        return await handle_link_action_to_brain_dump(arguments)
    elif name == "close_brain_dump":
        return await handle_close_brain_dump(arguments)
    elif name == "get_brain_dump_status":
        return await handle_get_brain_dump_status(arguments)
    # Memory System Tools
    elif name == "memory_store":
        return await handle_memory_store(arguments)
    elif name == "memory_search":
        return await handle_memory_search(arguments)
    elif name == "memory_recent":
        return await handle_memory_recent(arguments)
    elif name == "get_handoff":
        return await handle_get_handoff(arguments)
    elif name == "mark_consolidated":
        return await handle_mark_consolidated(arguments)
    # Self-Update Tools
    elif name == "check_updates":
        return await handle_check_updates(arguments)
    elif name == "get_upgrade_plan":
        return await handle_get_upgrade_plan(arguments)
    elif name == "execute_update":
        return await handle_execute_update(arguments)
    # Convenience Tools (canonical memory readers)
    elif name == "get_priorities":
        return await handle_get_priorities(arguments)
    elif name == "get_project_context":
        return await handle_get_project_context(arguments)
    elif name == "get_daily_digest":
        return await handle_get_daily_digest(arguments)
    elif name == "list_projects":
        return await handle_list_projects(arguments)
    elif name == "get_person_context":
        return await handle_get_person_context(arguments)
    elif name == "list_people":
        return await handle_list_people(arguments)
    # Local Sync Awareness Tools
    elif name == "check_local_sync":
        return await handle_check_local_sync(arguments)
    # bisque-computer Connection Tools
    elif name == "get_bisque_connection_url":
        return await handle_get_bisque_connection_url(arguments)
    elif name == "generate_bisque_login_token":
        return await handle_generate_bisque_login_token(arguments)
    # Skill Management Tools
    elif name == "get_skill_context":
        return await handle_get_skill_context(arguments)
    elif name == "list_skills":
        return await handle_list_skills(arguments)
    elif name == "activate_skill":
        return await handle_activate_skill(arguments)
    elif name == "deactivate_skill":
        return await handle_deactivate_skill(arguments)
    elif name == "get_skill_preferences":
        return await handle_get_skill_preferences(arguments)
    elif name == "set_skill_preference":
        return await handle_set_skill_preference(arguments)
    # Google Calendar Tools
    elif name == "create_calendar_event":
        return await handle_create_calendar_event(arguments)
    elif name == "list_calendar_events":
        return await handle_list_calendar_events(arguments)
    # /report Slash Command Tools
    elif name == "create_report":
        return await handle_create_report(arguments)
    elif name == "list_reports":
        return await handle_list_reports(arguments)
    # Session File Management Tools
    elif name == "create_session_file":
        return await handle_create_session_file(arguments)
    elif name == "get_session_file":
        return await handle_get_session_file(arguments)
    elif name == "update_session_file":
        return await handle_update_session_file(arguments)
    elif name == "list_session_files":
        return await handle_list_session_files(arguments)
    # User Model Tools (dispatched to user_model subsystem)
    elif name in _user_model_tool_names and _user_model is not None:
        result_json = _user_model.dispatch(name, arguments)
        return [TextContent(type="text", text=result_json)]
    elif name in _user_model_tool_names and _user_model is None:
        return [TextContent(type="text", text='{"error": "User model subsystem not initialized."}')]
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


def _find_message_file(directory: Path, message_id: str) -> Path | None:
    """Find a message file in a directory by ID or filename match."""
    for f in directory.glob("*.json"):
        if message_id in f.name:
            return f
        try:
            with open(f) as fp:
                msg = json.load(fp)
                if msg.get("id") == message_id:
                    return f
        except Exception:
            continue
    return None


def _stale_timeout_for_message(msg: dict) -> int:
    """Return the stale processing timeout in seconds based on message type.

    Text messages are expected to complete quickly; media types (voice, audio,
    photo, document) may take longer due to transcription or download time.
    """
    slow_types = {"voice", "photo", "document"}  # "audio" removed: normalized to "voice" at ingest
    msg_type = msg.get("type", "text")
    return 300 if msg_type in slow_types else 90


def _recover_stale_processing():
    """Move stale messages from processing/ back to inbox/.

    Uses a type-aware timeout: 90s for text messages, 300s for media
    (voice/audio/photo/document) where transcription or download can be slow.

    Releases the SQLite claim row BEFORE moving back to inbox/ so a fresh
    claim is possible on the next dispatch cycle (issue #1360).
    """
    now = time.time()
    for f in PROCESSING_DIR.glob("*.json"):
        try:
            age = now - f.stat().st_mtime
            msg = json.loads(f.read_text())
            max_age = _stale_timeout_for_message(msg)
            if age > max_age:
                # Extract message_id from filename (strip .json suffix)
                message_id = f.stem
                # Release claim BEFORE moving so a concurrent dispatcher cannot
                # win a new claim while the file is still in processing/.
                _claims_db.release(message_id)
                dest = INBOX_DIR / f.name
                f.rename(dest)
                log.warning(
                    f"Recovered stale message from processing: {f.name} "
                    f"(type: {msg.get('type', 'text')}, age: {int(age)}s, timeout: {max_age}s)"
                )
        except Exception:
            continue


def _recover_retryable_messages():
    """Move retry-eligible messages from failed/ back to inbox/."""
    now = time.time()
    for f in FAILED_DIR.glob("*.json"):
        try:
            msg = json.loads(f.read_text())
            if msg.get("_permanently_failed"):
                continue
            retry_at = msg.get("_retry_at", 0)
            if now >= retry_at:
                dest = INBOX_DIR / f.name
                f.rename(dest)
                log.info(f"Retrying message: {f.name} (attempt {msg.get('_retry_count', 0)})")
        except Exception:
            continue


def _build_active_sessions_prefix() -> str:
    """Return a compact active-sessions context block, or empty string if none running.

    Called at the start of wait_for_messages and on timeouts so the dispatcher
    always has an accurate picture of in-flight agents. Uses a synchronous SQLite
    read (<1ms) — safe to call from the main thread.
    """
    try:
        active = _session_store.get_active_sessions()
        return _session_store.format_active_sessions_block(active)
    except Exception:
        return ""


def _prepend_sessions_prefix(prefix: str, results: list[TextContent]) -> list[TextContent]:
    """Prepend active-sessions block to the first TextContent item if prefix is non-empty.

    Returns the (possibly modified) list unchanged in structure — only the text
    of the first element is prepended. This is a pure transformation.
    """
    if not prefix or not results:
        return results
    first = results[0]
    new_text = prefix + "\n\n" + first.text
    return [TextContent(type=first.type, text=new_text)] + results[1:]


async def handle_wait_for_messages(args: dict) -> list[TextContent]:
    """Block until new messages arrive in inbox, or return immediately if messages exist."""
    timeout = args.get("timeout", 72000)
    hibernate_on_timeout = args.get("hibernate_on_timeout", False)

    # Option A: tag-on-first-use.  wait_for_messages is dispatcher-exclusive,
    # so the session calling it is the dispatcher.  Tag it now so subsequent
    # guarded tool calls from this same session are allowed.
    if _http_session_manager is not None:
        session_id = _get_current_http_session_id()
        if session_id is not None:
            _tag_dispatcher_session(session_id)

    # Phase 2: SQLite dispatcher lock — enforce single-dispatcher structurally.
    # Take (or replace stale) dispatcher lock so a second concurrent loop cannot
    # run alongside this one.  We check whether the existing lock holder is still
    # an active HTTP session before taking over; if it is, we log a warning and
    # proceed anyway (fail-open) rather than blocking the dispatcher entirely.
    if _http_session_manager is not None:
        session_id = _get_current_http_session_id()
        if session_id is not None:
            existing_lock = _claims_db.get_dispatcher_lock()
            if existing_lock is not None and existing_lock["session_id"] != session_id:
                # Another session holds the lock — check whether it is still active.
                old_session_id = existing_lock["session_id"]
                lock_holder_active = False
                try:
                    lock_holder_active = _http_session_manager.has_session(old_session_id)
                except Exception:
                    lock_holder_active = False
                if lock_holder_active:
                    log.warning(
                        f"[dispatcher-lock] Second dispatcher detected: "
                        f"session {session_id!r} called wait_for_messages while "
                        f"{old_session_id!r} holds an active lock. "
                        "Allowing takeover to unblock the main loop."
                    )
                else:
                    log.info(
                        f"[dispatcher-lock] Stale lock from {old_session_id!r} — "
                        f"taking over for {session_id!r}"
                    )
            _claims_db.force_replace_dispatcher_lock(session_id)

    # Touch heartbeat at start - signals Claude is alive and waiting for messages
    touch_heartbeat()

    # Write "active" state so the health check always sees a live signal when
    # wait_for_messages is called.  This is the authoritative steady-state write:
    # even if claude-persistent.sh left the state in "starting" or another
    # transient mode, calling wait_for_messages means Claude is up and running.
    _write_lobster_state(LOBSTER_STATE_FILE, "active")

    # Write WFM-active signal so the health check can distinguish a healthy
    # idle-blocking dispatcher from a frozen one (issue #1713 / #949).
    # The health check treats a fresh WFM-active signal as GREEN even when
    # dispatcher-heartbeat is stale — PostToolUse hooks don't fire during a
    # blocking MCP call, so the heartbeat inevitably goes stale after 20 min.
    # We clear this file in the finally block so the signal only persists while
    # WFM is actually blocking.
    _write_wfm_active_signal()

    # Recover stale processing and retryable failed messages
    _recover_stale_processing()
    _recover_retryable_messages()

    # Flood detection: drain reconciler startup ghosts before the dispatcher
    # sees the inbox. This prevents the 100+ ghost WFM cycle storm that occurs
    # when the reconciler startup sweep generates stale subagent_result messages
    # for every dead session it finds (issue #1420).
    _drained_ghosts = _drain_reconciler_ghosts()

    # Build active-sessions prefix once (fast SQLite read, <1ms)
    sessions_prefix = _build_active_sessions_prefix()

    # Prepend flood drain summary to sessions_prefix so the dispatcher is informed
    # without requiring an extra WFM cycle or manual inbox check.
    if _drained_ghosts > 0:
        drain_notice = (
            f"[flood-drain] Auto-drained {_drained_ghosts} reconciler ghost(s) "
            f"from startup sweep (elapsed<{GHOST_ELAPSED_THRESHOLD_SECONDS}s, "
            f"within {STARTUP_WINDOW_SECONDS}s of server start). "
            "No dispatcher action needed — these were stale completion notices with no real work."
        )
        sessions_prefix = (drain_notice + "\n\n" + sessions_prefix) if sessions_prefix else drain_notice

    # Start the observer BEFORE the initial glob check to eliminate the TOCTOU
    # race window: a message that arrives between the glob and observer.start()
    # would previously be missed until the next timeout/self-check.
    message_arrived = threading.Event()

    class InboxHandler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory and event.src_path.endswith('.json'):
                message_arrived.set()
        def on_moved(self, event):
            if not event.is_directory and event.dest_path.endswith('.json'):
                message_arrived.set()

    observer = Observer()
    observer.schedule(InboxHandler(), str(INBOX_DIR), recursive=False)
    observer.start()

    # Start a daemon thread to refresh the WFM-active heartbeat on wall-clock
    # time, independent of the asyncio event loop (issue #1823).  The event
    # loop can stall for minutes during Claude API streaming; the daemon thread
    # keeps writing so the health check never sees a false-stale signal.
    # daemon=True means the thread dies with the process — no heartbeats
    # survive a crash.  The stop_event lets the finally block terminate the
    # thread cleanly on normal exit.
    _hb_stop = threading.Event()
    _hb_thread = threading.Thread(
        target=_wfm_heartbeat_thread_fn,
        args=(_hb_stop, WAIT_HEARTBEAT_INTERVAL),
        daemon=True,
        name="wfm-heartbeat",
    )
    _hb_thread.start()

    try:
        # Now that the observer is running, check for messages that already
        # existed before we started watching.  Any message that arrives from
        # this point onward will set message_arrived, so nothing can slip
        # through the gap.
        existing = list(INBOX_DIR.glob("*.json"))
        if existing:
            # Messages already waiting - return them immediately
            touch_heartbeat()
            inbox_results = await handle_check_inbox({"limit": 10})
            return _prepend_sessions_prefix(sessions_prefix, inbox_results)
        # Wait loop: block until a message arrives or timeout expires.
        # Heartbeat and WFM-active refresh are handled by the daemon thread
        # started above — they no longer depend on this event-loop coroutine.
        heartbeat_interval = WAIT_HEARTBEAT_INTERVAL
        elapsed = 0

        while elapsed < timeout:
            wait_time = min(heartbeat_interval, timeout - elapsed)

            arrived = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda wt=wait_time: message_arrived.wait(timeout=wt)
            )

            if arrived:
                break

            elapsed += wait_time

        if message_arrived.is_set():
            # Small delay to ensure file is fully written
            await asyncio.sleep(0.1)
            touch_heartbeat()
            log.info("New message(s) arrived in inbox")
            inbox_results = await handle_check_inbox({"limit": 10})
            return _prepend_sessions_prefix(sessions_prefix, inbox_results)
        else:
            # Timeout expired with no messages
            touch_heartbeat()
            log.info(f"wait_for_messages timed out after {timeout}s")

            if hibernate_on_timeout:
                # Write hibernate state so the bot knows to wake us on next message.
                # Guard: only write the state file when running inside the designated
                # main Lobster session.  Non-main sessions (including tests and
                # subagent instances) must never overwrite the production state file.
                if _is_main_session():
                    _write_lobster_state(LOBSTER_STATE_FILE, "hibernate")
                    log.info("Hibernating: wrote state=hibernate, signalling graceful exit")
                else:
                    log.info(
                        "Hibernating: skipped state write — not the main session "
                        "(session guard prevented production state file mutation)"
                    )
                return [TextContent(
                    type="text",
                    text=(
                        f"💤 No messages received in {timeout}s. "
                        "Hibernating: state written as 'hibernate'. "
                        "The bot will restart Claude when the next message arrives. "
                        "EXIT now by stopping your main loop."
                    ),
                )]

            timeout_text = f"⏰ No messages received in the last {timeout} seconds. Call `wait_for_messages` again to continue waiting."
            if sessions_prefix:
                timeout_text = sessions_prefix + "\n\n" + timeout_text
            return [TextContent(type="text", text=timeout_text)]
    finally:
        # Stop the heartbeat daemon thread before writing the tombstone.
        # Setting the event wakes the thread's stop_event.wait() immediately
        # so it exits without waiting for the next interval tick.
        # join() ensures the thread has fully exited before we write the tombstone,
        # so no stale heartbeat can overwrite the tombstone after _clear_wfm_active_signal().
        _hb_stop.set()
        _hb_thread.join(timeout=1)
        observer.stop()
        observer.join(timeout=1)
        # Write tombstone to WFM_ACTIVE_FILE instead of deleting it (issue #1730).
        # Deleting the file creates a TOCTOU race: the health check's -f gate can
        # pass just before unlink(), then cat returns empty and declares RED.
        # Writing the non-integer tombstone WFM_ACTIVE_TOMBSTONE means the file is
        # never transiently absent — the health check treats non-integer content as
        # "WFM not active" (same as absent), with no race window.
        _clear_wfm_active_signal()


def _is_report_command(text: str) -> bool:
    """Return True if text is a /report slash command (with a description).

    Matches "/report <description>" at the start of the message.
    The command token must be exactly "/report" (case-insensitive), followed by
    whitespace and a non-empty description. Does NOT match "/reports" or other
    commands that begin with "/report" but have extra characters.
    """
    if not text:
        return False
    stripped = text.strip()
    lower = stripped.lower()
    # Must start with "/report" followed by whitespace or end of string
    if lower == "/report":
        return False  # No description — ignore bare command
    if lower.startswith("/report ") or lower.startswith("/report\t"):
        rest = stripped[len("/report"):].strip()
        return bool(rest)
    return False


def _extract_report_description(text: str) -> str:
    """Extract the description part from a /report command text."""
    stripped = text.strip()
    rest = stripped[len("/report"):].strip()
    return rest


async def _handle_report_slash_command(msg: dict, msg_file: Path) -> None:
    """Auto-handle a /report slash command message.

    Creates the report record, queues a confirmation reply to the user, and
    moves the message to processed/. This is called from handle_check_inbox
    before the message reaches the main dispatcher loop.

    Args:
        msg:      The parsed message dict from the inbox JSON file.
        msg_file: The Path to the inbox JSON file.
    """
    text = msg.get("text", "")
    chat_id = msg.get("chat_id", "")
    source = msg.get("source", "telegram")
    msg_id = msg.get("id", msg_file.stem)

    description = _extract_report_description(text)

    # Capture active agent sessions
    active_session_ids: list[str] = []
    try:
        active = _session_store.get_active_sessions()
        active_session_ids = [s.get("id", "") for s in active if s.get("id")]
    except Exception:
        pass

    snapshot_state = {
        "active_session_count": len(active_session_ids),
        "lobster_state": _read_lobster_state(),
    }

    # Store the report
    report_id: str | None = None
    try:
        report = _session_store.create_report(
            description=description,
            chat_id=chat_id,
            source=source,
            recent_messages=None,  # not captured in pre-processor to stay fast
            active_session_ids=active_session_ids if active_session_ids else None,
            snapshot_state=snapshot_state,
            instance_id=_INSTANCE_ID,
        )
        report_id = report["report_id"]
        log.info(f"/report pre-processor: created {report_id} for chat {chat_id}")
    except Exception as exc:
        log.error(f"/report pre-processor: failed to create report: {exc}", exc_info=True)
        # Do not send a misleading RPT-ERR ID to the user — the message remains
        # in the inbox so the dispatcher can handle it or retry.
        return

    # Send confirmation reply and mark processed atomically
    confirmation = f"Report filed as {report_id}. We'll look into it."
    try:
        await handle_send_reply({
            "chat_id": chat_id,
            "text": confirmation,
            "source": source,
            "message_id": msg_id,
        })
    except Exception as exc:
        log.error(f"/report pre-processor: send_reply failed: {exc}", exc_info=True)
        # Fall back to just marking processed
        try:
            dest = PROCESSED_DIR / msg_file.name
            msg_file.rename(dest)
        except Exception:
            pass


def _get_owner_chat_id_and_source() -> tuple[int | str | None, str]:
    """Return (owner_chat_id, source) for delivering recovery notifications.

    Resolution order:
      1. config.env — LOBSTER_ENABLE_SLACK / LOBSTER_SLACK_ALLOWED_CHANNELS
         and TELEGRAM_ALLOWED_USERS (same logic as _resolve_debug_config())
      2. Environment variables — TELEGRAM_ALLOWED_USERS and
         LOBSTER_SLACK_ALLOWED_CHANNELS as a fallback for environments where
         config.env is absent or incomplete (e.g. CI, test runners).

    Returns (None, "telegram") with a logged warning when the owner's
    chat_id cannot be resolved from either source.
    """
    try:
        slack_enabled = False
        slack_channel: str | None = None
        telegram_chat_id: int | None = None

        config_file = _CONFIG_DIR / "config.env"
        if config_file.exists():
            for line in config_file.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("TELEGRAM_ALLOWED_USERS="):
                    val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    first = val.split(",")[0].strip()
                    if first.lstrip("-").isdigit():
                        telegram_chat_id = int(first)
                elif stripped.startswith("LOBSTER_ENABLE_SLACK="):
                    val = stripped.split("=", 1)[1].strip().strip('"').strip("'").lower()
                    slack_enabled = val == "true"
                elif stripped.startswith("LOBSTER_SLACK_ALLOWED_CHANNELS="):
                    val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    first_chan = val.split(",")[0].strip()
                    if first_chan:
                        slack_channel = first_chan

        # Env var fallback: used when config.env is absent or the relevant
        # vars were not set there (e.g. CI / test environments).
        if telegram_chat_id is None:
            env_users = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip().strip('"').strip("'")
            first = env_users.split(",")[0].strip() if env_users else ""
            if first.lstrip("-").isdigit():
                telegram_chat_id = int(first)

        if not slack_channel:
            env_chans = os.environ.get("LOBSTER_SLACK_ALLOWED_CHANNELS", "").strip().strip('"').strip("'")
            first_chan = env_chans.split(",")[0].strip() if env_chans else ""
            if first_chan:
                slack_channel = first_chan

        if not slack_enabled:
            env_slack = os.environ.get("LOBSTER_ENABLE_SLACK", "").strip().lower()
            slack_enabled = env_slack == "true"

        if slack_enabled and slack_channel:
            return slack_channel, "slack"
        if telegram_chat_id is not None:
            return telegram_chat_id, "telegram"

        log.warning(
            "_get_owner_chat_id_and_source: owner chat_id not resolvable from "
            "config.env or environment variables"
        )
        return None, "telegram"
    except Exception:
        log.warning(
            "_get_owner_chat_id_and_source: unexpected error resolving owner chat_id",
            exc_info=True,
        )
        return None, "telegram"


def _enqueue_recovery_notification(msg: dict) -> None:
    """Write a subagent_notification to the owner's inbox when a subagent_recovered event arrives.

    The notification tells the owner which agent failed to write a result and
    includes a brief summary of the salvaged transcript content. It is delivered
    to the owner's chat_id (resolved from config.env) — not to the original
    chat_id carried by the recovery message, which is always 0 (unknown).

    Best-effort: any failure is logged but never raises.
    """
    try:
        owner_chat_id, owner_source = _get_owner_chat_id_and_source()
        if owner_chat_id is None:
            log.warning(
                "subagent_recovered: cannot enqueue recovery notification — "
                "owner chat_id not resolvable from config.env or environment variables"
            )
            return

        task_id = msg.get("task_id") or "unknown"
        raw_text = msg.get("text", "")

        # Extract a brief summary (first 300 chars of salvaged content, if any).
        summary_marker = "Recovered content:\n\n"
        summary: str
        if summary_marker in raw_text:
            salvaged = raw_text.split(summary_marker, 1)[1]
            summary = salvaged[:300].strip()
            if len(salvaged) > 300:
                summary += "…"
            summary_line = f"Last known activity: {summary}"
        else:
            summary_line = "No recoverable transcript content was found."

        notification_text = (
            f"Agent `{task_id}` failed to write a result (exited without calling write_result).\n"
            f"{summary_line}\n\n"
            "Consider relaunching the task if the result is needed."
        )

        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
        safe_task_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)[:40]
        notification_id = f"{ts_ms}_{safe_task_id}_recovery_notify"

        notification = {
            "id": notification_id,
            "type": "subagent_notification",
            "source": owner_source,
            "chat_id": owner_chat_id,
            "text": notification_text,
            "task_id": task_id,
            "status": "error",
            "sent_reply_to_user": False,
            "timestamp": now.isoformat(),
            "warning": (
                "Agent exited without calling write_result. "
                "Salvaged content was logged. Consider relaunching if result is needed."
            ),
        }

        inbox_file = INBOX_DIR / f"{notification_id}.json"
        atomic_write_json(inbox_file, notification)
        log.info(
            f"subagent_recovered: recovery notification enqueued for task {task_id!r} "
            f"→ owner chat_id={owner_chat_id!r} ({owner_source})"
        )
    except Exception as exc:
        log.error(f"subagent_recovered: failed to enqueue recovery notification: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Priority inbox queue — P0-P4 ordering (issue #1079)
# ---------------------------------------------------------------------------

# P0: dispatcher housekeeping — zero-cost, must run before everything else
_INBOX_P0_TYPES: frozenset[str] = frozenset()
_INBOX_P0_SUBTYPES: frozenset[str] = frozenset({"compact-reminder", "self_check"})
_INBOX_P0_TEXT_PREFIXES: tuple[str, ...] = ("compact-reminder",)

# P1: real-user messages — latency-sensitive
_INBOX_P1_TYPES: frozenset[str] = frozenset({"text", "voice", "photo", "document"})

# P2: completing in-flight work
_INBOX_P2_TYPES: frozenset[str] = frozenset({"subagent_result", "subagent_error"})

# P3: error recovery
_INBOX_P3_TYPES: frozenset[str] = frozenset({"agent_failed"})

# P4: background / cron — everything else
_INBOX_P4_DEFAULT: int = 4


def _inbox_priority(msg: dict) -> int:
    """Return the priority tier (0=highest, 4=lowest) for an inbox message.

    Priority is derived from message type and subtype at read time — nothing
    is stored in the message file itself. Unrecognised types default to P4.
    """
    msg_type = msg.get("type", "")
    msg_subtype = msg.get("subtype", "")
    msg_text = msg.get("text", "")

    # P0: dispatcher housekeeping
    if msg_subtype in _INBOX_P0_SUBTYPES:
        return 0
    if msg_type in _INBOX_P0_TYPES:
        return 0
    # compact-reminder messages are sometimes typed as "text" with a specific prefix
    if any(msg_text.startswith(p) for p in _INBOX_P0_TEXT_PREFIXES):
        return 0

    # P1: real-user messages
    if msg_type in _INBOX_P1_TYPES:
        return 1

    # P2: completing in-flight subagent work
    if msg_type in _INBOX_P2_TYPES:
        return 2

    # P3: error recovery
    if msg_type in _INBOX_P3_TYPES:
        return 3

    # P4: everything else (scheduled_reminder, system_error, unknown, ...)
    return _INBOX_P4_DEFAULT


def _inbox_sort_key(
    f_name: str, priority: int, ts_epoch: float | None
) -> tuple[int, float, str]:
    """Return (priority, timestamp_epoch, filename) for stable ordering.

    Lower priority tier = earlier in queue.
    Within a tier, older messages (smaller epoch) come first (FIFO).
    Filename is the final tiebreaker for determinism.
    """
    # Use a large sentinel so unparseable timestamps sink to the back of their tier
    epoch = ts_epoch if ts_epoch is not None else float("inf")
    return (priority, epoch, f_name)


def _parse_iso_timestamp(ts: str) -> float | None:
    """
    Parse an ISO 8601 UTC timestamp string to a Unix epoch float.

    Accepts formats like '2026-01-01T12:00:00Z', '2026-01-01T12:00:00+00:00',
    and '2026-01-01T12:00:00.000000'. Returns None if parsing fails.
    """
    if not ts:
        return None
    # Normalise: replace trailing Z with +00:00 for fromisoformat compat
    normalised = ts.strip().replace("Z", "+00:00")
    # If no timezone offset is present, assume UTC
    if "+" not in normalised and normalised.count("-") < 3:
        normalised = normalised + "+00:00"
    try:
        from datetime import datetime, timezone as _tz
        dt = datetime.fromisoformat(normalised)
        # Ensure timezone-aware; treat naive as UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


async def handle_check_inbox(args: dict) -> list[TextContent]:
    """Check for new messages in inbox.

    When since_ts is provided, scans both inbox/ and processed/ directories
    for messages with timestamp >= since_ts. This mode is designed for catch-up
    agents that need to recover context after compaction. The limit still applies
    to the combined result set.
    """
    source_filter = args.get("source", "").lower()
    limit = args.get("limit", 10)
    since_ts_str = args.get("since_ts", "")
    since_epoch = _parse_iso_timestamp(since_ts_str) if since_ts_str else None

    messages = []

    if since_epoch is not None:
        # Historical scan mode: read from both processed/ and inbox/, sorted by timestamp.
        # Skip dispatcher-internal system subtypes that are not useful for catch-up context.
        _CATCHUP_EXCLUDED_SUBTYPES = frozenset({
            "self_check",
            "compact-reminder",
            "compact_catchup",
            "subagent_notification",
        })

        all_files = sorted(
            list(PROCESSED_DIR.glob("*.json")) + list(INBOX_DIR.glob("*.json"))
        )
        for f in all_files:
            try:
                with open(f) as fp:
                    msg = json.load(fp)
                ts_str = msg.get("timestamp", "")
                msg_epoch = _parse_iso_timestamp(ts_str)
                if msg_epoch is None or msg_epoch < since_epoch:
                    continue
                if source_filter and msg.get("source", "").lower() != source_filter:
                    continue
                if msg.get("subtype") in _CATCHUP_EXCLUDED_SUBTYPES:
                    continue
                msg["_filename"] = f.name
                msg["_directory"] = f.parent.name  # "inbox" or "processed"
                messages.append(msg)
                if len(messages) >= limit:
                    break
            except Exception:
                continue

        if not messages:
            return [TextContent(type="text", text=f"📭 No messages found since {since_ts_str}.")]

        log.info(f"check_inbox (since_ts={since_ts_str!r}) returning {len(messages)} message(s)")
    else:
        # --- Priority-ordered inbox scan (issue #1079) ---
        # Two-pass approach: read and score all inbox files, then sort by
        # (priority, timestamp, filename) before processing. Unreadable files
        # default to P4 so they never cause a crash or a skip.
        _scored: list[tuple[tuple[int, float, str], object, dict]] = []
        for f in INBOX_DIR.glob("*.json"):
            try:
                with open(f) as fp:
                    msg = json.load(fp)
                ts_epoch = _parse_iso_timestamp(msg.get("timestamp", ""))
                priority = _inbox_priority(msg)
                key = _inbox_sort_key(f.name, priority, ts_epoch)
                _scored.append((key, f, msg))
            except Exception:
                # Unreadable file: assign worst possible key so it appears last
                key = (_INBOX_P4_DEFAULT, float("inf"), f.name)
                _scored.append((key, f, {}))

        for _key, f, msg in sorted(_scored, key=lambda x: x[0]):
            try:
                if not msg:
                    # Re-read files that failed the first pass (e.g. transient lock)
                    with open(f) as fp:  # type: ignore[arg-type]
                        msg = json.load(fp)
                # Quarantine files with unrecognized sources (issue #1735).
                # This check runs before source_filter so that a bad-source file
                # is always quarantined regardless of whether the caller passed a
                # source= filter.  An unknown source means the dispatcher cannot
                # route or dismiss the message, creating an infinite hot-loop:
                # wait_for_messages sees the file on every call and returns
                # immediately, exhausting --max-turns in ~7 minutes.  Move to
                # failed/ permanently so the file is preserved for inspection but
                # cannot block the loop.
                msg_source = msg.get("source", "")
                if msg_source and msg_source not in INBOX_MESSAGE_SOURCES:
                    error_msg = (
                        f"Unrecognized inbox source {msg_source!r} — not in INBOX_MESSAGE_SOURCES. "
                        "File quarantined to failed/ to prevent hot-loop. "
                        "Add to INBOX_MESSAGE_SOURCES in message_types.py if this is intentional."
                    )
                    log.error(f"check_inbox: quarantining {f.name}: {error_msg}")  # type: ignore[union-attr]
                    try:
                        msg["_permanently_failed"] = True
                        msg["_last_error"] = error_msg
                        msg["_last_failed_at"] = datetime.now(timezone.utc).isoformat()
                        dest = FAILED_DIR / f.name  # type: ignore[union-attr]
                        atomic_write_json(dest, msg)
                        f.unlink(missing_ok=True)  # type: ignore[union-attr]
                        _emit_mcp_event(
                            "inbox.failed",
                            {"message_id": f.stem, "error": error_msg, "permanent": True},  # type: ignore[union-attr]
                            severity="warn",
                        )
                    except Exception as _q_exc:
                        log.error(f"check_inbox: quarantine failed for {f}: {_q_exc}")  # type: ignore[union-attr]
                    continue  # skip — quarantined
                if source_filter and msg.get("source", "").lower() != source_filter:
                    continue
                # /report slash command pre-processor: handle automatically without
                # surfacing the raw message to the main dispatcher loop.
                msg_text = msg.get("text", "")
                if _is_report_command(msg_text):
                    try:
                        await _handle_report_slash_command(msg, f)  # type: ignore[arg-type]
                    except Exception as exc:
                        log.error(f"check_inbox: /report pre-processor error: {exc}", exc_info=True)
                    continue  # skip — already handled
                # subagent_recovered pre-processor: enqueue an owner notification so the
                # user is informed about the failed agent. The raw recovery message still
                # flows through to the dispatcher (with a dispatcher_hint) so it can call
                # mark_processed — but the salvaged dump is never relayed directly.
                if msg.get("type") == "subagent_recovered":
                    try:
                        _enqueue_recovery_notification(msg)
                    except Exception as exc:
                        log.error(f"check_inbox: subagent_recovered pre-processor error: {exc}", exc_info=True)
                msg["_filename"] = f.name  # type: ignore[union-attr]
                messages.append(msg)
                # NOTE: Inbound cross-Lobster messages from bot-talk are routed to this
                # inbox by bot_talk_mirror.log_inbound_cross_lobster() with source="bot-talk".
                # We no longer mirror owner Telegram messages to bot-talk from here —
                # only actual cross-Lobster exchanges belong in bot-talk (issue #1350).
                # Emit EventBus event for inbound bot-talk messages so TelegramOutboxListener
                # can forward them as debug notifications (issue #1425). The message is
                # already in the inbox so we only emit to EventBus, not route again.
                if msg.get("source") == "bot-talk" and msg.get("direction") == "INBOUND":
                    try:
                        from bot_talk_mirror import _spawn_mirror  # type: ignore[import]
                        _spawn_mirror(
                            content=msg.get("text", ""),
                            genre="status-update",
                            direction="INBOUND",
                            from_=msg.get("from", msg.get("user_name", "unknown")),
                            to=msg.get("to", ""),
                        )
                    except Exception as _bt_exc:
                        log.warning(f"bot-talk EventBus inbound emit failed (non-fatal): {_bt_exc}")
                if len(messages) >= limit:
                    break
            except Exception:
                continue

        if not messages:
            return [TextContent(type="text", text="📭 No new messages in inbox.")]

        log.info(f"check_inbox returning {len(messages)} message(s)")

    # Format messages nicely
    output = f"📬 **{len(messages)} new message(s):**\n\n"
    for msg in messages:
        source = msg.get("source", "unknown").upper()
        user = msg.get("user_name", msg.get("username", "Unknown"))
        text = msg.get("text", "(no text)")
        ts = msg.get("timestamp", "")
        msg_id = msg.get("id", msg.get("_filename", ""))
        chat_id = msg.get("chat_id", "")
        msg_type = msg.get("type", "text")

        output += f"---\n"
        # Add type-specific indicators
        if msg_type == "voice":
            output += f"**[{source}]** 🎤 from **{user}**\n"
            if not msg.get("transcription"):
                output += f"⚠️ Voice message needs transcription - use `transcribe_audio`\n"
        elif msg_type == "photo":
            _image_files_hdr = msg.get("image_files")
            if _image_files_hdr:
                count = len(_image_files_hdr)
                output += f"**[{source}]** 📷 from **{user}** ({count} photos)\n"
            else:
                output += f"**[{source}]** 📷 from **{user}**\n"
        elif msg_type == "document":
            file_name = msg.get("file_name", "file")
            output += f"**[{source}]** 📎 from **{user}** ({file_name})\n"
        elif msg_type in ("subagent_result", "subagent_error"):
            status_icon = "✅" if msg_type == "subagent_result" else "❌"
            label = "RESULT" if msg_type == "subagent_result" else "ERROR"
            task_id = msg.get("task_id", "?")
            output += f"{status_icon} **[SUBAGENT {label}]** for task `{task_id}`\n"
        elif msg_type == "subagent_notification":
            task_id = msg.get("task_id", "?")
            status_icon = "✅" if msg.get("status") != "error" else "❌"
            output += f"User already received the subagent's reply. Don't summarize it. If you respond, add new value only — a question, a correction, missing context.\n"
            output += f"{status_icon} **[SUBAGENT NOTIFICATION]** for task `{task_id}`\n"
        elif msg_type == "subagent_observation":
            category = msg.get("category", "unknown")
            task_id = msg.get("task_id", "")
            task_suffix = f" from task `{task_id}`" if task_id else ""
            output += f"**[OBSERVATION]** category=`{category}`{task_suffix}\n"
        elif msg_type == "subagent_recovered":
            task_id = msg.get("task_id", "?")
            output += f"⚠️ **[SUBAGENT RECOVERY]** task `{task_id}` exited without calling write_result — salvaged content logged\n"
        elif msg_type == "reaction":
            emoji = msg.get("emoji", "?")
            reacted_to_text = msg.get("reacted_to_text", "")
            if reacted_to_text:
                output += f"**[{source}]** {emoji} reaction from **{user}** (on: '{reacted_to_text}')\n"
            else:
                output += f"**[{source}]** {emoji} reaction from **{user}**\n"
        else:
            output += f"**[{source}]** from **{user}**\n"
        output += f"Chat ID: `{chat_id}` | Message ID: `{msg_id}`\n"
        tg_msg_id = msg.get("telegram_message_id")
        if tg_msg_id:
            output += f"Telegram Message ID: `{tg_msg_id}` (pass as reply_to_message_id to send_reply to thread your reply)\n"
        output += f"Time: {_format_ts_with_et(ts)}\n"
        # dispatcher_hint: structural signals for the dispatcher to route correctly
        if msg_type == "subagent_notification":
            output += "dispatcher_hint: SUBAGENT_NOTIFICATION — user already received the subagent's reply. Don't summarize it. If you respond, add new value only — a question, a correction, missing context. Call mark_processed when done.\n"
        if msg_type == "subagent_recovered":
            output += "dispatcher_hint: SUBAGENT_RECOVERED — agent exited without calling write_result; content was salvaged from transcript. The owner has been notified via inbox. Do NOT relay the raw dump to the user. Call mark_processed when done.\n"
        _has_file = msg_type in ("voice", "photo", "document") or bool(
            msg.get("image_file") or msg.get("image_files") or
            msg.get("file_path") or msg.get("audio_file")
        )
        if _has_file:
            output += "dispatcher_hint: HINT: file attached - use subagent\n"
        output += "\n"
        # Surface image file paths for photo messages so Claude can read them
        if msg_type == "photo":
            image_files = msg.get("image_files")
            image_file = msg.get("image_file")
            if image_files:
                output += f"**Image files**:\n"
                for img_path in image_files:
                    output += f"  - `{img_path}`\n"
                output += "\n"
            elif image_file:
                output += f"**Image file**: `{image_file}`\n\n"
        # Surface file path for document messages so Claude can read them
        if msg_type == "document":
            doc_file_path = msg.get("file_path")
            doc_file_name = msg.get("file_name", "file")
            if doc_file_path:
                output += f"**Attached file** (read to view): `{doc_file_path}`\n"
                output += f"Original name: {doc_file_name}\n\n"
            else:
                output += f"**Attached file**: {doc_file_name} (file not downloaded)\n\n"
        # Show full reply-to context if present
        reply_to = msg.get("reply_to")
        if reply_to:
            reply_text = reply_to.get("reply_to_text") or reply_to.get("text")
            reply_type = reply_to.get("reply_to_type", "text")
            reply_msg_id = reply_to.get("reply_to_message_id") or reply_to.get("message_id")
            reply_from = reply_to.get("reply_to_from_user") or reply_to.get("from_user")

            # Build the reply header line
            type_label = f" [{reply_type}]" if reply_type and reply_type != "text" else ""
            from_label = f" from @{reply_from}" if reply_from else ""
            id_label = f" (msg_id={reply_msg_id})" if reply_msg_id else ""
            output += f"↩️ Replying to{type_label}{from_label}{id_label}:\n"

            if reply_text:
                # Display the full text, indented for visual clarity
                indented = "\n".join(f"  {line}" for line in reply_text.splitlines())
                output += f"{indented}\n\n"
            else:
                output += f"  (no text content)\n\n"
        output += f"> {text}\n\n"

    output += "---\n"
    output += "Use `send_reply` to respond, `mark_processed` when done."

    return [TextContent(type="text", text=output)]



async def handle_send_reply(args: dict) -> list[TextContent]:
    """Send a reply to a message with input validation."""
    # Validate inputs — return error TextContent instead of raising so callers
    # (and tests) always receive a list[TextContent] back.
    try:
        args = validate_send_reply_args(args)
    except ValidationError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    chat_id = args["chat_id"]
    text = args["text"]
    source = args["source"]
    buttons = args.get("buttons")
    thread_ts = args.get("thread_ts")

    # Create reply file in outbox
    reply_id = f"{int(time.time() * 1000)}_{source}"
    reply_data = {
        "id": reply_id,
        "source": source,
        "chat_id": chat_id,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Include buttons if provided (Telegram only)
    if buttons and source == "telegram":
        reply_data["buttons"] = buttons

    # Include thread_ts if provided (Slack only)
    if thread_ts and source == "slack":
        reply_data["thread_ts"] = thread_ts

    # Include reply_to_message_id if explicitly provided (Telegram only).
    # No auto-threading fallback: if reply_to_message_id is absent, the reply is
    # sent standalone. Auto-threading was removed because it caused replies to
    # thread under the wrong message when multiple messages were in-flight for
    # the same chat simultaneously.
    reply_to_msg_id = args.get("reply_to_message_id")
    if source == "telegram" and reply_to_msg_id:
        reply_data["reply_to_message_id"] = int(reply_to_msg_id)

    # Route bisque replies to the bisque-outbox so the relay server picks them up.
    # All other sources go to the standard outbox for the bot process.
    if source == "bisque":
        outbox_file = BISQUE_OUTBOX_DIR / f"{reply_id}.json"
    else:
        outbox_file = OUTBOX_DIR / f"{reply_id}.json"

    # Atomic write: temp file + fsync + rename to prevent watchdog race condition
    atomic_write_json(outbox_file, reply_data)

    # Save a copy to sent directory for conversation history
    sent_file = SENT_DIR / f"{reply_id}.json"
    atomic_write_json(sent_file, reply_data)

    # BIS-167 Slice 6: persist outbound reply to messages.db (no-op when LOBSTER_USE_DB!=1)
    if _db_persist_outbound is not None:
        _db_persist_outbound(reply_data)

    # Track reply for mark_processed guard
    _track_reply(chat_id)

    # Record direct send for write_result deduplication (suppress duplicate relays)
    _record_direct_send(chat_id, text)

    # Task-ID-based dedup (primary): if task_id provided, record so write_result can
    # auto-set sent_reply_to_user=True even when texts differ (e.g. full reply vs short summary).
    task_id_param = args.get("task_id", "").strip() if args.get("task_id") else ""
    if task_id_param:
        _record_task_replied(task_id_param, chat_id)
        log.debug(f"Recorded task_id dedup for task={task_id_param!r} chat={chat_id}")

    log.info(f"Reply sent to {source} chat {chat_id}")

    # Emit telegram.outbound to EventBus for audit trail (issue #1352).
    # Skipped for bot-talk — that source already emits via bot_talk_mirror.
    if source != "bot-talk":
        _emit_mcp_event(
            "telegram.outbound",
            {"source": source, "chat_id": chat_id, "text_len": len(text)},
            chat_id=chat_id,
        )

    # Mirror outbound bot-talk messages to the EventBus so TelegramOutboxListener
    # can forward them as debug notifications. Fire-and-forget: non-blocking.
    if source == "bot-talk":
        try:
            from bot_talk_mirror import mirror_outbound  # type: ignore[import]
            mirror_outbound(text, source, chat_id)
        except Exception as _bt_exc:
            log.warning(f"bot-talk mirror_outbound failed (non-fatal): {_bt_exc}")

    # Atomic mark_processed: if message_id provided, move message to processed/ in same call
    mark_info = ""
    message_id = args.get("message_id")
    if message_id:
        try:
            mid = validate_message_id(message_id)
            found = _find_message_file(PROCESSING_DIR, mid)
            if not found:
                found = _find_message_file(INBOX_DIR, mid)
            if found:
                dest = PROCESSED_DIR / found.name
                found.rename(dest)
                mark_info = f" | message {mid} marked processed"
                log.info(f"Atomic mark_processed via send_reply: {mid}")
                # BIS-167 Slice 6: persist the inbound message now that it is processed
                if _db_persist_inbound is not None:
                    try:
                        _db_persist_inbound(json.loads(dest.read_text()))
                    except Exception as _db_exc:
                        log.warning(f"[DB] inbound persist failed for {mid}: {_db_exc}")
                # Per-message WFM heartbeat: reset the WFM staleness clock so
                # a long message batch does not exhaust the suppression window
                # and trigger a spurious health-check restart (issue #694).
                _update_lobster_state_fields(
                    {"last_processed_at": datetime.now(timezone.utc).isoformat()}
                )
            else:
                mark_info = f" | ⚠️ message {mid} not found for mark_processed"
                log.warning(f"Atomic mark_processed: message not found: {mid}")
        except Exception as e:
            mark_info = f" | ⚠️ mark_processed failed: {e}"
            log.warning(f"Atomic mark_processed failed for {message_id}: {e}")

    button_info = f" with {sum(len(row) for row in buttons)} button(s)" if buttons else ""
    thread_info = f" (thread reply)" if thread_ts and source == "slack" else ""
    return [TextContent(type="text", text=f"✅ Reply queued for {source} (chat {chat_id}){button_info}{thread_info}{mark_info}:\n\n{text[:100]}{'...' if len(text) > 100 else ''}")]


async def handle_send_whatsapp_reply(args: dict) -> list[TextContent]:
    """Send a WhatsApp message directly via Twilio REST API.

    This is a convenience wrapper around the Twilio client. For the standard
    send_reply flow (which routes through the outbox watcher), use send_reply
    with source='whatsapp' instead.
    """
    to = str(args.get("to", "")).strip()
    text = str(args.get("text", "")).strip()

    if not to:
        return [TextContent(type="text", text="Error: 'to' phone number is required")]
    if not text:
        return [TextContent(type="text", text="Error: 'text' message body is required")]

    # Route through the standard outbox mechanism so the whatsapp_router sends it.
    # This keeps a consistent audit trail and conversation history.
    reply_id = f"{int(time.time() * 1000)}_whatsapp"
    # Normalize: strip whatsapp: prefix for chat_id consistency
    chat_id = to.replace("whatsapp:", "").strip()

    reply_data = {
        "id": reply_id,
        "source": "whatsapp",
        "chat_id": chat_id,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    outbox_file = OUTBOX_DIR / f"{reply_id}.json"
    atomic_write_json(outbox_file, reply_data)

    sent_file = SENT_DIR / f"{reply_id}.json"
    atomic_write_json(sent_file, reply_data)

    # BIS-167 Slice 6: persist outbound reply to messages.db
    if _db_persist_outbound is not None:
        _db_persist_outbound(reply_data)

    _track_reply(chat_id)
    _record_direct_send(chat_id, text)

    log.info(f"WhatsApp reply queued for {chat_id}")
    return [TextContent(type="text", text=f"✅ WhatsApp message queued for {chat_id}:\n\n{text[:100]}{'...' if len(text) > 100 else ''}")]


async def handle_send_sms_reply(args: dict) -> list[TextContent]:
    """Send an SMS message via the outbox mechanism (sms_router picks it up)."""
    to = str(args.get("to", "")).strip()
    text = str(args.get("text", "")).strip()

    if not to:
        return [TextContent(type="text", text="Error: 'to' phone number is required")]
    if not text:
        return [TextContent(type="text", text="Error: 'text' message body is required")]

    reply_id = f"{int(time.time() * 1000)}_sms"
    chat_id = to.strip()

    reply_data = {
        "id": reply_id,
        "source": "sms",
        "chat_id": chat_id,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    outbox_file = OUTBOX_DIR / f"{reply_id}.json"
    atomic_write_json(outbox_file, reply_data)

    sent_file = SENT_DIR / f"{reply_id}.json"
    atomic_write_json(sent_file, reply_data)

    # BIS-167 Slice 6: persist outbound reply to messages.db
    if _db_persist_outbound is not None:
        _db_persist_outbound(reply_data)

    _track_reply(chat_id)
    _record_direct_send(chat_id, text)

    log.info(f"SMS reply queued for {chat_id}")
    return [TextContent(type="text", text=f"SMS message queued for {chat_id}:\n\n{text[:100]}{'...' if len(text) > 100 else ''}")]


async def handle_mark_processed(args: dict) -> list[TextContent]:
    """Mark a message as processed."""
    try:
        message_id = validate_message_id(args.get("message_id", ""))
    except ValidationError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    force = args.get("force", False)

    # Check processing/ first, then inbox/ as fallback
    found = _find_message_file(PROCESSING_DIR, message_id)
    if not found:
        found = _find_message_file(INBOX_DIR, message_id)

    if not found:
        return [TextContent(type="text", text=f"Message not found: {message_id}")]

    # Guard: log a warning if a user-facing message is being marked processed
    # without a prior send_reply.  This is a dispatcher bug — the dispatcher
    # should always reply to user messages before marking them processed.
    #
    # We intentionally do NOT auto-send any fallback reply here (issue #1594).
    # The previous implementation auto-sent "Noted." which is actively harmful:
    # it masquerades as an intentional response and caused repeated spurious
    # "Noted." messages to be delivered to the user for self-checks, subagent
    # completions, and other non-reply-requiring messages.
    #
    # Correct fix: log the missing reply so it's visible in monitoring, then
    # mark processed silently.  A missing reply is a dispatcher bug, not
    # something to paper over with an auto-reply.
    if not force:
        try:
            msg = json.loads(found.read_text())
            msg_type = msg.get("type", "")
            chat_id = msg.get("chat_id", 0)
            msg_ts_raw = msg.get("timestamp", "")

            if msg_type in USER_FACING_TYPES and chat_id != 0:
                # Parse message timestamp to epoch
                msg_epoch = 0.0
                if msg_ts_raw:
                    try:
                        dt = datetime.fromisoformat(msg_ts_raw)
                        msg_epoch = dt.timestamp()
                    except (ValueError, TypeError):
                        pass

                chat_key = str(chat_id)
                reply_ts = _recent_replies.get(chat_key, 0.0)
                if reply_ts < msg_epoch:
                    # No reply was sent for this human message — log and proceed silently.
                    log.warning(
                        f"mark_processed called without send_reply for user message "
                        f"{message_id} (type={msg_type}, chat={chat_id}) — "
                        "dispatcher may have dropped a reply"
                    )
        except (json.JSONDecodeError, OSError):
            pass  # If we can't read the message, skip the guard

    # Move to processed
    dest = PROCESSED_DIR / found.name
    found.rename(dest)

    # Update claim status to 'processed' (issue #1360).
    # No-op on rows that predate this migration (message_id absent from table).
    _claims_db.update_status(message_id, "processed")

    # BIS-167 Slice 6: persist inbound message to messages.db now that it is fully processed.
    if _db_persist_inbound is not None:
        try:
            _db_persist_inbound(json.loads(dest.read_text()))
        except Exception as _db_exc:
            log.warning(f"[DB] inbound persist failed for {message_id}: {_db_exc}")

    # Write a per-message heartbeat so the health check can distinguish a
    # dispatcher that is actively draining a long message batch from one that
    # is genuinely stuck.  The WFM freshness check uses the more recent of the
    # WFM heartbeat file and this timestamp, so a burst of 20+ cron pings
    # processed without returning to wait_for_messages does not exhaust the
    # suppression window and trigger a spurious restart (issue #694).
    _update_lobster_state_fields(
        {"last_processed_at": datetime.now(timezone.utc).isoformat()}
    )

    # Emit inbox.processed to EventBus for audit trail (issue #1352).
    _emit_mcp_event("inbox.processed", {"message_id": message_id})

    log.info(f"Message processed: {message_id}")
    return [TextContent(type="text", text=f"✅ Message marked as processed: {message_id}")]


async def handle_mark_processing(args: dict) -> list[TextContent]:
    """Move message from inbox to processing to claim it."""
    message_id = validate_message_id(args.get("message_id", ""))

    found = _find_message_file(INBOX_DIR, message_id)
    if not found:
        return [TextContent(type="text", text=f"Message not found in inbox: {message_id}")]

    # Read message content BEFORE moving (for observation queue)
    try:
        msg_data = json.loads(found.read_text())
    except Exception:
        msg_data = {}

    # Normalize type aliases to canonical names before any routing logic sees
    # the message (issue #635). This is the single ingest normalization point.
    msg_data = normalize_message_type(msg_data)

    # Atomic claim via SQLite INSERT OR FAIL (issue #1360).
    # This is the claim gate: one caller wins, all others get already_claimed.
    # The filesystem rename becomes a consequence of a won claim, not the claim
    # itself — eliminating the last-writer-wins race in concurrent dispatchers.
    session_id = _get_current_http_session_id() or "dispatcher"
    if not _claims_db.claim(message_id, session_id):
        log.warning(f"mark_processing: already_claimed: {message_id}")
        return [TextContent(type="text", text=f"Error: already_claimed: {message_id}")]

    # Atomic move to processing (consequence of won claim)
    dest = PROCESSING_DIR / found.name
    found.rename(dest)

    # Validate message type against the formal taxonomy (issue #156).
    # Non-blocking: unknown types are logged as a warning but not rejected,
    # to preserve backward compatibility with external producers (scripts,
    # bots) that may use ad-hoc types.
    msg_type = msg_data.get("type", "")
    msg_source = msg_data.get("source", "")
    if msg_type and msg_type not in INBOX_MESSAGE_TYPES:
        log.warning(
            f"mark_processing: unknown message type {msg_type!r} "
            f"(source={msg_source!r}, id={message_id}). "
            "Add to INBOX_MESSAGE_TYPES in message_types.py if this is intentional."
        )
    if msg_source and msg_source not in INBOX_MESSAGE_SOURCES:
        log.warning(
            f"mark_processing: unknown message source {msg_source!r} "
            f"(type={msg_type!r}, id={message_id}). "
            "Add to INBOX_MESSAGE_SOURCES in message_types.py if this is intentional."
        )

    # Queue background observation (non-blocking, best-effort)
    msg_text = msg_data.get("text", "") or msg_data.get("transcription", "")
    if msg_text and msg_type in USER_FACING_TYPES:
        _queue_observation(
            msg_text, message_id,
            source=msg_data.get("source"),
            ts=msg_data.get("timestamp"),
        )

    # Conditionally inject user model context for messages that would benefit
    context_block = ""
    short_msg_id = message_id[:20] if len(message_id) > 20 else message_id
    if _user_model is not None and msg_text and msg_type in USER_FACING_TYPES:
        if _should_inject_user_context(msg_text):
            try:
                ctx = _user_model.get_context()
                if ctx and ctx.strip():
                    context_block = (
                        "\n\n---\n"
                        "**User Model Context** (auto-injected for this message):\n\n"
                        f"{ctx}"
                    )
                    # Debug: notify context was injected
                    _emit_event(
                        f"\U0001f50d [context injected] msg={short_msg_id} "
                        f"trigger matched, injected {len(ctx)} chars of user model context",
                        event_type="user_model.context_inject",
                        severity="debug",
                        source="mark-processing",
                    )
                else:
                    # Debug: trigger matched but no context available.
                    _emit_event(
                        f"\U0001f50d [context skipped] msg={short_msg_id} "
                        "trigger matched but user model returned empty context",
                        event_type="user_model.context_skip",
                        severity="debug",
                        source="mark-processing",
                    )
            except Exception as _ctx_exc:
                import traceback as _tb
                _emit_event(
                    f"\U0001f50d [context inject error] msg={short_msg_id} "
                    f"{type(_ctx_exc).__name__}: {_ctx_exc}\n"
                    + _tb.format_exc()[-600:],
                    event_type="system.error",
                    severity="error",
                    source="mark-processing",
                )
                # never block mark_processing
        else:
            # No trigger match — context injection skipped. No notification emitted;
            # this is the common case and emitting on every no-match is pure noise.
            pass

    # User message counter — session-note-appender trigger (issue #1159)
    # Shared helper handles filtering, incrementing, and reminder injection.
    _tick_user_message_counter(msg_type, msg_source)

    # Stamp _processing_started_at so stale detection uses actual claim time, not file mtime.
    try:
        msg_data["_processing_started_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(dest, msg_data)
    except Exception:
        pass  # non-fatal; stale recovery falls back to mtime

    # Emit telegram.inbound to EventBus for audit trail (issue #1352).
    _emit_mcp_event(
        "telegram.inbound",
        {"message_id": message_id, "source": msg_source, "msg_type": msg_type},
        chat_id=msg_data.get("chat_id"),
    )

    log.info(f"Message claimed for processing: {message_id}")
    return [TextContent(type="text", text=f"Message claimed: {message_id}{context_block}")]


async def handle_claim_and_ack(args: dict) -> list[TextContent]:
    """Atomically claim a message for processing and send an acknowledgement reply.

    Combines mark_processing + send_reply in a single call, eliminating the
    window between claiming a message and notifying the user. If the claim step
    fails (message not found or already claimed), the ack is never sent.
    If the ack fails after claiming, the message remains in processing/ and
    stale recovery handles it.
    """
    message_id = validate_message_id(args.get("message_id", ""))

    # --- Step 1: Claim the message (must succeed before sending ack) ---
    found = _find_message_file(INBOX_DIR, message_id)
    if not found:
        return [TextContent(type="text", text=f"Error: Message not found in inbox: {message_id}")]

    # Atomic claim via SQLite INSERT OR FAIL (issue #1360).
    # Must succeed before any filesystem move or ack is sent.
    session_id = _get_current_http_session_id() or "dispatcher"
    if not _claims_db.claim(message_id, session_id):
        log.warning(f"claim_and_ack: already_claimed: {message_id}")
        return [TextContent(type="text", text=f"Error: already_claimed: {message_id}")]

    # Read message content before moving (for observation queue + timestamp)
    try:
        msg_data = json.loads(found.read_text())
    except Exception:
        msg_data = {}

    # Stamp actual processing start time so stale detection uses it
    msg_data["_processing_started_at"] = datetime.now(timezone.utc).isoformat()

    # Atomic move to processing/ (consequence of won claim)
    dest = PROCESSING_DIR / found.name
    found.rename(dest)

    # Write the updated JSON (with timestamp) to the processing file
    try:
        atomic_write_json(dest, msg_data)
    except Exception:
        pass  # non-fatal; stale recovery falls back to mtime

    # Queue background observation (non-blocking, best-effort)
    msg_text = msg_data.get("text", "") or msg_data.get("transcription", "")
    msg_type = msg_data.get("type", "")
    _SKIP_OBSERVATION_TYPES = (
        "subagent_result", "subagent_error", "self_check", "subagent_observation"
    )
    if msg_text and msg_type not in _SKIP_OBSERVATION_TYPES:
        _queue_observation(
            msg_text, message_id,
            source=msg_data.get("source"),
            ts=msg_data.get("timestamp"),
        )

    # User message counter — session-note-appender trigger (issue #1159)
    # Shared helper handles filtering, incrementing, and reminder injection.
    _tick_user_message_counter(msg_type, msg_data.get("source", ""))

    log.info(f"claim_and_ack: message claimed: {message_id}")

    # --- Step 2: Send the ack reply ---
    ack_args = {
        "chat_id": args["chat_id"],
        "text": args["ack_text"],
        "source": args.get("source", "telegram"),
    }
    if args.get("reply_to_message_id") is not None:
        ack_args["reply_to_message_id"] = args["reply_to_message_id"]
    if args.get("buttons") is not None:
        ack_args["buttons"] = args["buttons"]

    try:
        ack_result = await handle_send_reply(ack_args)
        ack_text_preview = args["ack_text"][:100]
        ack_suffix = "..." if len(args["ack_text"]) > 100 else ""
        log.info(f"claim_and_ack: ack sent to chat {args['chat_id']}")
        return [TextContent(
            type="text",
            text=(
                f"Claimed and acked: {message_id}\n\n"
                f"Ack: {ack_text_preview}{ack_suffix}"
            ),
        )]
    except Exception as e:
        # Ack failed — message stays in processing/, stale recovery handles it
        log.warning(f"claim_and_ack: ack failed (message stays in processing/): {e}")
        return [TextContent(
            type="text",
            text=(
                f"Warning: message claimed but ack send failed: {e}\n"
                f"Message {message_id} remains in processing/. Stale recovery will handle it."
            ),
        )]


async def handle_mark_failed(args: dict) -> list[TextContent]:
    """Mark a message as failed with optional retry."""
    message_id = validate_message_id(args.get("message_id", ""))
    error = args.get("error", "Unknown error")
    max_retries = args.get("max_retries", 3)

    # Find in processing/ first, then inbox/
    found = _find_message_file(PROCESSING_DIR, message_id)
    if not found:
        found = _find_message_file(INBOX_DIR, message_id)
    if not found:
        return [TextContent(type="text", text=f"Message not found: {message_id}")]

    # Read message, inject retry metadata
    msg = json.loads(found.read_text())
    retry_count = msg.get("_retry_count", 0) + 1
    msg["_retry_count"] = retry_count
    msg["_last_error"] = error
    msg["_last_failed_at"] = datetime.now(timezone.utc).isoformat()
    msg["_max_retries"] = max_retries

    if retry_count > max_retries:
        # Permanently failed
        msg["_permanently_failed"] = True
        dest = FAILED_DIR / found.name
        # Write destination FIRST, then remove source (crash-safe ordering)
        # If we crash after write but before unlink, we have a duplicate
        # which is safe (idempotent). The reverse loses data.
        atomic_write_json(dest, msg)
        found.unlink(missing_ok=True)
        # Update claim status to 'failed' (issue #1360)
        _claims_db.update_status(message_id, "failed")
        log.error(f"Message permanently failed after {max_retries} retries: {message_id} - {error}")
        # Emit inbox.failed to EventBus for audit trail (issue #1352).
        _emit_mcp_event(
            "inbox.failed",
            {"message_id": message_id, "error": error, "permanent": True},
            severity="warn",
        )
        return [TextContent(type="text", text=f"Message permanently failed after {max_retries} retries: {message_id}")]

    # Schedule retry with exponential backoff: 60s, 120s, 240s
    backoff = 60 * (2 ** (retry_count - 1))
    retry_at = datetime.now(timezone.utc).timestamp() + backoff
    msg["_retry_at"] = retry_at

    dest = FAILED_DIR / found.name
    # Write destination FIRST, then remove source (crash-safe ordering)
    atomic_write_json(dest, msg)
    found.unlink(missing_ok=True)
    # Release claim row so the message can be re-claimed on retry (issue #1360)
    _claims_db.release(message_id)
    log.warning(f"Message failed (retry {retry_count}/{max_retries}, next in {backoff}s): {message_id} - {error}")
    # Emit inbox.failed to EventBus for audit trail (issue #1352).
    _emit_mcp_event(
        "inbox.failed",
        {"message_id": message_id, "error": error, "permanent": False},
        severity="warn",
    )
    return [TextContent(type="text", text=f"Message queued for retry ({retry_count}/{max_retries}, backoff {backoff}s): {message_id}")]


async def handle_list_sources(args: dict) -> list[TextContent]:
    """List available message sources."""
    output = "📡 **Message Sources:**\n\n"
    for key, source in SOURCES.items():
        status = "✅ Enabled" if source["enabled"] else "❌ Disabled"
        output += f"- **{source['name']}** ({key}): {status}\n"

    return [TextContent(type="text", text=output)]


async def handle_get_stats(args: dict) -> list[TextContent]:
    """Get inbox statistics.

    BIS-164 Slice 3: augments filesystem counters with aggregate counts from
    messages.db when the DB is available, providing a richer picture of
    total message history beyond the current filesystem snapshot.
    """
    inbox_count = len(list(INBOX_DIR.glob("*.json")))
    outbox_count = len(list(OUTBOX_DIR.glob("*.json")))
    processed_count = len(list(PROCESSED_DIR.glob("*.json")))
    processing_count = len(list(PROCESSING_DIR.glob("*.json")))
    failed_count = len(list(FAILED_DIR.glob("*.json")))

    # Count retry-pending vs permanently failed
    retry_pending = 0
    permanently_failed = 0
    for f in FAILED_DIR.glob("*.json"):
        try:
            msg = json.loads(f.read_text())
            if msg.get("_permanently_failed"):
                permanently_failed += 1
            else:
                retry_pending += 1
        except Exception:
            continue

    # Count by source (filesystem snapshot — inbox only)
    source_counts: dict = {}
    for f in INBOX_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                msg = json.load(fp)
                src = msg.get("source", "unknown")
                source_counts[src] = source_counts.get(src, 0) + 1
        except (OSError, json.JSONDecodeError):
            continue

    output = "📊 **Inbox Statistics:**\n\n"
    output += f"- Inbox: {inbox_count} messages\n"
    output += f"- Processing: {processing_count} in progress\n"
    output += f"- Outbox: {outbox_count} pending replies\n"
    output += f"- Processed: {processed_count} total\n"
    output += f"- Failed: {failed_count} ({retry_pending} retry pending, {permanently_failed} permanent)\n\n"

    if source_counts:
        output += "**By Source (inbox):**\n"
        for src, count in source_counts.items():
            output += f"- {src}: {count}\n"

    # ------------------------------------------------------------------
    # DB aggregate stats (BIS-164 Slice 3)
    # ------------------------------------------------------------------
    if _db_get_message_stats is not None:
        conn = _open_messages_db_conn()
        if conn is not None:
            try:
                db_stats = _db_get_message_stats(conn)
                output += "\n**messages.db Totals:**\n"
                output += f"- Total messages: {db_stats.get('total_messages', 0)}\n"
                output += f"- Inbound: {db_stats.get('inbound_count', 0)}\n"
                output += f"- Outbound: {db_stats.get('outbound_count', 0)}\n"
                by_source = db_stats.get("by_source", {})
                if by_source:
                    output += "\n**DB By Source:**\n"
                    for src, cnt in by_source.items():
                        output += f"- {src}: {cnt}\n"
                ae_count = db_stats.get("agent_events_count", 0)
                be_count = db_stats.get("bisque_events_count", 0)
                if ae_count or be_count:
                    output += f"\n- Agent events: {ae_count}\n"
                    output += f"- Bisque events: {be_count}\n"
            except Exception as db_exc:
                log.debug("handle_get_stats: DB query failed: %s", db_exc)
            finally:
                conn.close()

    return [TextContent(type="text", text=output)]


# =============================================================================
# Conversation History Handler — BIS-165 Slice 4
# =============================================================================


def _open_messages_db_conn():
    """Open messages.db for reading and return the connection, or None if unavailable.

    Uses the connection factory from src/db/connection.py when available.
    Returns None when:
      - The db.connection module was not importable (src/db not on sys.path).
      - MESSAGES_DB_PATH does not exist yet (pre-migration deployment).
      - The database file cannot be opened for any reason.

    Callers are responsible for closing the returned connection.
    """
    if _db_open_messages_db is None:
        return None
    try:
        if not MESSAGES_DB_PATH.exists():
            return None
        return _db_open_messages_db(MESSAGES_DB_PATH)
    except Exception as exc:
        log.debug("messages.db open failed: %s", exc)
        return None


def _scan_json_dirs_for_history(direction: str) -> list[dict]:
    """Scan processed/ and sent/ JSON directories and return all message dicts.

    Pure function: reads files from disk, returns a list.  Each dict has
    _direction set to 'received' or 'sent' and _filename set to the
    source filename.  Used as the fallback when messages.db is unavailable.

    Args:
        direction: 'all' | 'received' | 'sent'
    """
    messages: list[dict] = []

    if direction in ("all", "received"):
        for f in PROCESSED_DIR.glob("*.json"):
            try:
                with open(f) as fp:
                    msg = json.load(fp)
                msg["_direction"] = "received"
                msg["_filename"] = f.name
                messages.append(msg)
            except Exception:
                continue

    if direction in ("all", "sent"):
        for f in SENT_DIR.glob("*.json"):
            try:
                with open(f) as fp:
                    msg = json.load(fp)
                msg["_direction"] = "sent"
                msg["_filename"] = f.name
                messages.append(msg)
            except Exception:
                continue

    return messages


def _apply_sender_type_filter(messages: list[dict], sender_type: str | None) -> list[dict]:
    """Return only messages matching the given sender_type.

    Pure function: does not mutate the input list.

    sender_type semantics mirror the SQL layer in db/reader.py:
      'user'         — inbound (_direction='received') messages whose type is in
                       INBOX_USER_TYPES (real user messages, no system/cron noise)
      'lobster'      — outbound (_direction='sent') messages
      'conversation' — union of user and lobster (both, but no system noise)
      None / other   — all messages unchanged
    """
    if not sender_type or sender_type == "all":
        return messages

    if sender_type == "user":
        return [
            m for m in messages
            if m.get("_direction") == "received" and m.get("type", "text") in INBOX_USER_TYPES
        ]

    if sender_type == "lobster":
        return [m for m in messages if m.get("_direction") == "sent"]

    if sender_type == "conversation":
        return [
            m for m in messages
            if m.get("_direction") == "sent"
            or (m.get("_direction") == "received" and m.get("type", "text") in INBOX_USER_TYPES)
        ]

    # Unknown value — degrade gracefully
    return messages


def _apply_filters_and_paginate(
    messages: list[dict],
    *,
    chat_id_filter,
    source_filter: str,
    search_text: str,
    limit: int,
    offset: int,
    sender_type: str | None = None,
) -> tuple[list[dict], int]:
    """Apply chat_id / source / search / sender_type filters, sort by timestamp, then paginate.


    Returns (paginated_slice, total_count_before_pagination).
    All filtering and sorting is performed in-memory. This is the legacy
    fallback path used when messages.db is unavailable.
    """
    def _parse_ts(msg: dict) -> 'datetime':
        ts = msg.get("timestamp", "")
        try:
            if "+" in ts or ts.endswith("Z"):
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    filtered = messages

    if sender_type:
        filtered = _apply_sender_type_filter(filtered, sender_type)

    if chat_id_filter is not None:
        chat_id_str = str(chat_id_filter)
        filtered = [m for m in filtered if str(m.get("chat_id", "")) == chat_id_str]

    if source_filter:
        filtered = [m for m in filtered if m.get("source", "").lower() == source_filter]

    if search_text:
        search_lower = search_text.lower()
        filtered = [m for m in filtered if search_lower in m.get("text", "").lower()]

    filtered.sort(key=_parse_ts, reverse=True)

    total = len(filtered)
    return filtered[offset: offset + limit], total


_HISTORY_TEXT_DISPLAY_LIMIT = 4000  # Max chars shown per message in get_conversation_history


def _format_history_output(
    paginated: list[dict],
    total_count: int,
    offset: int,
    limit: int,
) -> str:
    """Render a list of message dicts into the conversation history markdown string.

    Each dict must have _direction set to 'received' or 'sent'.
    Fields source, chat_id, timestamp, text, user_name, username are optional.
    Messages longer than _HISTORY_TEXT_DISPLAY_LIMIT chars are shown with a
    [truncated] suffix so the caller knows the content was cut.
    """
    showing_end = min(offset + limit, total_count)
    output = f"**Conversation History** (showing {offset + 1}-{showing_end} of {total_count}):\n\n"

    for msg in paginated:
        direction_icon = "\u2b05\ufe0f" if msg["_direction"] == "received" else "\u27a1\ufe0f"
        direction_label = "RECEIVED" if msg["_direction"] == "received" else "SENT"
        source = msg.get("source", "unknown").upper()
        chat_id = msg.get("chat_id", "")
        ts = msg.get("timestamp", "")
        text = msg.get("text", "(no text)")

        try:
            ts_display = _format_iso_for_display(ts, "%Y-%m-%d %I:%M %p %Z")
        except (ValueError, TypeError):
            ts_display = ts

        was_truncated = len(text) > _HISTORY_TEXT_DISPLAY_LIMIT
        truncated = text[:_HISTORY_TEXT_DISPLAY_LIMIT] + (" [truncated]" if was_truncated else "")
        if msg["_direction"] == "received":
            user = msg.get("user_name", msg.get("username", "Unknown"))
            output += "---\n"
            output += f"{direction_icon} **{direction_label}** [{source}] from **{user}** | Chat: `{chat_id}`\n"
            output += f"Time: {ts_display}\n\n"
            output += f"> {truncated}\n\n"
        else:
            output += "---\n"
            output += f"{direction_icon} **{direction_label}** [{source}] to chat `{chat_id}`\n"
            output += f"Time: {ts_display}\n\n"
            output += f"> {truncated}\n\n"

    if total_count > offset + limit:
        next_offset = offset + limit
        output += f"---\n*More messages available. Use `offset={next_offset}` to see the next page.*\n"

    return output


async def handle_get_conversation_history(args: dict) -> list[TextContent]:
    """Retrieve past messages from conversation history.

    BIS-165 Slice 4: SQL-first read path with extracted pure-function helpers.
    Queries messages.db when available; falls back to the legacy filesystem scan
    (processed/ + sent/ JSON files) when the DB is unavailable or returns zero
    results for the given filters.
    """
    chat_id_filter = args.get("chat_id")
    search_text = args.get("search", "").strip()
    limit = min(args.get("limit", 20), 100)
    offset = args.get("offset", 0)
    direction = args.get("direction", "all").lower()
    source_filter = args.get("source", "").lower().strip()
    sender_type = args.get("sender_type") or None  # None when omitted or empty string

    paginated: list[dict] = []
    total_count: int = 0
    used_db: bool = False

    # ------------------------------------------------------------------
    # Primary path: SQL query against messages.db (BIS-165 Slice 4)
    # ------------------------------------------------------------------
    if _db_get_conversation_history is not None and _db_count_conversation_history is not None:
        conn = _open_messages_db_conn()
        if conn is not None:
            try:
                paginated = _db_get_conversation_history(
                    conn,
                    chat_id=chat_id_filter,
                    source=source_filter or None,
                    search=search_text or None,
                    direction=direction,
                    sender_type=sender_type,
                    limit=limit,
                    offset=offset,
                )
                total_count = _db_count_conversation_history(
                    conn,
                    chat_id=chat_id_filter,
                    source=source_filter or None,
                    search=search_text or None,
                    direction=direction,
                    sender_type=sender_type,
                )
                used_db = True
                log.debug(
                    "get_conversation_history: DB returned %d rows (total=%d)",
                    len(paginated),
                    total_count,
                )
            except Exception as db_exc:
                log.warning(
                    "get_conversation_history: DB query failed, falling back to filesystem: %s",
                    db_exc,
                )
                paginated = []
                total_count = 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Fallback: filesystem scan (legacy / migration window)
    # Activates when DB is unavailable or returned zero results.
    # ------------------------------------------------------------------
    if not used_db or (total_count == 0 and offset == 0):
        all_messages = _scan_json_dirs_for_history(direction)
        paginated, total_count = _apply_filters_and_paginate(
            all_messages,
            chat_id_filter=chat_id_filter,
            source_filter=source_filter,
            search_text=search_text,
            limit=limit,
            offset=offset,
            sender_type=sender_type,
        )

    # ------------------------------------------------------------------
    # Format and return
    # ------------------------------------------------------------------
    if not paginated:
        filter_info = []
        if chat_id_filter is not None:
            filter_info.append(f"chat_id={chat_id_filter}")
        if search_text:
            filter_info.append(f"search='{search_text}'")
        if direction != "all" and not sender_type:
            filter_info.append(f"direction={direction}")
        if source_filter:
            filter_info.append(f"source={source_filter}")
        if sender_type:
            filter_info.append(f"sender_type={sender_type}")
        filter_str = f" (filters: {', '.join(filter_info)})" if filter_info else ""
        return [TextContent(type="text", text=f"No messages found{filter_str}.")]

    output = _format_history_output(paginated, total_count, offset, limit)
    return [TextContent(type="text", text=output)]

async def handle_get_message_by_telegram_id(args: dict) -> list[TextContent]:
    """Look up a specific message by its Telegram message ID.

    BIS-164 Slice 3: DB-first lookup.  Queries messages.db when available;
    falls back to scanning the filesystem JSON directories when the DB is
    unavailable or the message is not yet persisted.
    """
    tg_id = args.get("telegram_message_id")
    if tg_id is None:
        return [TextContent(type="text", text="Error: telegram_message_id is required.")]

    tg_id = int(tg_id)
    chat_id_filter = args.get("chat_id")
    chat_id_str = str(chat_id_filter) if chat_id_filter is not None else None

    # ------------------------------------------------------------------
    # Primary path: DB lookup (BIS-164 Slice 3)
    # ------------------------------------------------------------------
    if _db_get_message_by_telegram_id_fn is not None:
        conn = _open_messages_db_conn()
        if conn is not None:
            try:
                msg = _db_get_message_by_telegram_id_fn(conn, tg_id, chat_id=chat_id_filter)
                if msg is not None:
                    source = msg.get("source", "unknown").upper()
                    chat_id = msg.get("chat_id", "")
                    ts = msg.get("timestamp", "")
                    text = msg.get("text", "(no text)")
                    msg_type = msg.get("type", "text")
                    user = msg.get("user_name", msg.get("username", "Unknown"))

                    try:
                        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts_display = ts_dt.strftime("%Y-%m-%d %H:%M UTC")
                    except (ValueError, TypeError):
                        ts_display = ts

                    output = f"**Message found** (in `messages.db`):\n\n"
                    output += f"Telegram Message ID: `{tg_id}`\n"
                    output += f"Lobster Message ID: `{msg.get('id', '?')}` \n"
                    output += f"Source: {source} | Chat ID: `{chat_id}` | Type: `{msg_type}`\n"
                    output += f"From: **{user}**\n"
                    output += f"Time: {ts_display}\n\n"
                    output += f"**Text:**\n> {text}\n"

                    reply_to = msg.get("reply_to")
                    if reply_to:
                        reply_text = reply_to.get("reply_to_text") or reply_to.get("text", "") if isinstance(reply_to, dict) else ""
                        reply_from = reply_to.get("reply_to_from_user") or reply_to.get("from_user", "") if isinstance(reply_to, dict) else ""
                        reply_msg_id = msg.get("reply_to_message_id", "")
                        output += f"\n**Reply to** (TG ID `{reply_msg_id}`, from {reply_from}):\n> {str(reply_text)[:300]}{'...' if len(str(reply_text)) > 300 else ''}\n"

                    for field in ("image_file", "file_path", "audio_file"):
                        val = msg.get(field)
                        if val:
                            output += f"\n**Attached file** (`{field}`): `{val}`\n"

                    return [TextContent(type="text", text=output)]
            except Exception as db_exc:
                log.debug("handle_get_message_by_telegram_id: DB lookup failed: %s", db_exc)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Fallback: filesystem scan
    # ------------------------------------------------------------------
    search_dirs = [
        (PROCESSED_DIR, "processed"),
        (INBOX_DIR, "inbox"),
        (PROCESSING_DIR, "processing"),
        (FAILED_DIR, "failed"),
    ]

    for directory, dir_label in search_dirs:
        for f in directory.glob("*.json"):
            try:
                with open(f) as fp:
                    msg = json.load(fp)
            except Exception:
                continue

            if msg.get("telegram_message_id") != tg_id:
                continue

            if chat_id_str is not None and str(msg.get("chat_id", "")) != chat_id_str:
                continue

            source = msg.get("source", "unknown").upper()
            chat_id = msg.get("chat_id", "")
            ts = msg.get("timestamp", "")
            text = msg.get("text", "(no text)")
            msg_type = msg.get("type", "text")
            user = msg.get("user_name", msg.get("username", "Unknown"))

            try:
                if "+" in ts or ts.endswith("Z"):
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                else:
                    dt = datetime.fromisoformat(ts)
                ts_display = dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                ts_display = ts

            output = f"**Message found** (in `{dir_label}/`):\n\n"
            output += f"Telegram Message ID: `{tg_id}`\n"
            output += f"Lobster Message ID: `{msg.get('id', '?')}` \n"
            output += f"Source: {source} | Chat ID: `{chat_id}` | Type: `{msg_type}`\n"
            output += f"From: **{user}**\n"
            output += f"Time: {ts_display}\n\n"
            output += f"**Text:**\n> {text}\n"

            reply_to = msg.get("reply_to")
            if reply_to:
                reply_text = reply_to.get("reply_to_text") or reply_to.get("text", "")
                reply_from = reply_to.get("reply_to_from_user") or reply_to.get("from_user", "")
                reply_msg_id = reply_to.get("reply_to_message_id") or reply_to.get("message_id", "")
                output += f"\n**Reply to** (TG ID `{reply_msg_id}`, from {reply_from}):\n> {reply_text[:300]}{'...' if len(reply_text) > 300 else ''}\n"

            for field in ("image_file", "file_path", "audio_file"):
                val = msg.get(field)
                if val:
                    output += f"\n**Attached file** (`{field}`): `{val}`\n"
            image_files = msg.get("image_files")
            if image_files:
                output += f"\n**Image files:**\n"
                for img in image_files:
                    output += f"  - `{img}`\n"

            return [TextContent(type="text", text=output)]

    filter_note = f" in chat `{chat_id_filter}`" if chat_id_filter is not None else ""
    return [TextContent(
        type="text",
        text=f"No message found with Telegram message ID `{tg_id}`{filter_note}. "
             f"Searched: messages.db (if available), processed, inbox, processing, failed directories.",
    )]


# =============================================================================
# Task Management Handlers
# =============================================================================

def load_tasks() -> dict:
    """Load tasks from file."""
    try:
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"tasks": [], "next_id": 1}


def save_tasks(data: dict) -> None:
    """Save tasks to file atomically (crash-safe)."""
    atomic_write_json(TASKS_FILE, data)


async def handle_list_tasks(args: dict) -> list[TextContent]:
    """List all tasks."""
    status_filter = args.get("status", "all").lower()
    chat_id = args.get("chat_id")
    data = load_tasks()
    tasks = data.get("tasks", [])

    # Filter by chat_id: show tasks owned by this user + legacy tasks (no chat_id)
    if chat_id is not None:
        tasks = [t for t in tasks if t.get("chat_id") == chat_id or "chat_id" not in t]

    if status_filter != "all":
        tasks = [t for t in tasks if t.get("status", "").lower() == status_filter]

    if not tasks:
        return [TextContent(type="text", text="📋 No tasks found.")]

    # Group by status
    pending = [t for t in tasks if t.get("status") == "pending"]
    in_progress = [t for t in tasks if t.get("status") == "in_progress"]
    completed = [t for t in tasks if t.get("status") == "completed"]

    output = "📋 **Tasks:**\n\n"

    if in_progress:
        output += "**🔄 In Progress:**\n"
        for t in in_progress:
            output += f"  #{t['id']} {t['subject']}\n"
        output += "\n"

    if pending:
        output += "**⏳ Pending:**\n"
        for t in pending:
            output += f"  #{t['id']} {t['subject']}\n"
        output += "\n"

    if completed:
        output += "**✅ Completed:**\n"
        for t in completed:
            output += f"  #{t['id']} {t['subject']}\n"
        output += "\n"

    output += f"---\nTotal: {len(tasks)} task(s)"

    return [TextContent(type="text", text=output)]


async def handle_create_task(args: dict) -> list[TextContent]:
    """Create a new task."""
    subject = args.get("subject", "").strip()
    description = args.get("description", "").strip()
    chat_id = args.get("chat_id")

    if not subject:
        return [TextContent(type="text", text="Error: subject is required.")]

    data = load_tasks()
    task_id = data.get("next_id", 1)

    task = {
        "id": task_id,
        "subject": subject,
        "description": description,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if chat_id is not None:
        task["chat_id"] = chat_id

    data["tasks"].append(task)
    data["next_id"] = task_id + 1
    save_tasks(data)

    return [TextContent(type="text", text=f"✅ Task #{task_id} created: {subject}")]


async def handle_update_task(args: dict) -> list[TextContent]:
    """Update a task."""
    task_id = args.get("task_id")
    if task_id is None:
        return [TextContent(type="text", text="Error: task_id is required.")]

    data = load_tasks()
    task = None
    for t in data["tasks"]:
        if t["id"] == task_id:
            task = t
            break

    if not task:
        return [TextContent(type="text", text=f"Error: Task #{task_id} not found.")]

    # Update fields
    if "status" in args:
        status = args["status"].lower()
        if status in ["pending", "in_progress", "completed"]:
            task["status"] = status
        else:
            return [TextContent(type="text", text=f"Error: Invalid status '{status}'. Use: pending, in_progress, completed")]

    if "subject" in args:
        task["subject"] = args["subject"]

    if "description" in args:
        task["description"] = args["description"]

    task["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_tasks(data)

    status_emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(task["status"], "")
    return [TextContent(type="text", text=f"{status_emoji} Task #{task_id} updated: {task['subject']} [{task['status']}]")]


async def handle_get_task(args: dict) -> list[TextContent]:
    """Get task details."""
    task_id = args.get("task_id")
    if task_id is None:
        return [TextContent(type="text", text="Error: task_id is required.")]

    data = load_tasks()
    task = None
    for t in data["tasks"]:
        if t["id"] == task_id:
            task = t
            break

    if not task:
        return [TextContent(type="text", text=f"Error: Task #{task_id} not found.")]

    status_emoji = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(task["status"], "")

    output = f"📋 **Task #{task['id']}**\n\n"
    output += f"**Subject:** {task['subject']}\n"
    output += f"**Status:** {status_emoji} {task['status']}\n"
    if task.get("description"):
        output += f"\n**Description:**\n{task['description']}\n"
    output += f"\n**Created:** {task.get('created_at', 'N/A')}\n"
    output += f"**Updated:** {task.get('updated_at', 'N/A')}\n"

    return [TextContent(type="text", text=output)]


async def handle_delete_task(args: dict) -> list[TextContent]:
    """Delete a task."""
    task_id = args.get("task_id")
    if task_id is None:
        return [TextContent(type="text", text="Error: task_id is required.")]

    data = load_tasks()
    original_len = len(data["tasks"])
    data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]

    if len(data["tasks"]) == original_len:
        return [TextContent(type="text", text=f"Error: Task #{task_id} not found.")]

    save_tasks(data)
    return [TextContent(type="text", text=f"🗑️ Task #{task_id} deleted.")]


# =============================================================================
# IFTTT Behavioral Rules Handlers
# =============================================================================


def _resolve_action_ref(action_ref: str) -> str:
    """Fetch memory DB content for an action_ref ID.

    Returns the content string, or a descriptive fallback when the memory
    system is unavailable or the entry is not found.
    """
    if _memory_provider is None:
        return "(memory system unavailable)"
    if not action_ref:
        return "(no action_ref)"
    if not hasattr(_memory_provider, "get"):
        return "(memory backend does not support get-by-ID)"
    try:
        event = _memory_provider.get(int(action_ref))
        if event is None:
            return f"(memory entry {action_ref} not found)"
        return event.content
    except (ValueError, TypeError):
        return f"(action_ref '{action_ref}' is not a valid integer ID)"
    except Exception as e:
        log.warning(f"_resolve_action_ref: failed for action_ref={action_ref}: {e}")
        return f"(error resolving action_ref: {e})"


def _generate_rule_id(condition: str) -> str:
    """Derive a stable slug from the condition text, falling back to a UUID suffix."""
    import re
    slug = condition.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = slug[:48].rstrip("-")
    if not slug:
        slug = "rule"
    # Append short UUID fragment to avoid collisions
    slug = f"{slug}-{_uuid_mod.uuid4().hex[:6]}"
    return slug


async def handle_list_rules(args: dict) -> list[TextContent]:
    """List IFTTT behavioral rules."""
    enabled_only = bool(args.get("enabled_only", False))
    resolve = bool(args.get("resolve", False))
    rules = _ifttt_load_rules()
    if enabled_only:
        rules = _ifttt_get_enabled_rules(rules)

    if not rules:
        label = "enabled " if enabled_only else ""
        return [TextContent(type="text", text=f"No {label}rules found.")]

    lines = []
    for r in rules:
        enabled_flag = "" if r.get("enabled", True) else " [disabled]"
        entry = (
            f"[{r['id']}]{enabled_flag}\n"
            f"  condition:  {r['condition']}\n"
            f"  action_ref: {r['action_ref']}"
        )
        if resolve:
            content = _resolve_action_ref(r["action_ref"])
            entry += f"\n  action:     {content}"
        lines.append(entry)
    summary = f"Rules: {len(rules)} total" + (f" ({sum(1 for r in rules if r.get('enabled', True))} enabled)" if not enabled_only else "")
    output = "\n\n".join(lines) + f"\n\n---\n{summary}"
    return [TextContent(type="text", text=output)]


async def handle_add_rule(args: dict) -> list[TextContent]:
    """Add a new IFTTT behavioral rule.

    Stores action_content to the memory DB and uses the resulting entry ID
    as action_ref in the YAML index.
    """
    condition = (args.get("condition") or "").strip()
    action_content = (args.get("action_content") or "").strip()

    if not condition:
        return [TextContent(type="text", text="Error: condition is required.")]
    if not action_content:
        return [TextContent(type="text", text="Error: action_content is required.")]

    if _memory_provider is None:
        return [TextContent(type="text", text="Error: memory system is not available — cannot store action content.")]

    event = MemoryEvent(
        id=None,
        timestamp=datetime.now(timezone.utc),
        type="ifttt_action",
        source="internal",
        project=None,
        content=action_content,
        metadata={"tags": ["ifttt_rule"], "condition": condition},
    )
    try:
        action_ref = str(_memory_provider.store(event))
    except Exception as e:
        log.error(f"handle_add_rule: memory store failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error storing action to memory DB: {e}")]

    rule_id = _generate_rule_id(condition)
    rules = _ifttt_load_rules()
    updated = _ifttt_add_rule(rules, rule_id=rule_id, condition=condition, action_ref=action_ref)
    _ifttt_save_rules(updated)

    return [TextContent(type="text", text=f"Rule added: {rule_id} (action_ref: {action_ref})")]


async def handle_delete_rule(args: dict) -> list[TextContent]:
    """Delete an IFTTT behavioral rule by ID.

    Pass delete_memory=True to also delete the memory DB entry for action_ref.
    """
    rule_id = (args.get("rule_id") or "").strip()
    if not rule_id:
        return [TextContent(type="text", text="Error: rule_id is required.")]

    rules = _ifttt_load_rules()
    rule = _ifttt_find_rule(rules, rule_id)
    if rule is None:
        return [TextContent(type="text", text="false")]

    delete_memory = bool(args.get("delete_memory", False))
    memory_note = ""
    if delete_memory:
        action_ref = rule.get("action_ref", "")
        if action_ref and _memory_provider is not None and hasattr(_memory_provider, "delete"):
            try:
                deleted = _memory_provider.delete(int(action_ref))
                memory_note = f" (memory entry {action_ref} {'deleted' if deleted else 'not found'})"
            except (ValueError, TypeError):
                memory_note = f" (could not delete memory entry: action_ref '{action_ref}' is not a valid integer ID)"
            except Exception as e:
                log.warning(f"handle_delete_rule: memory delete failed for action_ref={action_ref}: {e}")
                memory_note = f" (memory delete failed: {e})"
        elif delete_memory and _memory_provider is None:
            memory_note = " (memory system unavailable — rule deleted, memory entry not removed)"

    updated = _ifttt_remove_rule(rules, rule_id)
    _ifttt_save_rules(updated)
    return [TextContent(type="text", text=f"true{memory_note}")]


async def handle_get_rule(args: dict) -> list[TextContent]:
    """Get a single IFTTT behavioral rule by ID."""
    rule_id = (args.get("rule_id") or "").strip()
    if not rule_id:
        return [TextContent(type="text", text="Error: rule_id is required.")]

    resolve = bool(args.get("resolve", False))
    rules = _ifttt_load_rules()
    rule = _ifttt_find_rule(rules, rule_id)
    if rule is None:
        return [TextContent(type="text", text="null")]

    output = (
        f"id:         {rule['id']}\n"
        f"condition:  {rule['condition']}\n"
        f"action_ref: {rule['action_ref']}\n"
        f"enabled:    {rule.get('enabled', True)}"
    )
    if resolve:
        content = _resolve_action_ref(rule["action_ref"])
        output += f"\naction:     {content}"
    return [TextContent(type="text", text=output)]


async def handle_update_rule(args: dict) -> list[TextContent]:
    """Soft-disable or re-enable an IFTTT behavioral rule by ID."""
    rule_id = (args.get("rule_id") or "").strip()
    if not rule_id:
        return [TextContent(type="text", text="Error: rule_id is required.")]

    enabled = args.get("enabled")
    if enabled is None:
        return [TextContent(type="text", text="Error: enabled is required.")]

    rules = _ifttt_load_rules()
    rule = _ifttt_find_rule(rules, rule_id)
    if rule is None:
        return [TextContent(type="text", text="null")]

    updated_rules = [{**r, "enabled": bool(enabled)} if r["id"] == rule_id else r for r in rules]
    _ifttt_save_rules(updated_rules)

    updated_rule = _ifttt_find_rule(updated_rules, rule_id)
    output = (
        f"id:         {updated_rule['id']}\n"
        f"condition:  {updated_rule['condition']}\n"
        f"action_ref: {updated_rule['action_ref']}\n"
        f"enabled:    {updated_rule.get('enabled', True)}"
    )
    return [TextContent(type="text", text=output)]


# =============================================================================
# Session File Management Handlers
# =============================================================================

# Pointer file written/read to track the current session
_SESSION_POINTER_FILE = Path("/tmp/lobster-current-session-file")

# Boilerplate placeholder text in session template summaries
_SESSION_SUMMARY_BOILERPLATE = "<1-3 sentence summary"


def _resolve_sessions_dir() -> Path:
    """Return the sessions directory, creating it if absent."""
    sessions_dir = _USER_CONFIG / "memory" / "canonical" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


def _resolve_session_template(sessions_dir: Path) -> str | None:
    """Return template content, or None if the template file does not exist."""
    template_path = sessions_dir / "session.template.md"
    if template_path.exists():
        return template_path.read_text()
    return None


def _next_sequence_number(sessions_dir: Path, date_str: str) -> int:
    """Return the next available sequence number for *date_str* (YYYYMMDD)."""
    existing = [
        p.stem for p in sessions_dir.glob(f"{date_str}-*.md")
        if p.stem != "session.template"
    ]
    if not existing:
        return 1
    numbers = []
    for stem in existing:
        parts = stem.split("-", 1)
        if len(parts) == 2:
            try:
                numbers.append(int(parts[1]))
            except ValueError:
                pass
    return max(numbers) + 1 if numbers else 1


def _session_id_to_path(sessions_dir: Path, session_id: str) -> Path | None:
    """Return the Path for *session_id*, or None if not found."""
    candidate = sessions_dir / f"{session_id}.md"
    return candidate if candidate.exists() else None


def _resolve_current_session_path(sessions_dir: Path) -> Path | None:
    """Return the 'current' session path via pointer file or latest today."""
    if _SESSION_POINTER_FILE.exists():
        try:
            pointer = Path(_SESSION_POINTER_FILE.read_text().strip())
            if pointer.exists():
                return pointer
        except Exception:
            pass

    # Fall back to the latest session file for today (UTC)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    candidates = sorted(sessions_dir.glob(f"{today}-*.md"))
    return candidates[-1] if candidates else None


def _extract_section_content(text: str, section_name: str) -> str | None:
    """Return the body text of a named H2 section, or None if not found."""
    pattern = re.compile(
        rf"^## {re.escape(section_name)}\s*\n(.*?)(?=\Z|^## )",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1) if m else None


def _replace_section_content(text: str, section_name: str, new_body: str) -> str | None:
    """
    Replace the body of the named H2 section with *new_body*.

    Returns the updated full text, or None if the section was not found.
    """
    pattern = re.compile(
        rf"(^## {re.escape(section_name)}\s*\n)(.*?)(?=\Z|^## )",
        re.MULTILINE | re.DOTALL,
    )
    if not pattern.search(text):
        return None
    body = new_body if new_body.endswith("\n") else new_body + "\n"
    return pattern.sub(lambda m: m.group(1) + body, text)


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a sibling temp file + rename."""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.rename(tmp_path, path)


async def handle_create_session_file(args: dict) -> list[TextContent]:
    """Create a new session file from the template."""
    sessions_dir = _resolve_sessions_dir()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    seq = _next_sequence_number(sessions_dir, today)
    session_id = f"{today}-{seq:03d}"
    file_path = sessions_dir / f"{session_id}.md"

    template = _resolve_session_template(sessions_dir)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if template:
        content = (
            template
            .replace("YYYYMMDD-NNN", session_id)
            .replace("<ISO timestamp, e.g. 2026-03-25T14:32:00Z>", now_iso)
        )
    else:
        content = (
            f"# Session {session_id}\n\n"
            f"**Started:** {now_iso}\n"
            f"**Ended:** active\n\n"
            "## Summary\n\n"
            "## Open Threads\n\n"
            "## Open Tasks\n\n"
            "## Open Subagents\n\n"
            "## Communication Channels\n\n"
            "## Notable Events\n"
        )

    _atomic_write(file_path, content)
    _atomic_write(_SESSION_POINTER_FILE, str(file_path))

    import json as _json
    result = _json.dumps({"path": str(file_path), "session_id": session_id})
    return [TextContent(type="text", text=result)]


async def handle_get_session_file(args: dict) -> list[TextContent]:
    """Read a session file by ID or return the current one."""
    import json as _json

    session_id = args.get("session_id", "current")
    sessions_dir = _resolve_sessions_dir()

    if session_id == "current":
        path = _resolve_current_session_path(sessions_dir)
        if path is None:
            return [TextContent(type="text", text='{"error": "No current session file found."}')]
        resolved_id = path.stem
    else:
        path = _session_id_to_path(sessions_dir, session_id)
        if path is None:
            return [TextContent(type="text", text=_json.dumps({"error": f"Session file not found: {session_id}"}))]
        resolved_id = session_id

    content = path.read_text(encoding="utf-8")
    result = _json.dumps({"path": str(path), "content": content, "session_id": resolved_id})
    return [TextContent(type="text", text=result)]


async def handle_update_session_file(args: dict) -> list[TextContent]:
    """Update a named H2 section in a session file."""
    import json as _json

    section = args.get("section")
    new_content = args.get("content")
    session_id = args.get("session_id", "current")

    if not section:
        return [TextContent(type="text", text='{"error": "section is required."}')]
    if new_content is None:
        return [TextContent(type="text", text='{"error": "content is required."}')]

    sessions_dir = _resolve_sessions_dir()

    if session_id == "current":
        path = _resolve_current_session_path(sessions_dir)
        if path is None:
            return [TextContent(type="text", text='{"error": "No current session file found."}')]
    else:
        path = _session_id_to_path(sessions_dir, session_id)
        if path is None:
            return [TextContent(type="text", text=_json.dumps({"error": f"Session file not found: {session_id}"}))]

    original = path.read_text(encoding="utf-8")
    updated = _replace_section_content(original, section, new_content)

    if updated is None:
        return [TextContent(type="text", text=_json.dumps({"error": f"Section not found: {section}"}))]

    _atomic_write(path, updated)
    result = _json.dumps({"path": str(path), "section": section, "updated": True})
    return [TextContent(type="text", text=result)]


async def handle_list_session_files(args: dict) -> list[TextContent]:
    """List session files, optionally filtered by date."""
    import json as _json

    date_filter = args.get("date")
    sessions_dir = _resolve_sessions_dir()

    pattern = f"{date_filter}-*.md" if date_filter else "*.md"
    candidates = sorted(
        p for p in sessions_dir.glob(pattern)
        if p.stem != "session.template" and re.match(r"^\d{8}-\d{3}$", p.stem)
    )

    def _has_content(path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8")
            body = _extract_section_content(text, "Summary") or ""
            stripped = body.strip()
            return len(stripped) > 50 and not stripped.startswith(_SESSION_SUMMARY_BOILERPLATE)
        except Exception:
            return False

    entries = [
        {"session_id": p.stem, "path": str(p), "has_content": _has_content(p)}
        for p in candidates
    ]
    return [TextContent(type="text", text=_json.dumps(entries))]


# =============================================================================
# Audio Transcription Handler (Local Whisper.cpp)
# =============================================================================

# Paths for local whisper.cpp transcription
FFMPEG_PATH = Path.home() / ".local" / "bin" / "ffmpeg"
WHISPER_CPP_PATH = _WORKSPACE / "whisper.cpp" / "build" / "bin" / "whisper-cli"
WHISPER_MODEL_PATH = _WORKSPACE / "whisper.cpp" / "models" / "ggml-small.bin"


async def convert_ogg_to_wav(ogg_path: Path, wav_path: Path) -> bool:
    """Convert OGG audio to WAV format using FFmpeg."""
    ffmpeg = str(FFMPEG_PATH) if FFMPEG_PATH.exists() else "ffmpeg"
    cmd = [
        ffmpeg, "-i", str(ogg_path),
        "-ar", "16000",  # 16kHz sample rate
        "-ac", "1",      # Mono
        "-y",            # Overwrite
        str(wav_path)
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()

    return proc.returncode == 0


async def run_whisper_cpp(audio_path: Path) -> tuple[bool, str]:
    """Run whisper.cpp CLI on an audio file. Returns (success, transcription_or_error)."""
    if not WHISPER_CPP_PATH.exists():
        return False, f"whisper.cpp not found at {WHISPER_CPP_PATH}"
    if not WHISPER_MODEL_PATH.exists():
        return False, f"Whisper model not found at {WHISPER_MODEL_PATH}"

    cmd = [
        str(WHISPER_CPP_PATH),
        "-m", str(WHISPER_MODEL_PATH),
        "-f", str(audio_path),
        "-l", "en",      # English language
        "-nt",           # No timestamps in output
        "--no-prints",   # Suppress progress output
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_msg = stderr.decode().strip() if stderr else "Unknown error"
        return False, f"whisper.cpp failed: {error_msg}"

    # Parse output - whisper.cpp outputs the transcription to stdout
    transcription = stdout.decode().strip()

    # Remove any remaining timing info if present (lines starting with [)
    lines = [line for line in transcription.split('\n') if not line.strip().startswith('[')]
    transcription = ' '.join(lines).strip()

    return True, transcription


async def handle_transcribe_audio(args: dict) -> list[TextContent]:
    """Transcribe a voice message using local whisper.cpp (small model)."""
    message_id = args.get("message_id", "")

    if not message_id:
        return [TextContent(type="text", text="Error: message_id is required.")]

    # Find the message file
    msg_file = None
    msg_data = None
    for f in INBOX_DIR.glob("*.json"):
        if message_id in f.name:
            msg_file = f
            break
        try:
            with open(f) as fp:
                data = json.load(fp)
                if data.get("id") == message_id:
                    msg_file = f
                    msg_data = data
                    break
        except (OSError, json.JSONDecodeError):
            continue

    if not msg_file:
        # Also check processing directory (messages claimed via mark_processing)
        for f in PROCESSING_DIR.glob("*.json"):
            if message_id in f.name:
                msg_file = f
                break
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    if data.get("id") == message_id:
                        msg_file = f
                        msg_data = data
                        break
            except (OSError, json.JSONDecodeError):
                continue

    if not msg_file:
        # Also check processed directory
        for f in PROCESSED_DIR.glob("*.json"):
            if message_id in f.name:
                msg_file = f
                break
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    if data.get("id") == message_id:
                        msg_file = f
                        msg_data = data
                        break
            except (OSError, json.JSONDecodeError):
                continue

    if not msg_file:
        return [TextContent(type="text", text=f"Error: Message not found: {message_id}")]

    # Load message data if not already loaded
    if not msg_data:
        with open(msg_file) as fp:
            msg_data = json.load(fp)

    # Check if it's a voice message
    if msg_data.get("type") != "voice":
        return [TextContent(type="text", text=f"Error: Message {message_id} is not a voice message.")]

    # Check if already transcribed
    if msg_data.get("transcription"):
        return [TextContent(type="text", text=f"✅ Already transcribed:\n\n{msg_data['transcription']}")]

    # Get the audio file path
    audio_path = Path(msg_data.get("audio_file", ""))
    if not audio_path.exists():
        return [TextContent(type="text", text=f"Error: Audio file not found: {audio_path}")]

    # Local whisper.cpp transcription
    try:
        # whisper.cpp requires 16 kHz mono WAV. Convert any non-WAV format.
        # This handles .ogg, .opus, .webm (Chrome MediaRecorder), .mp4, .m4a, etc.
        _NON_WAV_EXTS = {".ogg", ".oga", ".opus", ".webm", ".mp4", ".m4a", ".aac", ".flac", ".mp3"}
        if audio_path.suffix.lower() in _NON_WAV_EXTS:
            wav_path = audio_path.with_suffix(".wav")
            if not wav_path.exists():
                success = await convert_ogg_to_wav(audio_path, wav_path)
                if not success:
                    return [TextContent(type="text", text="Error: Failed to convert audio to WAV format.")]
            transcribe_path = wav_path
        else:
            transcribe_path = audio_path

        # Run whisper.cpp transcription
        success, result = await run_whisper_cpp(transcribe_path)

        if not success:
            return [TextContent(type="text", text=f"Error: {result}")]

        transcription = result
        if not transcription:
            return [TextContent(type="text", text="Error: Empty transcription returned.")]

        # Update the message file with transcription
        msg_data["transcription"] = transcription
        msg_data["text"] = transcription  # Replace placeholder text
        msg_data["transcribed_at"] = datetime.now(timezone.utc).isoformat()
        msg_data["transcription_model"] = "whisper.cpp-small"

        with open(msg_file, "w") as fp:
            json.dump(msg_data, fp, indent=2)

        return [TextContent(type="text", text=f"🎤 **Transcription complete (whisper.cpp small):**\n\n{transcription}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error during transcription: {str(e)}")]


# =============================================================================
# TTS Voice Note Handler
# =============================================================================

async def handle_send_voice_note(args: dict) -> list[TextContent]:
    """Synthesize text to a voice note and send via Telegram.

    Uses piper TTS locally (no cloud). Falls back to a text send_reply if TTS
    or the Telegram voice send fails, so the user always gets a response.
    """
    chat_id = str(args.get("chat_id", "")).strip()
    text = str(args.get("text", "")).strip()
    source = str(args.get("source", "telegram")).strip() or "telegram"

    if not chat_id:
        return [TextContent(type="text", text="Error: chat_id is required.")]
    if not text:
        return [TextContent(type="text", text="Error: text is required.")]

    # Non-Telegram sources: voice notes are not supported; fall back to text.
    if source != "telegram":
        log.info(f"send_voice_note: source={source!r} is not telegram — falling back to text")
        fallback_args = {"chat_id": chat_id, "text": text, "source": source}
        return await handle_send_reply(fallback_args)

    # Import TTS module lazily so missing piper doesn't crash the server.
    try:
        from tts.piper import text_to_voice_file  # type: ignore[import]
    except ImportError as e:
        log.warning(f"send_voice_note: tts module not importable ({e}) — falling back to text")
        return await handle_send_reply({"chat_id": chat_id, "text": text, "source": source})

    # Generate OGG voice file
    tts_result = text_to_voice_file(text)
    if not tts_result.ok:
        log.warning(f"send_voice_note: TTS failed ({tts_result.error}) — falling back to text")
        tts_result.cleanup()
        return await handle_send_reply({"chat_id": chat_id, "text": text, "source": source})

    ogg_path = tts_result.ogg_path
    try:
        # Write a voice-type outbox message pointing to the OGG file.
        # The Telegram bot's outbox handler reads this and calls bot.send_voice().
        import time as _time
        reply_id = f"{int(_time.time() * 1000)}_telegram_voice"
        reply_data = {
            "id": reply_id,
            "source": "telegram",
            "chat_id": chat_id,
            "type": "voice",
            "voice_path": str(ogg_path),
            "text": text,  # kept as fallback caption / for sent-messages history
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        outbox_file = OUTBOX_DIR / f"{reply_id}.json"
        atomic_write_json(outbox_file, reply_data)
        # Save to sent directory for conversation history
        sent_file = SENT_DIR / f"{reply_id}.json"
        atomic_write_json(sent_file, reply_data)

        log.info(f"send_voice_note: queued voice note to {chat_id} ({ogg_path})")
        return [TextContent(type="text", text=f"Voice note queued for delivery to chat {chat_id}.")]
    except Exception as e:
        log.error(f"send_voice_note: failed to queue voice note ({e}) — falling back to text")
        tts_result.cleanup()
        return await handle_send_reply({"chat_id": chat_id, "text": text, "source": source})


# =============================================================================
# Headless Browser Fetch Handler
# =============================================================================

async def handle_fetch_page(args: dict) -> list[TextContent]:
    """Fetch a web page using a headless browser, wait for JS to render, return text content."""
    url = args.get("url", "").strip()
    wait_seconds = args.get("wait_seconds", 3)
    timeout_seconds = args.get("timeout", 30)

    if not url:
        return [TextContent(type="text", text="Error: url is required.")]

    # Ensure URL has a scheme
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                java_script_enabled=True,
            )

            page = await context.new_page()

            # Navigate to the URL
            timeout_ms = timeout_seconds * 1000
            try:
                response = await page.goto(
                    url,
                    timeout=timeout_ms,
                    wait_until="domcontentloaded",
                )
            except Exception as nav_err:
                await browser.close()
                return [TextContent(type="text", text=f"Error navigating to {url}: {str(nav_err)}")]

            # Wait additional time for JS rendering
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            # Try to wait for network to be idle (best effort)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(10000, timeout_ms // 2))
            except Exception:
                pass  # Don't fail if networkidle times out

            # Get the final URL (after redirects)
            final_url = page.url

            # Get page title
            title = await page.title()

            # Extract text content, trying different strategies
            text_content = ""

            # Strategy 1: For Twitter/X, look for specific tweet content
            if "twitter.com" in url or "x.com" in url:
                try:
                    # Wait for tweet content to appear
                    await page.wait_for_selector('[data-testid="tweetText"]', timeout=8000)
                    # Get all tweet texts
                    tweet_elements = await page.query_selector_all('[data-testid="tweetText"]')
                    tweet_texts = []
                    for el in tweet_elements:
                        t = await el.inner_text()
                        if t.strip():
                            tweet_texts.append(t.strip())

                    # Get tweet author
                    author_elements = await page.query_selector_all('[data-testid="User-Name"]')
                    authors = []
                    for el in author_elements:
                        a = await el.inner_text()
                        if a.strip():
                            authors.append(a.strip())

                    if tweet_texts:
                        parts = []
                        for i, tweet in enumerate(tweet_texts[:10]):  # Limit to 10 tweets
                            author = authors[i] if i < len(authors) else ""
                            if author:
                                parts.append(f"{author}\n{tweet}")
                            else:
                                parts.append(tweet)
                        text_content = "\n\n---\n\n".join(parts)
                except Exception:
                    pass  # Fall through to generic extraction

            # Strategy 2: For articles, try to find main content
            if not text_content:
                try:
                    # Try common article selectors
                    for selector in ["article", "main", '[role="main"]', ".post-content", ".article-body", ".entry-content"]:
                        el = await page.query_selector(selector)
                        if el:
                            candidate = await el.inner_text()
                            if len(candidate.strip()) > len(text_content):
                                text_content = candidate.strip()
                except Exception:
                    pass

            # Strategy 3: Fall back to full body text
            if not text_content or len(text_content) < 50:
                try:
                    text_content = await page.inner_text("body")
                except Exception:
                    text_content = ""

            # Get HTTP status
            status_code = response.status if response else "unknown"

            await browser.close()

            # Clean up the text
            if text_content:
                # Remove excessive whitespace/newlines
                text_content = re.sub(r'\n{3,}', '\n\n', text_content)
                text_content = text_content.strip()

                # Truncate if very long
                max_len = 15000
                if len(text_content) > max_len:
                    text_content = text_content[:max_len] + f"\n\n... (truncated, {len(text_content)} total chars)"

            if not text_content:
                return [TextContent(
                    type="text",
                    text=f"Page loaded but no text content extracted.\n\nURL: {final_url}\nStatus: {status_code}\nTitle: {title}"
                )]

            # Build output
            header = f"**{title}**\nURL: {final_url}\nStatus: {status_code}\n\n---\n\n"
            return [TextContent(type="text", text=header + text_content)]

    except ImportError:
        return [TextContent(type="text", text="Error: Playwright is not installed. Run: pip install playwright && python -m playwright install chromium")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error fetching page: {str(e)}")]


# =============================================================================
# Scheduled Jobs Handlers (systemd timer backend)
# =============================================================================

from systemd_jobs import (
    validate_name as _sj_validate_name,
    validate_command as _sj_validate_command,
    validate_schedule as _sj_validate_schedule,
    normalize_schedule as _sj_normalize_schedule,
    create_job as _sj_create_job,
    list_jobs as _sj_list_jobs,
    update_job as _sj_update_job,
    delete_job as _sj_delete_job,
    get_scaffold as _sj_get_scaffold,
    _timer_path as _sj_timer_path,
    _service_path as _sj_service_path,
    _read_unit_field as _sj_read_unit_field,
    _is_lobster_unit as _sj_is_lobster_unit,
)


async def handle_create_scheduled_job(args: dict) -> list[TextContent]:
    """Create a new systemd-timer-backed scheduled job."""
    name = args.get("name", "").strip().lower()
    schedule = args.get("schedule", "").strip()
    command = args.get("command", "").strip()
    description = args.get("description", "").strip()

    err = _sj_validate_name(name)
    if err:
        return [TextContent(type="text", text=f"Error: {err}")]

    # Normalize converts cron expressions and validates via systemd-analyze
    schedule, err = _sj_normalize_schedule(schedule)
    if err:
        return [TextContent(type="text", text=f"Error: {err}")]

    err = _sj_validate_command(command)
    if err:
        return [TextContent(type="text", text=f"Error: {err}")]

    try:
        result = await _sj_create_job(name, schedule, command, description)
    except Exception as exc:
        return [TextContent(type="text", text=f"Error creating job '{name}': {exc}")]

    if result.status == "already_exists":
        return [TextContent(type="text", text=f"Job '{name}' already exists with the same schedule and command (no changes made).")]

    return [TextContent(type="text", text=(
        f"Created scheduled job '{name}'\n"
        f"Schedule: {schedule}\n"
        f"Command: {command}\n"
        f"Timer: /etc/systemd/system/lobster-{name}.timer\n"
        f"Service: /etc/systemd/system/lobster-{name}.service"
    ))]


async def handle_list_scheduled_jobs(args: dict) -> list[TextContent]:
    """List all lobster-managed systemd timer jobs."""
    try:
        jobs = await _sj_list_jobs()
    except Exception as exc:
        return [TextContent(type="text", text=f"Error listing jobs: {exc}")]

    if not jobs:
        return [TextContent(type="text", text="No lobster-managed scheduled jobs found.\n\nUse `create_scheduled_job` to create one.")]

    lines = ["**Scheduled Jobs:**\n"]
    for job in sorted(jobs, key=lambda j: j.name):
        status = "active" if job.active else "inactive"
        lines.append(f"**{job.name}** ({status})")
        lines.append(f"  Schedule: {job.schedule}")
        lines.append(f"  Command: {job.command}")
        lines.append(f"  Last run: {job.last_run or 'never'}")
        lines.append(f"  Next run: {job.next_run or 'unknown'}")
        lines.append("")

    lines.append(f"---\nTotal: {len(jobs)} job(s)")
    return [TextContent(type="text", text="\n".join(lines))]


async def handle_get_scheduled_job(args: dict) -> list[TextContent]:
    """Get details of a specific lobster-managed systemd timer job."""
    name = args.get("name", "").strip().lower()

    if not name:
        return [TextContent(type="text", text="Error: name is required")]

    timer = _sj_timer_path(name)
    service = _sj_service_path(name)

    if not timer.exists() or not _sj_is_lobster_unit(timer):
        return [TextContent(type="text", text=f"Error: Job '{name}' not found")]

    schedule = _sj_read_unit_field(timer, "OnCalendar") or "(unknown)"
    command = _sj_read_unit_field(service, "ExecStart") or "(unknown)"

    output = f"**Job: {name}**\n\n"
    output += f"**Schedule**: {schedule}\n"
    output += f"**Command**: {command}\n"
    output += f"**Timer unit**: /etc/systemd/system/lobster-{name}.timer\n"
    output += f"**Service unit**: /etc/systemd/system/lobster-{name}.service\n\n"
    output += "---\n\n**Timer unit contents:**\n\n```ini\n"
    try:
        output += timer.read_text()
    except OSError:
        output += "(unable to read)"
    output += "\n```\n\n**Service unit contents:**\n\n```ini\n"
    try:
        output += service.read_text()
    except OSError:
        output += "(unable to read)"
    output += "\n```"

    return [TextContent(type="text", text=output)]


async def handle_update_scheduled_job(args: dict) -> list[TextContent]:
    """Update schedule, command, and/or enabled state for an existing lobster job."""
    name = args.get("name", "").strip().lower()

    if not name:
        return [TextContent(type="text", text="Error: name is required")]

    schedule = args.get("schedule", "").strip() or None
    command = args.get("command", "").strip() or None
    enabled_raw = args.get("enabled")
    enabled = None  # type: bool | None
    if enabled_raw is not None:
        if isinstance(enabled_raw, bool):
            enabled = enabled_raw
        elif isinstance(enabled_raw, str):
            enabled = enabled_raw.lower() not in ("false", "0", "no")

    if schedule is not None:
        # Normalize converts cron expressions and validates via systemd-analyze
        schedule, err = _sj_normalize_schedule(schedule)
        if err:
            return [TextContent(type="text", text=f"Error: {err}")]

    if command is not None:
        err = _sj_validate_command(command)
        if err:
            return [TextContent(type="text", text=f"Error: {err}")]

    try:
        result = await _sj_update_job(name, schedule=schedule, command=command, enabled=enabled)
    except FileNotFoundError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error updating job '{name}': {exc}")]

    if not result.updated_fields:
        return [TextContent(type="text", text="No changes specified. Provide schedule, command, or enabled.")]

    return [TextContent(type="text", text=(
        f"Updated job '{name}':\n- " + "\n- ".join(result.updated_fields)
    ))]


async def handle_delete_scheduled_job(args: dict) -> list[TextContent]:
    """Delete a lobster-managed systemd timer job."""
    name = args.get("name", "").strip().lower()

    if not name:
        return [TextContent(type="text", text="Error: name is required")]

    try:
        result = await _sj_delete_job(name)
    except PermissionError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error deleting job '{name}': {exc}")]

    if result.status == "not_found":
        return [TextContent(type="text", text=f"Job '{name}' not found (nothing to delete).")]

    return [TextContent(type="text", text=f"Deleted job '{name}' (timer stopped, disabled, unit files removed).")]


async def handle_get_job_scaffold(args: dict) -> list[TextContent]:
    """Return a starter script template for a lobster scheduled job."""
    kind = args.get("kind", "poller").strip() or "poller"
    content = _sj_get_scaffold(kind)
    return [TextContent(type="text", text=f"**Job scaffold ({kind}):**\n\n```python\n{content}\n```")]


async def handle_check_task_outputs(args: dict) -> list[TextContent]:
    """Check recent task outputs."""
    since = args.get("since")
    limit = args.get("limit", 10)
    job_name_filter = args.get("job_name", "").strip().lower()

    # Get all output files, sorted by mtime descending (newest first).
    # Sorting by filename is unreliable: non-date-prefixed files (e.g. old
    # write_result artifacts) sort lexicographically ahead of date-prefixed
    # write_task_output files and would dominate every result page.
    all_files = list(TASK_OUTPUTS_DIR.glob("*.json"))
    output_files = sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True)

    if not output_files:
        return [TextContent(type="text", text="No task outputs yet.\n\nOutputs will appear here when scheduled jobs complete.")]

    outputs = []
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    for f in output_files:
        if len(outputs) >= limit:
            break

        try:
            with open(f) as fp:
                data = json.load(fp)

            # Skip files that are not write_task_output records.  The task-outputs
            # directory may contain stale write_result artifacts (schema: task_id/text)
            # from an older code path.  These have no job_name or output fields and
            # would render as "unknown" / "(no output)".  Silently skip them.
            if "job_name" not in data:
                continue

            # Filter by job name
            if job_name_filter and data.get("job_name", "").lower() != job_name_filter:
                continue

            # Filter by time
            if since_dt:
                try:
                    output_dt = datetime.fromisoformat(data.get("timestamp", "").replace("Z", "+00:00"))
                    if output_dt < since_dt:
                        continue
                except (ValueError, TypeError):
                    pass

            data["_filename"] = f.name
            outputs.append(data)

        except Exception:
            continue

    if not outputs:
        filter_msg = ""
        if job_name_filter:
            filter_msg = f" for job '{job_name_filter}'"
        if since:
            filter_msg += f" since {since}"
        return [TextContent(type="text", text=f"No task outputs found{filter_msg}.")]

    result = f"**Recent Task Outputs** ({len(outputs)}):\n\n"

    for out in outputs:
        job = out.get("job_name", "unknown")
        ts = out.get("timestamp", "")
        status = out.get("status", "unknown")
        output = out.get("output", "(no output)")
        duration = out.get("duration_seconds")

        status_icon = "" if status == "success" else ""
        duration_str = f" ({duration}s)" if duration else ""

        # Format timestamp nicely in owner's local timezone
        try:
            ts = _format_iso_for_display(ts, "%Y-%m-%d %I:%M %p %Z")
        except (ValueError, TypeError):
            pass

        result += f"---\n"
        result += f"**{job}** {status_icon} {ts}{duration_str}\n\n"
        result += f"> {output[:500]}{'...' if len(output) > 500 else ''}\n\n"

    return [TextContent(type="text", text=result)]


async def handle_write_task_output(args: dict) -> list[TextContent]:
    """Write output from a scheduled task."""
    job_name = args.get("job_name", "").strip().lower()
    output = args.get("output", "").strip()
    status = args.get("status", "success").lower()

    if not job_name:
        return [TextContent(type="text", text="Error: job_name is required")]
    if not output:
        return [TextContent(type="text", text="Error: output is required")]

    if status not in ["success", "failed"]:
        status = "success"

    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d-%H%M%S")

    output_data = {
        "job_name": job_name,
        "timestamp": now.isoformat(),
        "status": status,
        "output": output,
    }

    output_file = TASK_OUTPUTS_DIR / f"{timestamp_str}-{job_name}.json"
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    # Emit job.completed to EventBus for audit trail (issue #1352).
    _emit_mcp_event(
        "job.completed",
        {"job_name": job_name, "status": status, "output_len": len(output)},
        severity="warn" if status == "failed" else "info",
    )

    return [TextContent(type="text", text=f"Output recorded for job '{job_name}'")]


# =============================================================================
# Subagent Result Relay Handler
# =============================================================================

async def handle_write_result(args: dict) -> list[TextContent]:
    """Write a subagent result into the inbox so the main thread can relay it to the user.

    The message written has type 'subagent_result' (or 'subagent_error' on failure).
    The main thread's wait_for_messages / check_inbox loop will pick it up, call
    send_reply to deliver the text to the user, and mark it processed — keeping the
    main thread as the single point of user communication.
    """
    task_id = args.get("task_id", "").strip()
    chat_id = args.get("chat_id")
    text = args.get("text", "").strip()
    reply_text = (args.get("reply_text") or "").strip() or None
    source = args.get("source", "telegram").strip() or "telegram"
    status = args.get("status", "success")
    artifacts = args.get("artifacts") or []
    thread_ts = args.get("thread_ts")
    # Accept new name (sent_reply_to_user) with backward-compat alias (forward).
    # Semantics: sent_reply_to_user=True means subagent already called send_reply →
    # dispatcher should NOT relay. This is the inverse of the old `forward` field.
    if "sent_reply_to_user" in args:
        sent_reply_to_user = bool(args["sent_reply_to_user"])
    elif "forward" in args:
        # Legacy callers: forward=True meant "dispatcher relays" → sent_reply_to_user=False
        sent_reply_to_user = not bool(args["forward"])
    else:
        sent_reply_to_user = False  # default: dispatcher should relay

    if not task_id:
        return [TextContent(type="text", text="Error: task_id is required")]
    if chat_id is None:
        return [TextContent(type="text", text="Error: chat_id is required")]
    if not text:
        return [TextContent(type="text", text="Error: text is required")]

    # Server-side deduplication: promote sent_reply_to_user to True when the subagent
    # already delivered a reply directly via send_reply, preventing duplicates.
    #
    # Primary path — task_id registry: if send_reply was called with this task_id, the
    # (task_id, chat_id) pair is recorded in TASK_REPLIED_DIR.  This works even when
    # send_reply and write_result carry different texts (e.g. full reply vs short summary).
    if not sent_reply_to_user and _was_task_replied(task_id, chat_id):
        log.info(
            f"write_result dedup (task_id): suppressing relay for task {task_id!r} — "
            f"send_reply was already called with task_id={task_id!r} for chat {chat_id}"
        )
        sent_reply_to_user = True

    # Secondary path — text-hash fallback: catches cases where task_id was not passed
    # to send_reply but the texts happen to match.
    if not sent_reply_to_user and _was_sent_directly(chat_id, text):
        log.info(
            f"write_result dedup (text-hash): suppressing relay for task {task_id!r} — "
            f"identical message already sent directly to chat {chat_id}"
        )
        sent_reply_to_user = True

    if status not in ("success", "error"):
        status = "success"

    # When sent_reply_to_user=True the subagent already called send_reply directly.
    # Use a distinct message type so the dispatcher knows to read for situational
    # awareness and mark_processed without calling send_reply — no duplicate risk.
    if sent_reply_to_user:
        msg_type = "subagent_notification"
    else:
        msg_type = "subagent_result" if status == "success" else "subagent_error"

    now = datetime.now(timezone.utc)
    # Use millisecond timestamp + task_id fragment for a unique, sortable filename
    ts_ms = int(now.timestamp() * 1000)
    safe_task_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)[:40]
    message_id = f"{ts_ms}_{safe_task_id}"

    message = {
        "id": message_id,
        "type": msg_type,
        "source": source,
        "chat_id": chat_id,
        "text": text,
        "task_id": task_id,
        "status": status,
        "sent_reply_to_user": bool(sent_reply_to_user),
        "timestamp": now.isoformat(),
    }
    if msg_type == "subagent_notification":
        message["warning"] = "User already received the subagent's reply. Don't summarize it. If you respond, add new value only — a question, a correction, missing context."
    # reply_text: user-facing reply separate from the internal dispatcher summary (text).
    # Only stored when there is an active user relay path: sent_reply_to_user=False and
    # chat_id not in (0, "0") (chat_id 0 is dispatcher-internal; no user reply is ever sent).
    if reply_text and not sent_reply_to_user and chat_id not in (0, "0"):
        message["reply_text"] = reply_text
    if artifacts:
        message["artifacts"] = artifacts
    if thread_ts:
        message["thread_ts"] = thread_ts

    inbox_file = INBOX_DIR / f"{message_id}.json"
    atomic_write_json(inbox_file, message)

    # BIS-167 Slice 6: persist agent event immediately on write_result.
    # Before auto-unregister so the record is in the DB even if the dispatcher crashes.
    if _db_persist_agent_event is not None:
        _db_persist_agent_event(message)

    # Auto-unregister: mark this agent session as completed in the SQLite store.
    # The task_id passed to write_result matches the agent_id or task_id registered by the dispatcher.
    # This is the atomic "result delivered → agent done" guarantee described in issue #295.
    # Uses session_store.session_end directly so the completion status is recorded in history.
    try:
        _session_store.session_end(
            id_or_task_id=task_id,
            status="completed",
            result_summary=(text[:200] if text else None),
            stop_reason="end_turn",
        )
        # Mark notified immediately so the reconciler's startup sweep does not
        # re-enqueue this session on the next MCP restart.  The startup sweep
        # queries for completed/dead rows where notified_at IS NULL — leaving it
        # NULL here is what caused the April 4 flood (issue #1432).
        _session_store.set_notified(task_id)
    except Exception as exc:
        log.warning(f"write_result auto-unregister failed for task_id={task_id!r}: {exc}")

    # Notify wire server so SSE clients update within 40ms
    asyncio.create_task(_notify_wire_server())

    # Debug alert: emit bus event when LOBSTER_DEBUG=true.
    # Fires at the MCP layer (before the dispatcher picks up the inbox message)
    # so the user sees the subagent message arrive in real time.
    # system_context events are suppressed by TelegramOutboxListener — bus gate.
    agent_id = args.get("agent_id", "").strip() or None
    alert_lines = [
        f"\U0001f4e8 [subagent\u2192dispatcher] type: {msg_type}",
        f"task: {task_id}",
    ]
    if agent_id:
        alert_lines.append(f"agent: {agent_id}")
    if status:
        alert_lines.append(f"status: {status}")
    alert_lines.append(f"sent_reply: {bool(sent_reply_to_user)}")
    _emit_event(
        "\n".join(alert_lines),
        event_type="agent.write_result",
        severity="debug",
        source="write-result",
        emitter=f"task:{task_id}",
        task_id=task_id,
    )

    log.info(f"Subagent result queued in inbox: task_id={task_id} status={status} chat_id={chat_id}")
    if msg_type == "subagent_notification":
        delivery_note = "Subagent already sent reply via send_reply — dispatcher will mark processed without relaying."
    else:
        delivery_note = f"The main thread will deliver it to chat {chat_id}."
    return [TextContent(
        type="text",
        text=f"Result queued in inbox as {msg_type} (id={message_id}). {delivery_note}",
    )]


# =============================================================================
# Subagent Observation Handler
# =============================================================================

OBSERVATION_CATEGORIES = frozenset({"user_context", "system_context", "system_error"})


async def handle_write_observation(args: dict) -> list[TextContent]:
    """Write a subagent observation to the dispatcher inbox.

    Observations are separate from primary results — they surface things the
    subagent noticed in passing (user context, system state, errors). All
    categories always flow through the dispatcher inbox regardless of debug mode:

      user_context   → written to inbox; dispatcher stores and may forward to user
      system_context → written to inbox; dispatcher stores/logs
      system_error   → written to inbox; dispatcher stores/logs

    When LOBSTER_DEBUG=true, debug mode is purely additive: every observation
    that is written to the inbox also triggers an additional debug inbox message
    so the user gets real-time visibility into what the dispatcher sees.
    This mirror copy is suppressed for noop observations from the dispatcher's own
    context-injection logic (those are emitted by mark_processing, not here).

    When LOBSTER_DEBUG=false (production), only the inbox write occurs.
    """
    chat_id = args.get("chat_id")
    text = args.get("text", "").strip()
    category = args.get("category", "").strip()
    task_id = args.get("task_id", "").strip() or None
    source = args.get("source", "telegram").strip() or "telegram"

    if chat_id is None:
        return [TextContent(type="text", text="Error: chat_id is required")]
    if not text:
        return [TextContent(type="text", text="Error: text is required")]
    if category not in OBSERVATION_CATEGORIES:
        valid = ", ".join(sorted(OBSERVATION_CATEGORIES))
        return [TextContent(type="text", text=f"Error: category must be one of: {valid}")]

    _resolve_debug_config()

    now = datetime.now(timezone.utc)
    ts_ms = int(now.timestamp() * 1000)
    message_id = f"{ts_ms}_observation_{uuid.uuid4().hex[:8]}"

    message: dict = {
        "id": message_id,
        "type": "subagent_observation",
        "source": source,
        "chat_id": chat_id,
        "text": text,
        "category": category,
        "timestamp": now.isoformat(),
    }
    if task_id:
        message["task_id"] = task_id

    inbox_file = INBOX_DIR / f"{message_id}.json"
    atomic_write_json(inbox_file, message)

    # BIS-167 Slice 6: persist observation as agent event immediately
    if _db_persist_agent_event is not None:
        _db_persist_agent_event(message)

    # Belt-and-suspenders: for system_error, also append directly to observations.log
    # at the MCP layer. The inbox write above is the primary path — the dispatcher
    # picks it up and writes to the log as part of routing. This direct append is a
    # durability fallback: if the dispatcher is restarting or compacting at the moment
    # the observation arrives, the error is still recorded. The source field
    # "mcp-direct" distinguishes these entries from dispatcher-written ones.
    # Worst case: two log entries for one event (acceptable — no deduplication needed).
    if category == "system_error":
        obs_log = LOG_DIR / "observations.log"
        log_entry: dict = {
            "ts": now.isoformat(),
            "category": category,
            "content": text,
            "source": "mcp-direct",
        }
        if task_id:
            log_entry["task_id"] = task_id
        try:
            with obs_log.open("a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except OSError as exc:
            log.warning(f"Failed to write observation to {obs_log}: {exc}")

    # When LOBSTER_DEBUG=true, emit a direct debug observation for non-system_context
    # categories so the user sees the observation arrive in real time.
    # system_context is suppressed (internal bookkeeping only).
    # This is additive: the inbox write above always happens regardless of debug mode.
    emitter = f"task:{task_id}" if task_id else "unknown"
    if _DEBUG_MODE and category != "system_context":
        _emit_debug_observation(
            text,
            category=category,
            visibility="mcp-only",
            emitter=emitter,
        )

    log.info(
        f"Subagent observation queued in inbox: category={category} chat_id={chat_id}"
        + (f" task_id={task_id}" if task_id else "")
    )
    return [TextContent(
        type="text",
        text=f"Observation queued (id={message_id}, category={category}). The dispatcher will route it.",
    )]


# =============================================================================
# emit_event MCP tool handler (issue #1665)
# =============================================================================


async def handle_emit_event(args: dict) -> list[TextContent]:
    """
    MCP tool handler for emit_event (issue #1665).

    Validates the level field against VALID_SEVERITIES, constructs a LobsterEvent,
    and emits it fire-and-forget. Never raises — validation errors are returned as
    error text in the response rather than exceptions.

    Functional design: pure input validation (returns early on error), then a
    single side-effect call to the bus.
    """
    event_type = args.get("event_type", "")
    level = args.get("level", "")
    msg = args.get("msg", "")
    payload_extra: dict = args.get("payload") or {}
    task_id: str | None = args.get("task_id")
    chat_id = args.get("chat_id")

    # Validate level — reject unknown values with a descriptive error
    if level not in _VALID_SEVERITIES:
        valid = sorted(_VALID_SEVERITIES)
        return [TextContent(
            type="text",
            text=(
                f"emit_event: unknown level {level!r}. "
                f"Valid levels: {valid}. Event was not emitted."
            ),
        )]

    if not _EVENT_BUS_AVAILABLE:
        return [TextContent(type="text", text="emit_event: event bus unavailable; event not emitted.")]

    try:
        payload = {"msg": msg, **payload_extra}
        event = LobsterEvent(
            event_type=event_type,
            severity=level,
            source="mcp-tool",
            payload=payload,
            task_id=task_id,
            chat_id=chat_id,
        )
        get_event_bus().emit_sync(event)
    except Exception:
        pass  # emit_event must never crash the caller

    return [TextContent(type="text", text=f"emit_event: emitted {event_type!r} [{level}]")]


# =============================================================================
# Pending Agent Tracker Handlers
# =============================================================================


async def handle_register_agent(args: dict) -> list[TextContent]:
    """Record a newly-spawned background agent in the pending-agents tracker.

    Delegates to tracker.add_pending_agent(), which atomically writes to
    ~/messages/config/pending-agents.json under a file lock. Records survive
    dispatcher restarts and context compactions.
    """
    agent_id = args.get("agent_id", "").strip()
    description = args.get("description", "").strip()
    chat_id = args.get("chat_id")
    task_id = args.get("task_id") or None
    source = (args.get("source") or "telegram").strip() or "telegram"
    output_file = args.get("output_file") or None
    timeout_minutes = args.get("timeout_minutes") or None
    idempotency = args.get("idempotency") or None
    task_origin = args.get("task_origin") or None

    if not agent_id:
        return [TextContent(type="text", text="Error: agent_id is required")]
    if not description:
        return [TextContent(type="text", text="Error: description is required")]
    if chat_id is None:
        return [TextContent(type="text", text="Error: chat_id is required")]

    # Normalise chat_id to int when possible (tracker stores it as int)
    try:
        chat_id_int = int(chat_id)
    except (TypeError, ValueError):
        chat_id_int = chat_id  # type: ignore[assignment]

    # Normalise timeout_minutes to int when possible
    if timeout_minutes is not None:
        try:
            timeout_minutes = int(timeout_minutes)
        except (TypeError, ValueError):
            timeout_minutes = None

    try:
        _add_pending_agent(
            agent_id=agent_id,
            description=description,
            chat_id=chat_id_int,
            task_id=task_id,
            source=source,
            output_file=output_file,
            timeout_minutes=timeout_minutes,
            idempotency=idempotency,
            task_origin=task_origin,
        )
    except Exception as exc:
        log.error(f"register_agent failed: {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error recording agent: {exc}")]

    log.info(f"Registered pending agent: agent_id={agent_id!r} chat_id={chat_id_int}")
    _emit_event(
        text=f"agent.spawn: agent_id={agent_id!r} description={description!r}",
        event_type="agent.spawn",
        severity="info",
        task_id=task_id,
        chat_id=chat_id_int,
    )
    return [TextContent(
        type="text",
        text=f"Agent registered: {agent_id!r} — {description}",
    )]


async def handle_unregister_agent(args: dict) -> list[TextContent]:
    """Remove a completed or failed agent from the pending-agents tracker.

    Idempotent: removing an agent_id that does not exist is a no-op.
    """
    agent_id = args.get("agent_id", "").strip()

    if not agent_id:
        return [TextContent(type="text", text="Error: agent_id is required")]

    try:
        _remove_pending_agent(agent_id=agent_id)
    except Exception as exc:
        log.error(f"unregister_agent failed: {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error removing agent: {exc}")]

    log.info(f"Unregistered pending agent: agent_id={agent_id!r}")
    _emit_event(
        text=f"agent.complete: agent_id={agent_id!r} status=completed",
        event_type="agent.complete",
        severity="info",
    )
    return [TextContent(
        type="text",
        text=f"Agent unregistered: {agent_id!r}",
    )]


# =============================================================================
# Agent Session Store Handlers (SQLite-backed, supersede register/unregister)
# =============================================================================


async def handle_session_start(args: dict) -> list[TextContent]:
    """Record a newly-spawned background agent session in the SQLite store.

    Richer than register_agent: supports agent_type, parent_id, input_summary,
    and causality fields (trigger_message_id, trigger_snippet).
    register_agent remains a working alias that delegates to this via tracker.py.
    """
    agent_id = args.get("agent_id", "").strip()
    description = args.get("description", "").strip()
    chat_id = args.get("chat_id")
    agent_type = args.get("agent_type") or None
    task_id = args.get("task_id") or None
    source = (args.get("source") or "telegram").strip() or "telegram"
    output_file = args.get("output_file") or None
    timeout_minutes = args.get("timeout_minutes") or None
    parent_id = args.get("parent_id") or None
    input_summary = args.get("input_summary") or None
    trigger_message_id = args.get("trigger_message_id") or None
    trigger_snippet = args.get("trigger_snippet") or None
    idempotency = args.get("idempotency") or None
    task_origin = args.get("task_origin") or None
    claude_session_id = (args.get("claude_session_id") or "").strip() or None

    if not agent_id:
        return [TextContent(type="text", text="Error: agent_id is required")]
    if not description:
        return [TextContent(type="text", text="Error: description is required")]
    if chat_id is None:
        return [TextContent(type="text", text="Error: chat_id is required")]

    if timeout_minutes is not None:
        try:
            timeout_minutes = int(timeout_minutes)
        except (TypeError, ValueError):
            timeout_minutes = None

    try:
        _session_store.session_start(
            id=agent_id,
            description=description,
            chat_id=str(chat_id),
            agent_type=agent_type,
            task_id=task_id,
            source=source,
            output_file=output_file,
            timeout_minutes=timeout_minutes,
            parent_id=parent_id,
            input_summary=input_summary,
            trigger_message_id=trigger_message_id,
            trigger_snippet=trigger_snippet,
            idempotency=idempotency,
            task_origin=task_origin,
        )
    except Exception as exc:
        log.error(f"session_start failed: {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error starting session: {exc}")]

    log.info(f"Session started: agent_id={agent_id!r} agent_type={agent_type!r} chat_id={chat_id}")
    _emit_event(
        text=f"agent.spawn: agent_id={agent_id!r} agent_type={agent_type!r} description={description!r}",
        event_type="agent.spawn",
        severity="info",
        task_id=task_id,
        chat_id=chat_id if isinstance(chat_id, (int, str)) else None,
    )

    # Option B: explicit dispatcher declaration.
    # If the caller declares itself as the dispatcher, tag its MCP session ID
    # immediately.  This is more robust than Option A (tag-on-first-use via
    # wait_for_messages) because it fires during session_start, before any
    # guarded tools are called.  Both options coexist; whichever fires first wins.
    if agent_type == "dispatcher" and _http_session_manager is not None:
        http_session_id = _get_current_http_session_id()
        if http_session_id is not None:
            _tag_dispatcher_session(http_session_id)

    # Write the Claude session UUID when the dispatcher provides it.
    # SessionStart hooks receive the Claude UUID (hook_input["session_id"]),
    # which is a different ID space from the HTTP transport session ID written
    # above by _tag_dispatcher_session().  Having a dedicated file for the
    # Claude UUID allows hooks to match correctly without any ID-format conversion.
    if agent_type == "dispatcher" and claude_session_id:
        _write_dispatcher_claude_session_file(claude_session_id)

    # Notify wire server so SSE clients update within 40ms
    asyncio.create_task(_notify_wire_server())
    return [TextContent(
        type="text",
        text=f"Session started: {agent_id!r} ({agent_type or 'agent'}) — {description}",
    )]


async def handle_session_end(args: dict) -> list[TextContent]:
    """Mark an agent session as completed or failed in the SQLite store.

    Matches on agent_id or task_id. Idempotent.
    """
    agent_id = args.get("agent_id", "").strip()
    status = args.get("status", "completed").strip()
    result_summary = args.get("result_summary") or None
    stop_reason = args.get("stop_reason") or None

    if not agent_id:
        return [TextContent(type="text", text="Error: agent_id is required")]
    if status not in ("completed", "failed", "dead"):
        return [TextContent(type="text", text=f"Error: status must be 'completed', 'failed', or 'dead' (got {status!r})")]

    try:
        _session_store.session_end(
            id_or_task_id=agent_id,
            status=status,
            result_summary=result_summary,
            stop_reason=stop_reason,
        )
    except Exception as exc:
        log.error(f"session_end failed: {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error ending session: {exc}")]

    log.info(f"Session ended: agent_id={agent_id!r} status={status!r}")
    _emit_event(
        text=f"agent.complete: agent_id={agent_id!r} status={status!r}",
        event_type="agent.complete",
        severity="info" if status == "completed" else "warn",
    )
    # Notify wire server so SSE clients update within 40ms
    asyncio.create_task(_notify_wire_server())
    return [TextContent(
        type="text",
        text=f"Session ended: {agent_id!r} → {status}",
    )]


async def handle_get_active_sessions(args: dict) -> list[TextContent]:
    """Return all currently running agent sessions from the SQLite store."""
    try:
        chat_id = args.get("chat_id")
        sessions = _session_store.get_active_sessions(chat_id=chat_id)
    except Exception as exc:
        log.error(f"get_active_sessions failed: {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error querying active sessions: {exc}")]

    if not sessions:
        return [TextContent(type="text", text="No active agent sessions.")]

    import json as _json_mod
    return [TextContent(
        type="text",
        text=_json_mod.dumps(sessions, indent=2),
    )]


async def handle_get_session_history(args: dict) -> list[TextContent]:
    """Return historical agent session records from the SQLite store."""
    limit = int(args.get("limit", 20))
    status = args.get("status") or None

    try:
        history = _session_store.get_session_history(limit=limit, status=status)
    except Exception as exc:
        log.error(f"get_session_history failed: {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error querying session history: {exc}")]

    if not history:
        filter_note = f" with status={status!r}" if status else ""
        return [TextContent(type="text", text=f"No session history{filter_note}.")]

    import json as _json_mod
    return [TextContent(
        type="text",
        text=_json_mod.dumps(history, indent=2),
    )]


async def handle_record_reply(args: dict) -> list[TextContent]:
    """Append a sent reply message_id to an agent session's reply_message_ids list.

    This builds the causal chain: trigger_message → agent task → outbound replies.
    Call this immediately after send_reply when you have an active agent task.
    """
    agent_id = args.get("agent_id", "").strip()
    message_id = args.get("message_id", "").strip()

    if not agent_id:
        return [TextContent(type="text", text="Error: agent_id is required")]
    if not message_id:
        return [TextContent(type="text", text="Error: message_id is required")]

    try:
        _session_store.append_reply_message_id(agent_id=agent_id, message_id=message_id)
    except Exception as exc:
        log.error(f"record_reply failed: {exc}", exc_info=True)
        return [TextContent(type="text", text=f"Error recording reply: {exc}")]

    log.info(f"Recorded reply {message_id!r} for agent {agent_id!r}")
    return [TextContent(
        type="text",
        text=f"Reply {message_id!r} recorded for agent {agent_id!r}.",
    )]


# =============================================================================
# Brain Dump Triage Handlers
# =============================================================================

# Brain dump triage workflow labels
BRAIN_DUMP_LABELS = {
    "raw": "raw",           # New brain dump, not yet triaged
    "triaged": "triaged",   # Brain dump has been analyzed and action items identified
    "actioned": "actioned", # All action items have been created
    "closed": "closed",     # Brain dump is fully processed
}


async def run_gh_command(args: list[str]) -> tuple[bool, str, str]:
    """Run a gh CLI command. Returns (success, stdout, stderr)."""
    cmd = ["gh"] + args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode == 0,
        stdout.decode().strip() if stdout else "",
        stderr.decode().strip() if stderr else ""
    )


async def ensure_label_exists(owner: str, repo: str, label: str, color: str = "0e8a16", description: str = "") -> bool:
    """Ensure a label exists in the repository. Creates it if missing."""
    # Check if label exists
    success, _, _ = await run_gh_command([
        "label", "view", label,
        "--repo", f"{owner}/{repo}",
        "--json", "name"
    ])
    if success:
        return True

    # Create label
    cmd = ["label", "create", label, "--repo", f"{owner}/{repo}", "--color", color]
    if description:
        cmd.extend(["--description", description])
    success, _, stderr = await run_gh_command(cmd)
    return success


async def handle_triage_brain_dump(args: dict) -> list[TextContent]:
    """Mark a brain dump issue as triaged with action items listed."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    issue_number = args.get("issue_number")
    action_items = args.get("action_items", [])
    triage_notes = args.get("triage_notes", "").strip()

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not issue_number:
        return [TextContent(type="text", text="Error: issue_number is required.")]

    # Ensure labels exist
    await ensure_label_exists(owner, repo, "raw", "d4c5f9", "New brain dump, not yet processed")
    await ensure_label_exists(owner, repo, "triaged", "0e8a16", "Brain dump has been triaged")
    await ensure_label_exists(owner, repo, "actioned", "1d76db", "All action items created")
    await ensure_label_exists(owner, repo, "action-item", "fbca04", "Action item from brain dump")

    # Build triage comment
    comment_lines = ["## Triage Complete", ""]

    if action_items:
        comment_lines.append(f"**{len(action_items)} action item(s) identified:**")
        comment_lines.append("")
        for i, item in enumerate(action_items, 1):
            title = item.get("title", "Untitled")
            desc = item.get("description", "")
            comment_lines.append(f"{i}. **{title}**")
            if desc:
                comment_lines.append(f"   - {desc}")
        comment_lines.append("")
        comment_lines.append("Action items will be created as separate issues and linked back here.")
    else:
        comment_lines.append("No action items identified - this brain dump is for reference only.")

    if triage_notes:
        comment_lines.append("")
        comment_lines.append("### Notes")
        comment_lines.append(triage_notes)

    comment_lines.append("")
    comment_lines.append("---")
    comment_lines.append(f"*Triaged at {_format_display_ts(datetime.now(timezone.utc))}*")

    comment_body = "\n".join(comment_lines)

    # Add comment
    success, stdout, stderr = await run_gh_command([
        "issue", "comment", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--body", comment_body
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding triage comment: {stderr}")]

    # Remove 'raw' label if present
    await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--remove-label", "raw"
    ])

    # Add 'triaged' label
    success, _, stderr = await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--add-label", "triaged"
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding triaged label: {stderr}")]

    return [TextContent(
        type="text",
        text=f"Brain dump #{issue_number} triaged.\n- {len(action_items)} action item(s) identified\n- Label updated: raw -> triaged\n- Triage comment added"
    )]


async def handle_create_action_item(args: dict) -> list[TextContent]:
    """Create an action item issue linked to a brain dump."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    brain_dump_issue = args.get("brain_dump_issue")
    title = args.get("title", "").strip()
    body = args.get("body", "").strip()
    labels = args.get("labels", [])

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not brain_dump_issue:
        return [TextContent(type="text", text="Error: brain_dump_issue is required.")]
    if not title:
        return [TextContent(type="text", text="Error: title is required.")]

    # Ensure action-item label exists
    await ensure_label_exists(owner, repo, "action-item", "fbca04", "Action item from brain dump")

    # Build issue body
    issue_body_lines = []
    if body:
        issue_body_lines.append(body)
        issue_body_lines.append("")

    issue_body_lines.append("---")
    issue_body_lines.append(f"**Source:** Brain dump #{brain_dump_issue}")
    issue_body_lines.append("")
    issue_body_lines.append(f"*Created from brain dump triage*")

    issue_body = "\n".join(issue_body_lines)

    # Create the issue
    cmd = [
        "issue", "create",
        "--repo", f"{owner}/{repo}",
        "--title", title,
        "--body", issue_body,
        "--label", "action-item"
    ]

    # Add additional labels
    for label in labels:
        if label and label != "action-item":
            cmd.extend(["--label", label])

    success, stdout, stderr = await run_gh_command(cmd)
    if not success:
        return [TextContent(type="text", text=f"Error creating action item: {stderr}")]

    # Parse issue number from URL (gh returns URL like https://github.com/owner/repo/issues/123)
    action_issue_number = None
    if stdout:
        # Extract issue number from URL
        parts = stdout.rstrip("/").split("/")
        if parts:
            try:
                action_issue_number = int(parts[-1])
            except ValueError:
                pass

    if not action_issue_number:
        return [TextContent(
            type="text",
            text=f"Action item created but could not parse issue number.\nURL: {stdout}"
        )]

    return [TextContent(
        type="text",
        text=f"Action item created: #{action_issue_number}\n- Title: {title}\n- Linked to brain dump #{brain_dump_issue}\n- URL: {stdout}"
    )]


async def handle_link_action_to_brain_dump(args: dict) -> list[TextContent]:
    """Add a comment to brain dump linking to an action item."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    brain_dump_issue = args.get("brain_dump_issue")
    action_issue = args.get("action_issue")
    action_title = args.get("action_title", "").strip()

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not brain_dump_issue:
        return [TextContent(type="text", text="Error: brain_dump_issue is required.")]
    if not action_issue:
        return [TextContent(type="text", text="Error: action_issue is required.")]

    # Build link comment
    title_part = f": {action_title}" if action_title else ""
    comment_body = f"Action item created: #{action_issue}{title_part}"

    # Add comment
    success, _, stderr = await run_gh_command([
        "issue", "comment", str(brain_dump_issue),
        "--repo", f"{owner}/{repo}",
        "--body", comment_body
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding link comment: {stderr}")]

    return [TextContent(
        type="text",
        text=f"Linked action item #{action_issue} to brain dump #{brain_dump_issue}"
    )]


async def handle_close_brain_dump(args: dict) -> list[TextContent]:
    """Close a brain dump issue with summary."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    issue_number = args.get("issue_number")
    summary = args.get("summary", "").strip()
    action_issues = args.get("action_issues", [])

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not issue_number:
        return [TextContent(type="text", text="Error: issue_number is required.")]
    if not summary:
        return [TextContent(type="text", text="Error: summary is required.")]

    # Ensure labels exist
    await ensure_label_exists(owner, repo, "actioned", "1d76db", "All action items created")
    await ensure_label_exists(owner, repo, "closed", "000000", "Brain dump fully processed")

    # Build closure comment
    comment_lines = ["## Brain Dump Processed", ""]
    comment_lines.append(summary)
    comment_lines.append("")

    if action_issues:
        comment_lines.append("### Action Items Created")
        for issue_num in action_issues:
            comment_lines.append(f"- #{issue_num}")
        comment_lines.append("")

    comment_lines.append("---")
    comment_lines.append(f"*Closed at {_format_display_ts(datetime.now(timezone.utc))}*")

    comment_body = "\n".join(comment_lines)

    # Add closure comment
    success, _, stderr = await run_gh_command([
        "issue", "comment", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--body", comment_body
    ])
    if not success:
        return [TextContent(type="text", text=f"Error adding closure comment: {stderr}")]

    # Update labels: remove triaged, add actioned
    await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--remove-label", "triaged"
    ])

    await run_gh_command([
        "issue", "edit", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--add-label", "actioned"
    ])

    # Close the issue
    success, _, stderr = await run_gh_command([
        "issue", "close", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--reason", "completed"
    ])
    if not success:
        return [TextContent(type="text", text=f"Error closing issue: {stderr}")]

    action_count = len(action_issues) if action_issues else 0
    return [TextContent(
        type="text",
        text=f"Brain dump #{issue_number} closed.\n- {action_count} action item(s) created\n- Label: actioned\n- Status: closed (completed)"
    )]


async def handle_get_brain_dump_status(args: dict) -> list[TextContent]:
    """Get the current status of a brain dump issue."""
    owner = args.get("owner", "").strip()
    repo = args.get("repo", "").strip()
    issue_number = args.get("issue_number")

    if not owner or not repo:
        return [TextContent(type="text", text="Error: owner and repo are required.")]
    if not issue_number:
        return [TextContent(type="text", text="Error: issue_number is required.")]

    # Get issue details
    success, stdout, stderr = await run_gh_command([
        "issue", "view", str(issue_number),
        "--repo", f"{owner}/{repo}",
        "--json", "title,state,labels,comments"
    ])
    if not success:
        return [TextContent(type="text", text=f"Error fetching issue: {stderr}")]

    try:
        issue_data = json.loads(stdout)
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Error parsing issue data: {stdout}")]

    title = issue_data.get("title", "Unknown")
    state = issue_data.get("state", "unknown")
    labels = [l.get("name", "") for l in issue_data.get("labels", [])]
    comments = issue_data.get("comments", [])

    # Determine workflow status
    workflow_status = "unknown"
    if "actioned" in labels or state.lower() == "closed":
        workflow_status = "completed"
    elif "triaged" in labels:
        workflow_status = "triaged"
    elif "raw" in labels:
        workflow_status = "raw"
    else:
        workflow_status = "untagged"

    # Find linked action items from comments
    action_items = []
    for comment in comments:
        body = comment.get("body", "")
        # Look for patterns like "Action item created: #123" or "#{number}"
        matches = re.findall(r"Action item created: #(\d+)", body)
        action_items.extend([int(m) for m in matches])

    output_lines = [
        f"## Brain Dump #{issue_number}",
        "",
        f"**Title:** {title}",
        f"**State:** {state}",
        f"**Workflow Status:** {workflow_status}",
        f"**Labels:** {', '.join(labels) if labels else 'none'}",
        "",
    ]

    if action_items:
        output_lines.append(f"**Linked Action Items:** {len(action_items)}")
        for item in action_items:
            output_lines.append(f"- #{item}")
    else:
        output_lines.append("**Linked Action Items:** none")

    return [TextContent(type="text", text="\n".join(output_lines))]


# =============================================================================
# Memory System Handlers
# =============================================================================


CANONICAL_DIR = _USER_CONFIG / "memory" / "canonical"
HANDOFF_PATH = CANONICAL_DIR / "handoff.md"


async def handle_memory_store(arguments: dict[str, Any]) -> list[TextContent]:
    """Store an event in memory."""
    if _memory_provider is None:
        return [TextContent(type="text", text="Memory system is not available.")]

    content = arguments.get("content", "")
    if not content:
        return [TextContent(type="text", text="Error: content is required.")]

    metadata = {"tags": arguments.get("tags", [])}
    chat_id = arguments.get("chat_id")
    if chat_id is not None:
        metadata["source_chat_id"] = chat_id

    event = MemoryEvent(
        id=None,
        timestamp=datetime.now(timezone.utc),
        type=arguments.get("type", "note"),
        source=arguments.get("source", "internal"),
        project=arguments.get("project"),
        content=content,
        metadata=metadata,
    )

    try:
        event_id = _memory_provider.store(event)
        result_text = f"Stored memory event #{event_id} (type={event.type}, source={event.source})"
    except Exception as e:
        log.error(f"memory_store failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error storing memory: {e}")]

    # Debug alert: best-effort, isolated so a failure here never affects the store result.
    # system_context events are suppressed by TelegramOutboxListener — no manual gate needed.
    try:
        task_id_label = arguments.get("task_id", "").strip() or "dispatcher"
        content_preview = content[:80] + "…" if len(content) > 80 else content
        _emit_event(
            f"\U0001f9e0 [memory write] agent: {task_id_label}\n"
            f"type: {event.type}\n"
            f"content: {content_preview}",
            event_type="memory.write",
            severity="debug",
            source="memory-store",
            emitter=task_id_label,
            task_id=task_id_label if task_id_label != "dispatcher" else None,
        )
    except Exception:
        pass

    return [TextContent(type="text", text=result_text)]


async def handle_memory_search(arguments: dict[str, Any]) -> list[TextContent]:
    """Search memory for events matching a query."""
    if _memory_provider is None:
        return [TextContent(type="text", text="Memory system is not available.")]

    query = arguments.get("query", "")
    if not query:
        return [TextContent(type="text", text="Error: query is required.")]

    limit = arguments.get("limit", 10)
    project = arguments.get("project")

    try:
        results = _memory_provider.search(query, limit=limit, project=project)
    except Exception as e:
        log.error(f"memory_search failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error searching memory: {e}")]

    # Post-filter by chat_id if provided (attribution-based filtering)
    chat_id = arguments.get("chat_id")
    if chat_id is not None and results:
        results = [
            e for e in results
            if e.metadata.get("source_chat_id") == chat_id
            or "source_chat_id" not in e.metadata
        ]

    # Debug alert: best-effort, isolated so a failure here never affects the search result.
    # system_context events are suppressed by TelegramOutboxListener — no manual gate needed.
    try:
        task_id_label = arguments.get("task_id", "").strip() or "dispatcher"
        result_count = len(results) if results else 0
        _emit_event(
            f"\U0001f50d [memory read] agent: {task_id_label}\n"
            f"query: {query}\n"
            f"results: {result_count} found",
            event_type="memory.search",
            severity="debug",
            source="memory-search",
            emitter=task_id_label,
            task_id=task_id_label if task_id_label != "dispatcher" else None,
        )
    except Exception:
        pass

    if not results:
        return [TextContent(type="text", text=f"No memory events found for: {query}")]

    lines = [f"**Memory Search Results** ({len(results)} found for \"{query}\"):"]
    for i, event in enumerate(results, 1):
        ts = _format_display_ts(event.timestamp, "%Y-%m-%d %I:%M %p %Z") if event.timestamp else "?"
        proj = f" [{event.project}]" if event.project else ""
        eid = f"#{event.id}" if event.id else ""
        # Truncate content for display
        content_preview = event.content[:200] + "..." if len(event.content) > 200 else event.content
        lines.append(f"\n{i}. {eid} ({event.type}/{event.source}{proj}) {ts}")
        lines.append(f"   {content_preview}")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_memory_recent(arguments: dict[str, Any]) -> list[TextContent]:
    """Get recent events from memory."""
    if _memory_provider is None:
        return [TextContent(type="text", text="Memory system is not available.")]

    hours = arguments.get("hours", 24)
    project = arguments.get("project")

    try:
        results = _memory_provider.recent(hours=hours, project=project)

        # Post-filter by chat_id if provided (attribution-based filtering)
        chat_id = arguments.get("chat_id")
        if chat_id is not None and results:
            results = [
                e for e in results
                if e.metadata.get("source_chat_id") == chat_id
                or "source_chat_id" not in e.metadata
            ]

        if not results:
            return [TextContent(type="text", text=f"No events in the last {hours} hours.")]

        lines = [f"**Recent Events** ({len(results)} in last {hours}h):"]
        for event in results:
            ts = _format_display_ts(event.timestamp, "%Y-%m-%d %I:%M %p %Z") if event.timestamp else "?"
            proj = f" [{event.project}]" if event.project else ""
            eid = f"#{event.id}" if event.id else ""
            consolidated = " [consolidated]" if event.consolidated else ""
            content_preview = event.content[:150] + "..." if len(event.content) > 150 else event.content
            lines.append(f"- {eid} {ts} ({event.type}/{event.source}{proj}){consolidated}: {content_preview}")

        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        log.error(f"memory_recent failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error getting recent events: {e}")]


async def handle_get_handoff(arguments: dict[str, Any]) -> list[TextContent]:
    """Read and return the current handoff document."""
    try:
        if HANDOFF_PATH.exists():
            content = HANDOFF_PATH.read_text()
            return [TextContent(type="text", text=content)]
        else:
            return [TextContent(type="text", text="Handoff document not found at " + str(HANDOFF_PATH))]
    except Exception as e:
        log.error(f"get_handoff failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error reading handoff: {e}")]


async def handle_mark_consolidated(arguments: dict[str, Any]) -> list[TextContent]:
    """Mark memory events as consolidated."""
    if _memory_provider is None:
        return [TextContent(type="text", text="Memory system is not available.")]

    event_ids = arguments.get("event_ids", [])
    if not event_ids:
        return [TextContent(type="text", text="Error: event_ids is required and must be non-empty.")]

    try:
        _memory_provider.mark_consolidated(event_ids)
        return [TextContent(
            type="text",
            text=f"Marked {len(event_ids)} event(s) as consolidated: {event_ids}"
        )]
    except Exception as e:
        log.error(f"mark_consolidated failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error marking consolidated: {e}")]


async def handle_check_updates(arguments: dict[str, Any]) -> list[TextContent]:
    """Check if Lobster updates are available."""
    try:
        result = _update_manager.check_for_updates()
        if not result["updates_available"]:
            return [TextContent(type="text", text=f"Lobster is up to date (SHA: {result['local_sha'][:7]}).")]

        lines = [
            f"**Updates available!** ({result['commits_behind']} commits behind)",
            f"Local: `{result['local_sha'][:7]}` | Remote: `{result['remote_sha'][:7]}`",
            "",
            "**Recent commits:**",
        ]
        for commit in result["commit_log"][:10]:
            lines.append(f"- {commit}")
        if len(result["commit_log"]) > 10:
            lines.append(f"  ... and {len(result['commit_log']) - 10} more")

        lines.append("")
        lines.append("Use `get_upgrade_plan` for full changelog and compatibility analysis.")
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        log.error(f"check_updates failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error checking for updates: {e}")]


async def handle_get_upgrade_plan(arguments: dict[str, Any]) -> list[TextContent]:
    """Generate a full upgrade plan with changelog and compatibility analysis."""
    try:
        plan = _update_manager.create_upgrade_plan()
        if plan["action"] == "none":
            return [TextContent(type="text", text=plan["message"])]

        lines = [
            f"**Upgrade Plan** ({plan['commits_behind']} commits behind)",
            "",
            plan["changelog"],
            "---",
            f"**Recommendation:** {plan['compatibility']['recommendation']}",
            f"**Safe to auto-update:** {'Yes' if plan['compatibility']['safe_to_update'] else 'No'}",
        ]

        if plan["compatibility"]["issues"]:
            lines.append("")
            lines.append("**Issues:**")
            for issue in plan["compatibility"]["issues"]:
                lines.append(f"- {issue}")

        if plan["compatibility"]["warnings"]:
            lines.append("")
            lines.append("**Warnings:**")
            for warning in plan["compatibility"]["warnings"]:
                lines.append(f"- {warning}")

        lines.append("")
        lines.append("**Steps:**")
        for step in plan["steps"]:
            lines.append(f"  {step}")

        if plan["action"] == "auto":
            lines.append("")
            lines.append("Use `execute_update` with `confirm: true` to apply this update.")

        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        log.error(f"get_upgrade_plan failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error generating upgrade plan: {e}")]


async def handle_execute_update(arguments: dict[str, Any]) -> list[TextContent]:
    """Execute a safe auto-update."""
    confirm = arguments.get("confirm", False)
    if not confirm:
        return [TextContent(type="text", text="Error: You must pass `confirm: true` to execute an update.")]

    try:
        result = _update_manager.execute_safe_update()
        if result["success"]:
            lines = [
                f"Update successful! {result['message']}",
                "",
                f"**Rollback:** `{result['rollback_command']}`",
                "",
                "Note: You may need to restart the MCP server for changes to take effect.",
            ]
            return [TextContent(type="text", text="\n".join(lines))]
        else:
            return [TextContent(type="text", text=f"Update failed: {result['message']}")]
    except Exception as e:
        log.error(f"execute_update failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error executing update: {e}")]


# =============================================================================
# Convenience Tools — Canonical Memory Readers
# =============================================================================


def _read_canonical_file(relative_path: str, missing_message: str) -> str:
    """Pure helper: read a file under CANONICAL_DIR or return a fallback message."""
    path = CANONICAL_DIR / relative_path
    if path.exists():
        return path.read_text()
    return missing_message


def _list_project_names() -> list[dict]:
    """Pure helper: list project markdown files under CANONICAL_DIR/projects/."""
    projects_dir = CANONICAL_DIR / "projects"
    if not projects_dir.exists():
        return []
    return [
        {"name": f.stem, "path": str(f)}
        for f in sorted(projects_dir.glob("*.md"))
    ]


def _list_person_names() -> list[dict]:
    """Pure helper: list person markdown files under CANONICAL_DIR/people/."""
    people_dir = CANONICAL_DIR / "people"
    if not people_dir.exists():
        return []
    return [
        {"name": f.stem, "path": str(f)}
        for f in sorted(people_dir.glob("*.md"))
    ]


async def handle_get_priorities(arguments: dict[str, Any]) -> list[TextContent]:
    """Return the canonical priorities.md content."""
    try:
        content = _read_canonical_file(
            "priorities.md",
            "No priorities file found. Nightly consolidation has not run yet.",
        )
        return [TextContent(type="text", text=content)]
    except Exception as e:
        log.error(f"get_priorities failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error reading priorities: {e}")]


async def handle_get_project_context(arguments: dict[str, Any]) -> list[TextContent]:
    """Return a specific project's canonical markdown content."""
    project = arguments.get("project", "")
    if not project:
        return [TextContent(type="text", text="Error: project name is required.")]

    # Sanitize: reject path traversal attempts
    if "/" in project or "\\" in project or ".." in project:
        return [TextContent(type="text", text="Error: invalid project name.")]

    try:
        path = CANONICAL_DIR / "projects" / f"{project}.md"
        if path.exists():
            return [TextContent(type="text", text=path.read_text())]
        available = [f.stem for f in (CANONICAL_DIR / "projects").glob("*.md")] if (CANONICAL_DIR / "projects").exists() else []
        return [TextContent(
            type="text",
            text=f"No project file for '{project}'. Available: {', '.join(available) or 'none'}",
        )]
    except Exception as e:
        log.error(f"get_project_context failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error reading project context: {e}")]


async def handle_get_daily_digest(arguments: dict[str, Any]) -> list[TextContent]:
    """Return the canonical daily-digest.md content."""
    try:
        content = _read_canonical_file(
            "daily-digest.md",
            "No daily digest found. Nightly consolidation has not run yet.",
        )
        return [TextContent(type="text", text=content)]
    except Exception as e:
        log.error(f"get_daily_digest failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error reading daily digest: {e}")]


async def handle_list_projects(arguments: dict[str, Any]) -> list[TextContent]:
    """List all project files in canonical memory."""
    try:
        projects = _list_project_names()
        if not projects:
            return [TextContent(type="text", text="No project files found in canonical memory.")]
        return [TextContent(type="text", text=json.dumps(projects, indent=2))]
    except Exception as e:
        log.error(f"list_projects failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error listing projects: {e}")]


async def handle_get_person_context(arguments: dict[str, Any]) -> list[TextContent]:
    """Return a specific person's canonical markdown content."""
    person = arguments.get("person", "")
    if not person:
        return [TextContent(type="text", text="Error: person name is required.")]

    # Sanitize: reject path traversal attempts
    if "/" in person or "\\" in person or ".." in person:
        return [TextContent(type="text", text="Error: invalid person name.")]

    try:
        path = CANONICAL_DIR / "people" / f"{person}.md"
        if path.exists():
            return [TextContent(type="text", text=path.read_text())]
        available = [f.stem for f in (CANONICAL_DIR / "people").glob("*.md")] if (CANONICAL_DIR / "people").exists() else []
        return [TextContent(
            type="text",
            text=f"No person file for '{person}'. Available: {', '.join(available) or 'none'}",
        )]
    except Exception as e:
        log.error(f"get_person_context failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error reading person context: {e}")]


async def handle_list_people(arguments: dict[str, Any]) -> list[TextContent]:
    """List all person files in canonical memory."""
    try:
        people = _list_person_names()
        if not people:
            return [TextContent(type="text", text="No person files found in canonical memory.")]
        return [TextContent(type="text", text=json.dumps(people, indent=2))]
    except Exception as e:
        log.error(f"list_people failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error listing people: {e}")]


# =============================================================================
# Local Sync Awareness -- lobster-sync Branch Monitoring
# =============================================================================

# Path to the sync repos config (lives in the config directory)
SYNC_REPOS_CONFIG = _CONFIG_DIR / "sync-repos.json"


def load_sync_repos(repo_filter: str | None = None) -> list[dict]:
    """Load the sync repos config, optionally filtering to one repo.

    Returns a list of dicts with keys: owner, name.
    If repo_filter is provided (e.g. 'SiderealPress/Lobster'), only that
    repo is returned (if it exists in the config and is enabled).
    """
    config_path = SYNC_REPOS_CONFIG
    if not config_path.exists():
        return []

    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    repos = [
        {"owner": r["owner"], "name": r["name"]}
        for r in data.get("repos", [])
        if r.get("enabled", True)
    ]

    if repo_filter:
        parts = repo_filter.split("/", 1)
        if len(parts) == 2:
            owner, name = parts
            repos = [
                r for r in repos
                if r["owner"].lower() == owner.lower()
                and r["name"].lower() == name.lower()
            ]
        else:
            repos = [
                r for r in repos
                if r["name"].lower() == repo_filter.lower()
            ]

    return repos


def parse_branch_info(api_response: dict, owner: str, name: str) -> dict:
    """Pure function: extract sync status from a GitHub branch API response."""
    commit = api_response.get("commit", {})
    commit_detail = commit.get("commit", {})
    committer = commit_detail.get("committer", {})
    author = commit_detail.get("author", {})
    return {
        "repo": f"{owner}/{name}",
        "last_sync": committer.get("date", "unknown"),
        "message": commit_detail.get("message", ""),
        "sha": commit.get("sha", "")[:8],
        "author": author.get("name", "unknown"),
    }


def parse_compare_info(api_response: dict) -> dict:
    """Pure function: extract divergence summary from a GitHub compare API response."""
    return {
        "ahead_by": api_response.get("ahead_by", 0),
        "behind_by": api_response.get("behind_by", 0),
        "total_commits": api_response.get("total_commits", 0),
        "changed_files": len(api_response.get("files", [])),
    }


def format_sync_status(results: list[dict]) -> str:
    """Pure function: format sync check results into a readable report."""
    if not results:
        return "No registered repos found. Configure repos in config/sync-repos.json."

    lines = ["**Local Sync Status**", ""]

    for r in results:
        if r.get("error"):
            lines.append(f"**{r['repo']}** -- {r['error']}")
            lines.append("")
            continue

        lines.append(f"**{r['repo']}**")
        lines.append(f"- Last sync: {r.get('last_sync', 'unknown')}")
        lines.append(f"- Commit: `{r.get('sha', '?')}` {r.get('message', '')}")
        lines.append(f"- Author: {r.get('author', 'unknown')}")

        div = r.get("divergence")
        if div:
            lines.append(
                f"- Divergence from main: {div['ahead_by']} commits ahead, "
                f"{div['behind_by']} behind, {div['changed_files']} files changed"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


async def fetch_sync_branch(
    owner: str, name: str, sync_branch: str = "lobster-sync",
) -> dict:
    """Fetch lobster-sync branch info from GitHub API using gh CLI.

    Returns a result dict suitable for format_sync_status. Side effect boundary.
    """
    result: dict = {"repo": f"{owner}/{name}"}

    success, stdout, stderr = await run_gh_command([
        "api", f"/repos/{owner}/{name}/branches/{sync_branch}",
        "--jq", ".",
    ])

    if not success:
        if "404" in stderr or "Not Found" in stderr:
            result["error"] = f"No `{sync_branch}` branch found"
        else:
            result["error"] = f"API error: {stderr[:200]}"
        return result

    try:
        branch_data = json.loads(stdout)
    except json.JSONDecodeError:
        result["error"] = "Failed to parse branch API response"
        return result

    parsed = parse_branch_info(branch_data, owner, name)
    result.update(parsed)

    cmp_success, cmp_stdout, _ = await run_gh_command([
        "api", f"/repos/{owner}/{name}/compare/main...{sync_branch}",
        "--jq", "{ahead_by, behind_by, total_commits, files: [.files[].filename]}",
    ])

    if cmp_success:
        try:
            cmp_data = json.loads(cmp_stdout)
            result["divergence"] = parse_compare_info(cmp_data)
        except json.JSONDecodeError:
            pass

    return result


async def handle_check_local_sync(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle the check_local_sync tool call."""
    repo_filter = arguments.get("repo")

    try:
        repos = load_sync_repos(repo_filter)
        if not repos:
            if repo_filter:
                msg = (
                    f"Repo '{repo_filter}' not found in sync config. "
                    "Check config/sync-repos.json."
                )
            else:
                msg = (
                    "No repos configured for sync monitoring. "
                    "Add repos to config/sync-repos.json."
                )
            return [TextContent(type="text", text=msg)]

        sync_branch = "lobster-sync"
        if SYNC_REPOS_CONFIG.exists():
            try:
                cfg = json.loads(SYNC_REPOS_CONFIG.read_text())
                sync_branch = cfg.get("sync_branch", "lobster-sync")
            except (json.JSONDecodeError, OSError):
                pass

        results = await asyncio.gather(*(
            fetch_sync_branch(r["owner"], r["name"], sync_branch)
            for r in repos
        ))

        report = format_sync_status(list(results))
        return [TextContent(type="text", text=report)]
    except Exception as e:
        log.error(f"check_local_sync failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error checking local sync: {e}")]


async def handle_get_bisque_connection_url(arguments: dict[str, Any]) -> list[TextContent]:
    """Return the WebSocket connection URL for bisque-computer.

    Reads the dashboard token from ~/messages/config/dashboard-token and the
    public IP from ~/lobster-config/config.env (LOBSTER_PUBLIC_IP). Falls back
    to ``curl -s ifconfig.me`` when the config entry is absent.
    """
    # Read token
    token_file = _MESSAGES / "config" / "dashboard-token"
    if not token_file.exists():
        return [TextContent(type="text", text=(
            "Dashboard token not found. Start the dashboard server first:\n"
            "nohup /home/admin/lobster/.venv/bin/python3 "
            "/home/admin/lobster/src/dashboard/server.py --host 0.0.0.0 --port 9100 &"
        ))]
    token = token_file.read_text().strip()
    if not token:
        return [TextContent(type="text", text="Dashboard token file is empty. Restart the dashboard server to regenerate it.")]

    # Read public IP from config, with ifconfig.me fallback
    public_ip: str = ""
    config_file = _CONFIG_DIR / "config.env"
    if config_file.exists():
        for line in config_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("LOBSTER_PUBLIC_IP="):
                public_ip = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                break

    if not public_ip:
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "--max-time", "5", "ifconfig.me",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            public_ip = stdout.decode().strip()
        except Exception:
            pass

    if not public_ip:
        return [TextContent(type="text", text="Could not determine public IP. Add LOBSTER_PUBLIC_IP=<IP> to ~/lobster-config/config.env.")]

    url = f"ws://{public_ip}:9100?token={token}"
    return [TextContent(type="text", text=url)]


async def handle_generate_bisque_login_token(arguments: dict[str, Any]) -> list[TextContent]:
    """Generate a bisque-chat login token for the given email.

    Calls the relay server's POST /auth/admin/token endpoint (running locally on
    port 9101 by default, or the HTTP URL derived from BISQUE_RELAY_URL).

    The returned loginToken is a base64url-encoded JSON:
        { url: <relay_ws_url>, token: <bootstrap> }
    Users paste this into the bisque-chat login screen.
    """
    import urllib.request
    import urllib.error

    email = arguments.get("email", "").strip()
    if not email or "@" not in email:
        return [TextContent(type="text", text="Error: a valid email address is required.")]

    # Read config from file
    config_file = _CONFIG_DIR / "config.env"
    admin_secret = ""
    relay_url = ""  # WebSocket URL (wss://...)
    relay_http_url = ""  # HTTP base URL for the relay API

    if config_file.exists():
        for line in config_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("BISQUE_ADMIN_SECRET="):
                admin_secret = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            elif stripped.startswith("ADMIN_SECRET=") and not admin_secret:
                admin_secret = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            elif stripped.startswith("BISQUE_RELAY_URL="):
                relay_url = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            elif stripped.startswith("BISQUE_RELAY_HTTP_URL="):
                relay_http_url = stripped.split("=", 1)[1].strip().strip('"').strip("'")

    # Fall back to environment variables
    if not admin_secret:
        admin_secret = os.environ.get("BISQUE_ADMIN_SECRET", "") or os.environ.get("ADMIN_SECRET", "")
    if not relay_url:
        relay_url = os.environ.get("BISQUE_RELAY_URL", "")
    if not relay_http_url:
        relay_http_url = os.environ.get("BISQUE_RELAY_HTTP_URL", "")

    if not admin_secret:
        return [TextContent(type="text", text=(
            "BISQUE_ADMIN_SECRET is not configured. Add it to ~/lobster-config/config.env:\n"
            "  BISQUE_ADMIN_SECRET=<your-secret>\n\n"
            "This secret must match the BISQUE_ADMIN_SECRET used by the relay server."
        ))]

    # Derive the HTTP base URL for the relay.
    # If not explicitly set, default to localhost:9101 (where the relay runs).
    if not relay_http_url:
        relay_http_url = "http://localhost:9101"

    endpoint = f"{relay_http_url.rstrip('/')}/auth/admin/token"

    payload: dict[str, str] = {"email": email}
    # Pass relayUrl override only if explicitly provided by the caller
    relay_url_override = arguments.get("relayUrl", "").strip()
    if relay_url_override:
        payload["relayUrl"] = relay_url_override

    try:
        req_body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {admin_secret}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            err_msg = err_body.get("error", str(exc))
        except Exception:
            err_msg = str(exc)
        return [TextContent(type="text", text=f"Failed to generate token via relay: {err_msg}")]
    except Exception as exc:
        return [TextContent(type="text", text=(
            f"Could not reach bisque relay at {relay_http_url}: {exc}\n\n"
            "Make sure the relay is running and BISQUE_ADMIN_SECRET is set in ~/lobster-config/config.env."
        ))]

    login_token = resp_body.get("loginToken", "")
    returned_email = resp_body.get("email", email)

    instructions = (
        f"Login token for {returned_email}:\n\n"
        f"{login_token}\n\n"
        "Paste this token into the bisque-chat login screen."
    )
    return [TextContent(type="text", text=instructions)]


# =============================================================================
# Skill Management Handlers
# =============================================================================

async def handle_get_skill_context(args: dict) -> list[TextContent]:
    """Return assembled context from all active skills."""
    try:
        context = _get_skill_context()
        if not context:
            return [TextContent(type="text", text="No active skills.")]
        return [TextContent(type="text", text=context)]
    except Exception as e:
        log.error(f"get_skill_context failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error: {e}")]


async def handle_list_skills(args: dict) -> list[TextContent]:
    """List available skills with install/active status."""
    try:
        status_filter = args.get("status", "all").lower()
        skills = _list_available_skills()

        if status_filter == "installed":
            skills = [s for s in skills if s["installed"]]
        elif status_filter == "active":
            skills = [s for s in skills if s["active"]]
        elif status_filter == "available":
            skills = [s for s in skills if not s["installed"]]

        if not skills:
            return [TextContent(type="text", text=f"No skills found (filter: {status_filter}).")]

        lines = [f"**Lobster Skills** ({len(skills)} found)\n"]
        for s in skills:
            status_parts = []
            if s["active"]:
                status_parts.append("active")
            elif s["installed"]:
                status_parts.append("installed")
            else:
                status_parts.append("available")
            status_str = ", ".join(status_parts)
            lines.append(f"- **{s['name']}** v{s['version']} [{status_str}] — {s['description']}")

        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        log.error(f"list_skills failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error: {e}")]


async def handle_activate_skill(args: dict) -> list[TextContent]:
    """Activate a skill."""
    skill_name = args.get("skill_name", "").strip()
    if not skill_name:
        return [TextContent(type="text", text="Error: skill_name is required.")]
    mode = args.get("mode", "always")
    result = _activate_skill(skill_name, mode=mode)
    return [TextContent(type="text", text=result)]


async def handle_deactivate_skill(args: dict) -> list[TextContent]:
    """Deactivate a skill."""
    skill_name = args.get("skill_name", "").strip()
    if not skill_name:
        return [TextContent(type="text", text="Error: skill_name is required.")]
    result = _deactivate_skill(skill_name)
    return [TextContent(type="text", text=result)]


async def handle_get_skill_preferences(args: dict) -> list[TextContent]:
    """Get merged preferences for a skill."""
    skill_name = args.get("skill_name", "").strip()
    if not skill_name:
        return [TextContent(type="text", text="Error: skill_name is required.")]
    try:
        prefs = _get_skill_preferences(skill_name)
        if not prefs:
            return [TextContent(type="text", text=f"No preferences for '{skill_name}'.")]
        lines = [f"**Preferences for {skill_name}:**\n"]
        for k, v in sorted(prefs.items()):
            lines.append(f"- `{k}`: {v}")
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        log.error(f"get_skill_preferences failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Error: {e}")]


async def handle_set_skill_preference(args: dict) -> list[TextContent]:
    """Set a preference value for a skill."""
    skill_name = args.get("skill_name", "").strip()
    key = args.get("key", "").strip()
    value = args.get("value")
    if not skill_name or not key:
        return [TextContent(type="text", text="Error: skill_name and key are required.")]
    if value is None:
        return [TextContent(type="text", text="Error: value is required.")]
    result = _set_skill_preference(skill_name, key, value)
    return [TextContent(type="text", text=result)]


async def handle_create_calendar_event(args: dict) -> list[TextContent]:
    """Create an event on a user's primary Google Calendar.

    Resolves the user's token via the configured backend (myownlobster or
    local), then calls the Google Calendar API to create the event.

    Required args:
        telegram_chat_id  — int or str Telegram chat_id
        title             — event summary
        start_datetime    — ISO 8601 datetime string
        end_datetime      — ISO 8601 datetime string

    Optional args:
        timezone    — IANA timezone name (default: America/Los_Angeles)
        location    — event location string
        description — event description / notes
    """
    import zoneinfo
    from datetime import datetime, timezone as dt_timezone
    # Ensure src/ is on sys.path (needed when running as a script without src/ in path)
    _src = str(Path(__file__).resolve().parent.parent)
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from integrations.google_calendar.client import create_event, CalendarAPIError

    chat_id = str(args.get("telegram_chat_id", "")).strip()
    title = args.get("title", "").strip()
    start_str = args.get("start_datetime", "").strip()
    end_str = args.get("end_datetime", "").strip()
    tz_name = args.get("timezone", "America/Los_Angeles").strip() or "America/Los_Angeles"
    location = args.get("location", "")
    description = args.get("description", "")

    if not chat_id:
        return [TextContent(type="text", text="Error: telegram_chat_id is required.")]
    if not title:
        return [TextContent(type="text", text="Error: title is required.")]
    if not start_str or not end_str:
        return [TextContent(type="text", text="Error: start_datetime and end_datetime are required.")]

    # Parse datetimes — apply the requested timezone if they are naive
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        return [TextContent(type="text", text=f"Error: unknown timezone '{tz_name}'.")]

    def _parse_dt(s: str, tz) -> datetime:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt

    try:
        start_dt = _parse_dt(start_str, tz)
        end_dt = _parse_dt(end_str, tz)
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error parsing datetime: {exc}")]

    event = create_event(
        user_id=chat_id,
        title=title,
        start=start_dt,
        end=end_dt,
        description=description,
        location=location,
    )

    if event is None:
        return [TextContent(type="text", text=(
            f"Failed to create calendar event for telegram_chat_id={chat_id}. "
            "The user may not have a valid Google Calendar token — "
            "they need to connect their Google account via myownlobster.ai."
        ))]

    result = {
        "id": event.id,
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "location": event.location,
        "url": event.url,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_list_calendar_events(args: dict) -> list[TextContent]:
    """List events from a user's primary Google Calendar.

    Required args:
        telegram_chat_id  — int or str Telegram chat_id

    Optional args:
        time_min    — ISO 8601 start of range (default: now)
        time_max    — ISO 8601 end of range (default: 7 days from now)
        max_results — max events to return (default: 10)
    """
    from datetime import datetime, timedelta, timezone as dt_timezone
    # Ensure src/ is on sys.path (needed when running as a script without src/ in path)
    _src = str(Path(__file__).resolve().parent.parent)
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from integrations.google_calendar.token_store import get_valid_token
    from integrations.google_calendar.client import _call_calendar_api, _parse_event, CalendarAPIError

    chat_id = str(args.get("telegram_chat_id", "")).strip()
    max_results = int(args.get("max_results", 10))

    if not chat_id:
        return [TextContent(type="text", text="Error: telegram_chat_id is required.")]

    now = datetime.now(tz=dt_timezone.utc)
    default_max = now + timedelta(days=7)

    def _parse_opt_dt(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt
        except ValueError:
            return None

    time_min = _parse_opt_dt(args.get("time_min")) or now
    time_max = _parse_opt_dt(args.get("time_max")) or default_max

    token = get_valid_token(chat_id)
    if token is None:
        return [TextContent(type="text", text=(
            f"No valid Google Calendar token for telegram_chat_id={chat_id}. "
            "The user needs to connect their Google account via myownlobster.ai."
        ))]

    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    params = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max_results,
    }

    try:
        data = _call_calendar_api("GET", url, token.access_token, params=params)
    except (CalendarAPIError, Exception) as exc:
        return [TextContent(type="text", text=f"Google Calendar API error: {type(exc).__name__}: {exc}")]

    items = data.get("items", [])
    events = [_parse_event(item) for item in items]

    result = [
        {
            "id": e.id,
            "title": e.title,
            "start": e.start.isoformat(),
            "end": e.end.isoformat(),
            "location": e.location,
            "description": e.description,
            "url": e.url,
        }
        for e in events
    ]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ---------------------------------------------------------------------------
# /report Slash Command Handler
# ---------------------------------------------------------------------------

async def handle_create_report(args: dict) -> list[TextContent]:
    """Handle the create_report MCP tool.

    Captures a point-in-time snapshot (recent conversation messages, active
    agent sessions) alongside the user's description and stores it in the
    reports table of agent_sessions.db. Returns a confirmation dict with
    the generated report_id.

    This is the backend for the /report slash command pre-processor.
    """
    description = str(args.get("description", "")).strip()
    chat_id = args.get("chat_id", "")
    source = str(args.get("source", "telegram")).strip() or "telegram"

    if not description:
        return [TextContent(type="text", text='{"error": "description is required"}')]
    if not chat_id:
        return [TextContent(type="text", text='{"error": "chat_id is required"}')]

    # Capture ambient state: last 10 messages for this chat from conversation history
    recent_messages: list = []
    try:
        history_result = await handle_get_conversation_history({
            "chat_id": chat_id,
            "limit": 10,
            "direction": "all",
        })
        # The handler returns JSON text — parse it back to extract raw message list
        if history_result:
            raw = history_result[0].text
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    recent_messages = parsed
                elif isinstance(parsed, dict) and "messages" in parsed:
                    recent_messages = parsed["messages"]
            except (json.JSONDecodeError, ValueError):
                pass  # Non-JSON response format — skip snapshot
    except Exception:
        pass  # Conversation history is best-effort; never block report creation

    # Capture active agent session IDs
    active_session_ids: list[str] = []
    try:
        active = _session_store.get_active_sessions()
        active_session_ids = [s.get("id", "") for s in active if s.get("id")]
    except Exception:
        pass  # Session capture is best-effort

    # Build a minimal ambient snapshot
    snapshot_state = {
        "active_session_count": len(active_session_ids),
        "lobster_state": _read_lobster_state(),
    }

    # Store the report
    try:
        report = _session_store.create_report(
            description=description,
            chat_id=chat_id,
            source=source,
            recent_messages=recent_messages if recent_messages else None,
            active_session_ids=active_session_ids if active_session_ids else None,
            snapshot_state=snapshot_state,
            instance_id=_INSTANCE_ID,
        )
    except Exception as exc:
        log.error(f"create_report failed: {exc}", exc_info=True)
        raise ValueError(f"Failed to create report: {exc}") from exc

    log.info(f"Report filed: {report['report_id']} from chat {chat_id}")
    return [TextContent(type="text", text=json.dumps(report))]


async def handle_list_reports(args: dict) -> list[TextContent]:
    """Handle the list_reports MCP tool.

    Returns a JSON array of report records, newest first, optionally filtered
    by chat_id and/or status.
    """
    chat_id = args.get("chat_id")
    status = str(args.get("status", "open")).strip() or "open"
    limit_raw = args.get("limit", 20)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 20

    try:
        reports = _session_store.list_reports(
            chat_id=chat_id,
            status=status,
            limit=limit,
        )
    except Exception as exc:
        log.error(f"list_reports failed: {exc}", exc_info=True)
        raise ValueError(f"Failed to list reports: {exc}") from exc

    return [TextContent(type="text", text=json.dumps(reports))]


def _read_last_output(output_file: str | None, max_chars: int = 500) -> str | None:
    """Return the last `max_chars` characters of an agent output file, or None.

    Pure function except for filesystem read. Used to enrich agent_failed
    notifications with enough context for the dispatcher to decide whether
    to re-queue, escalate, or drop silently.
    """
    if not output_file:
        return None
    try:
        path = Path(output_file).resolve()
        if not path.exists():
            return None
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_chars:
                f.seek(-max_chars, 2)
            raw = f.read(max_chars)
        return raw.decode("utf-8", errors="replace").strip() or None
    except OSError:
        return None


def _build_reconciler_message(
    session: dict,
    outcome: str,
    now: datetime,
) -> dict:
    """Return the inbox message payload for a reconciler notification (pure).

    For 'completed' outcomes: routes to the originating chat_id so the
    dispatcher can relay the result to the user.

    For 'dead' outcomes: routes to chat_id=0 with type='agent_failed' so the
    dispatcher treats it as an internal system event — never forwarded to the
    user directly. The dispatcher decides whether to re-queue, escalate, or drop.

    Args:
        session: Session dict from get_active_sessions() or get_unnotified_completed().
        outcome: 'completed' or 'dead'.
        now:     Current UTC datetime (injected for testability).
    """
    agent_id = session.get("id", "")
    description = session.get("description", "unknown task")
    task_id = session.get("task_id") or agent_id
    input_summary = session.get("input_summary")
    output_file = session.get("output_file")
    session_task_origin = session.get("task_origin") or "user"

    elapsed_raw = session.get("elapsed_seconds")
    try:
        elapsed = int(elapsed_raw) if elapsed_raw is not None else 0
    except (TypeError, ValueError):
        elapsed = 0
    elapsed_min = elapsed // 60

    ts_ms = int(now.timestamp() * 1000)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent_id)[:40]
    message_id = f"{ts_ms}_reconciler_{safe_id}"

    if outcome == "completed":
        # Route completed notifications to the originating chat so the user sees the result.
        return {
            "id": message_id,
            "type": "subagent_result",
            "source": session.get("source", "telegram"),
            "chat_id": session.get("chat_id", ""),
            "task_origin": session_task_origin,
            "text": (
                f"Agent completed: {description}\n"
                f"(reconciler-detected via stop_reason=end_turn, {elapsed_min}m elapsed)"
            ),
            "task_id": task_id,
            "agent_id": agent_id,
            "status": "success",
            "sent_reply_to_user": False,
            "timestamp": now.isoformat(),
        }
    else:
        # Route failure notifications to chat_id=0 (dispatcher-internal).
        # The dispatcher sees type='agent_failed' and decides whether to re-queue,
        # escalate to the user, or drop silently. Never relay raw failure noise to
        # the user's Telegram.
        last_output = _read_last_output(output_file)
        original_chat_id = session.get("chat_id", "")
        return {
            "id": message_id,
            "type": "agent_failed",
            "source": "system",
            "chat_id": 0,
            "task_origin": "internal",
            "text": (
                f"Agent failed/disappeared: {description}\n"
                f"(no output file after {elapsed_min}m — marked dead)"
            ),
            "task_id": task_id,
            "agent_id": agent_id,
            "original_chat_id": original_chat_id,
            "original_prompt": input_summary,
            "last_output": last_output,
            "status": "error",
            "sent_reply_to_user": False,
            "timestamp": now.isoformat(),
        }


def _enqueue_reconciler_notification(session: dict, outcome: str) -> None:
    """Write a structured inbox message for a reconciler-detected session transition.

    For completed agents: routes to the originating chat so the dispatcher can
    relay the result to the user.

    For dead/failed agents: routes to chat_id=0 with type='agent_failed' so the
    dispatcher handles it internally. Raw failure noise is never forwarded to
    the user's Telegram — the dispatcher decides whether to re-queue, escalate,
    or drop silently.

    Also marks the session as notified_at to prevent duplicate notifications
    on the next reconciler cycle.

    Args:
        session: Session dict from get_active_sessions() or get_unnotified_completed().
        outcome: 'completed' or 'dead'.
    """
    # Idempotency guard — if already notified, skip
    if session.get("notified_at"):
        return

    # Dead sessions with no real user don't need dispatcher action — route to
    # debug log only, not the inbox. The dispatcher cannot notify a user
    # (no chat_id) or take any meaningful action. Logging preserves observability.
    if outcome == "dead":
        _chat_id = session.get("chat_id")
        _chat_id_str = str(_chat_id).strip() if _chat_id is not None else ""
        if _chat_id_str in ("0", "", "None"):
            _agent_id = session.get("id", "")
            log.debug(
                "[reconciler] Dead session %r has no real user (chat_id=%r) — "
                "skipping inbox notification (logged to debug only)",
                _agent_id, _chat_id,
            )
            # Mark as notified so this session is not re-enqueued on every
            # restart. The early return skips inbox delivery (intentional —
            # there is no user to notify), but bookkeeping must still happen.
            try:
                _session_store.set_notified(_agent_id)
            except Exception as _exc:
                log.error(
                    "[reconciler] Failed to set_notified for no-user dead session %r: %s",
                    _agent_id, _exc,
                )
            return

    agent_id = session.get("id", "")
    now = datetime.now(timezone.utc)
    message = _build_reconciler_message(session, outcome, now)

    try:
        inbox_file = INBOX_DIR / f"{message['id']}.json"
        atomic_write_json(inbox_file, message)
        # Mark notified so duplicate notification is not sent on next cycle
        _session_store.set_notified(agent_id)
        log.info(
            f"[reconciler] Enqueued notification for agent {agent_id!r} "
            f"(outcome={outcome!r}, type={message['type']!r}, inbox={message['id']!r})"
        )
    except Exception as exc:
        log.error(
            f"[reconciler] Failed to enqueue notification for agent {agent_id!r}: {exc}",
            exc_info=True,
        )
        # Do NOT mark notified — next cycle will retry (at-least-once guarantee)


def _inbox_already_has_agent(agent_id: str) -> bool:
    """Return True if any file in the inbox already references agent_id.

    Used by _startup_sweep() as an idempotency guard: if the server crashed
    after writing the inbox file but before set_notified() was called, the
    file is already there and re-enqueuing would deliver the notification twice.

    Pure check — reads existing inbox files, no writes.
    """
    if not agent_id:
        return False
    for path in INBOX_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if data.get("agent_id") == agent_id:
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


async def _startup_sweep() -> None:
    """Send missed notifications for sessions that completed while server was down.

    Queries for completed/dead sessions where notified_at IS NULL and enqueues
    a synthetic inbox message for each. Called once at server startup before
    entering the reconciler loop.

    This implements the restart-safety guarantee: if the server was killed between
    marking a session completed and writing notified_at, the notification is
    re-sent on the next startup. The at-most-once property is upheld by
    set_notified() being called immediately after enqueueing.

    An additional idempotency guard checks whether an inbox file for the agent
    already exists (handles the crash-after-write-before-set_notified race).
    """
    try:
        unnotified = _session_store.get_unnotified_completed(since_hours=24)
        if unnotified:
            log.info(
                f"[reconciler] Startup sweep: found {len(unnotified)} unnotified "
                f"completed/dead session(s) — re-enqueuing notifications"
            )
        for session in unnotified:
            agent_id = session.get("id", "")
            if _inbox_already_has_agent(agent_id):
                log.debug(
                    "[reconciler] Startup sweep: inbox file already exists for "
                    "agent %r — skipping re-enqueue (idempotency guard)",
                    agent_id,
                )
                continue
            outcome = session.get("status", "completed")
            _enqueue_reconciler_notification(session, outcome=outcome)
    except Exception as exc:
        log.error(f"[reconciler] Startup sweep error: {exc}", exc_info=True)


async def reconcile_agent_sessions() -> None:
    """Background task: auto-close finished or dead agent sessions.

    Runs every 5 seconds inside the inbox_server asyncio event loop.
    Uses scan_agent_outputs() — a deterministic check of the ``stop_reason``
    field in Claude Code JSONL output files — to detect completion without
    requiring any cooperation from the agent itself.

    Reconciliation rules:
      - Session in DB with status='running', scanner says 'done'
        → call session_end(..., status='completed'), enqueue Telegram notification
      - Session in DB with status='running', output file missing AND session
        is older than its registered timeout_minutes (default 30 min) →
        call session_end(..., status='dead'), enqueue Telegram notification
      - Session in DB with status='running', file shows stop_reason=tool_use AND
        session is older than its registered timeout_minutes (default 120 min) →
        call session_end(..., status='dead'), enqueue Telegram notification
      - Sessions found by scanner but not in DB: 'running' orphans are logged;
        'done' orphans are logged at WARNING level (no chat_id, cannot notify)

    Bug fixes vs previous version:
      - TypeError: elapsed is coerced to 0 when None (was unguarded comparison)
      - scan_key: always uses agent_id as fallback; only uses output_file stem
        when the output_file path exists in the scan results (prevents mismatch)
      - Done orphans: now logged at WARNING level (were silently skipped)
      - Notification gap: now enqueues inbox message on every COMPLETED/DEAD
        transition (was silently closing sessions with no notification)

    This eliminates the mtime-based heuristic used previously and provides
    ~5-second latency from agent completion to DB reconciliation.

    Fix (issue #400): The reconciler now uses check_output_file_status() to
    read each session's output_file directly from the DB, instead of scanning a
    hardcoded directory. This eliminates the path mismatch that caused the
    reconciler to always return an empty scan. The previous scan_agent_outputs()
    approach relied on a default path (/tmp/claude-1000/-home-admin-lobster-workspace/tasks)
    that does not exist on this system — and even if fixed, Claude Code places
    output symlinks in project-specific subdirectories, not a flat tasks dir.
    """
    from agents.session_store import check_output_file_status, get_output_file_mtime

    DEFAULT_DEAD_THRESHOLD_SECONDS = 30 * 60   # 30 minutes — fallback for missing output files
    DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS = 120 * 60  # 120 minutes — fallback for stuck tool_use files
    GRACE_PERIOD_SECONDS = 30          # Newly spawned agents get grace before DEAD
    # Mtime staleness gate (issue #868): if output file hasn't been written to in
    # this many seconds, treat the agent as interrupted rather than actively running.
    # An active agent updates its JSONL output continuously; an interrupted one stops
    # immediately. 15 minutes gives ample margin to avoid false positives during slow
    # tool calls, while cutting the misclassification window from 120 min → 30 min.
    MTIME_STALE_THRESHOLD_SECONDS = 15 * 60    # 15 minutes

    # Startup sweep: re-send notifications for sessions that completed while down
    await _startup_sweep()

    while True:
        await asyncio.sleep(5)
        try:
            active_sessions = _session_store.get_active_sessions()

            for session in active_sessions:
                agent_id = session.get("id", "")
                output_file = session.get("output_file") or ""

                # Issue #781 Fix 2: Skip dispatcher-type sessions entirely.
                # The SessionStart hook may register the dispatcher itself in
                # agent_sessions.db with agent_type='dispatcher' (on crash-restart
                # before the marker file is cleared).  The dispatcher is never a
                # "dead agent" — never emit agent_failed for it.
                if (session.get("agent_type") or "") == "dispatcher":
                    log.debug(
                        f"[reconciler] Skipping dispatcher session {agent_id!r} "
                        "(agent_type='dispatcher' — not a subagent)"
                    )
                    continue

                # Fix: guard elapsed against None before any numeric comparison
                elapsed_raw = session.get("elapsed_seconds")
                try:
                    elapsed = int(elapsed_raw) if elapsed_raw is not None else 0
                except (TypeError, ValueError):
                    elapsed = 0

                # Derive per-session thresholds from registered timeout_minutes.
                # Mirrors the pattern used in session_store.cleanup_stale_running_sessions().
                timeout_minutes_raw = session.get("timeout_minutes")
                try:
                    timeout_minutes = int(timeout_minutes_raw) if timeout_minutes_raw is not None else None
                except (TypeError, ValueError):
                    timeout_minutes = None

                if timeout_minutes is not None:
                    dead_threshold_running = timeout_minutes * 60
                    # For the "missing file" branch use the same registered timeout,
                    # but floor it at the default so very-short registrations don't
                    # cause premature kills before the grace period expires.
                    dead_threshold_missing = max(timeout_minutes * 60, DEFAULT_DEAD_THRESHOLD_SECONDS)
                else:
                    dead_threshold_running = DEFAULT_DEAD_THRESHOLD_RUNNING_SECONDS
                    dead_threshold_missing = DEFAULT_DEAD_THRESHOLD_SECONDS

                # Check this agent's output file directly using the path stored in DB.
                # This avoids the directory-scan approach that relied on a hardcoded
                # (and often wrong) default path. When output_file is absent, the
                # status is "missing" and the dead-threshold logic applies as before.
                file_status = check_output_file_status(output_file)

                if file_status == "done":
                    log.info(
                        f"[reconciler] Agent {agent_id!r} finished (stop_reason=end_turn) "
                        f"— marking completed (output_file={output_file!r})"
                    )
                    _session_store.session_end(
                        id_or_task_id=agent_id,
                        status="completed",
                        result_summary="Auto-closed by reconciler: stop_reason=end_turn",
                        stop_reason="end_turn",
                    )
                    # Enqueue Telegram notification (the critical missing step)
                    _enqueue_reconciler_notification(session, outcome="completed")
                    # Notify wire server so dashboard updates within 40ms
                    asyncio.create_task(_notify_wire_server())

                elif file_status == "missing":
                    if elapsed < GRACE_PERIOD_SECONDS:
                        # Agent just spawned — output file not yet created. Normal.
                        pass
                    elif elapsed > dead_threshold_missing:
                        log.warning(
                            f"[reconciler] Agent {agent_id!r} output file missing after "
                            f"{elapsed}s (>{dead_threshold_missing}s) "
                            f"— marking dead (output_file={output_file!r})"
                        )
                        _session_store.session_end(
                            id_or_task_id=agent_id,
                            status="dead",
                            result_summary=f"Auto-closed by reconciler: output missing after {elapsed}s",
                            stop_reason="killed",
                        )
                        # Enqueue Telegram notification (failure case)
                        _enqueue_reconciler_notification(session, outcome="dead")
                        # Notify wire server so dashboard updates within 40ms
                        asyncio.create_task(_notify_wire_server())
                    else:
                        # Between grace period and dead threshold — wait and watch
                        log.debug(
                            f"[reconciler] Agent {agent_id!r} output missing, "
                            f"elapsed {elapsed}s — within window, waiting"
                        )
                elif file_status == "running":
                    # File exists but no stop_reason=end_turn (either tool_use or
                    # no stop_reason at all — both return "running" from the scanner).
                    # This is normal for live agents, but two failure modes land here:
                    #   1. Agent killed mid-turn (no stop_reason written) — file mtime
                    #      stops updating immediately; detectable within 15 minutes.
                    #   2. Legitimately slow tool call — mtime keeps ticking; leave alone.
                    #
                    # Mtime gate (issue #868): if the output file has been idle for
                    # MTIME_STALE_THRESHOLD_SECONDS, use the short threshold (same as
                    # the "missing file" branch) rather than the generous 120-minute cap.
                    # This closes the gap where interrupted agents are misclassified as
                    # "still running" for up to 120 minutes.
                    output_mtime = get_output_file_mtime(output_file)
                    now_ts = time.time()
                    file_is_stale = (
                        output_mtime is not None
                        and (now_ts - output_mtime) > MTIME_STALE_THRESHOLD_SECONDS
                    )
                    effective_running_threshold = (
                        dead_threshold_missing if file_is_stale else dead_threshold_running
                    )

                    if elapsed > effective_running_threshold:
                        stale_note = (
                            f", file idle {int(now_ts - output_mtime)}s (mtime gate active)"
                            if file_is_stale
                            else ""
                        )
                        log.warning(
                            f"[reconciler] Agent {agent_id!r} output stuck at tool_use "
                            f"after {elapsed}s (>{effective_running_threshold}s{stale_note}) "
                            f"— marking dead (output_file={output_file!r})"
                        )
                        _session_store.session_end(
                            id_or_task_id=agent_id,
                            status="dead",
                            result_summary=(
                                f"Auto-closed by reconciler: output idle "
                                f"after {elapsed}s"
                                + (f" (mtime stale {int(now_ts - output_mtime)}s)" if file_is_stale else "")
                            ),
                        )
                        _enqueue_reconciler_notification(session, outcome="dead")
                        asyncio.create_task(_notify_wire_server())

            # Phase 3: Detect orphans (no output_file registered but elapsed > threshold)
            # The previous orphan detection scanned the tasks directory for files not in the DB.
        except Exception as exc:
            log.error(f"[reconciler] Error in reconcile_agent_sessions: {exc}", exc_info=True)


async def main():
    """Run the MCP server."""
    setup_logging()
    _ensure_observation_worker()

    # Clear the dispatcher state file on startup so a stale session ID from a
    # previous run cannot linger and mislead hooks into thinking the old session
    # is still active.  The file is re-written by _tag_dispatcher_session() once
    # the dispatcher identifies itself via Options A, B, or C.
    _clear_dispatcher_state_file()

    # Write a session-lost-reminder to the inbox so the dispatcher knows to
    # re-orient after reconnecting.  Skipped for mid-session MCP reconnects.
    # See _write_session_lost_reminder() for full rationale.
    _write_session_lost_reminder()

    # Initialize the event bus singleton with standard listeners.
    # JsonlFileListener writes all events to logs/events.jsonl.
    # TelegramOutboxListener delivers debug events to Telegram when LOBSTER_DEBUG=true.
    # This is a no-op if called more than once (idempotent).
    from event_bus import init_event_bus
    init_event_bus()

    # Startup cleanup: mark stale 'running' rows as 'dead' before reconciler loop begins.
    # After a force-restart, agents killed mid-run leave their output files with
    # stop_reason=tool_use, which the reconciler treats as still-running. We fix this
    # here by checking file existence and stop_reason against the server start time.
    #
    # This runs here (inside main) rather than at module level so that importing
    # this module from inbox_server_http.py does NOT trigger the cleanup.  A
    # crash-looping HTTP bridge would otherwise incorrectly kill every running
    # agent on every restart.
    #
    # Note on asymmetric notification: this sweep intentionally does NOT enqueue
    # user notifications for the sessions it marks dead. Any sessions completed or
    # dead before this restart but not yet notified are handled by the reconciler's
    # _startup_sweep(), which fires immediately after the reconciler loop starts.
    try:
        _dead_ids = _session_store.cleanup_stale_running_sessions(
            server_start_time=_SERVER_START_TIME
        )
        if _dead_ids:
            log.warning(
                f"[startup] Marked {len(_dead_ids)} stale 'running' session(s) as dead "
                f"(pre-existing from before this server start): {_dead_ids}"
            )
        else:
            log.info("[startup] No stale 'running' sessions found at startup")
    except Exception as _cleanup_err:
        log.warning(f"[startup] Stale session cleanup failed (non-fatal): {_cleanup_err}")

    asyncio.create_task(reconcile_agent_sessions())

    # Transport selection: HTTP (streamable-http) or stdio.
    #
    # HTTP mode is activated by --http flag or MCP_TRANSPORT=http env var.
    # In HTTP mode the server binds to localhost:PORT (default 8766) and
    # Claude Code connects via "url": "http://localhost:PORT/mcp" in settings.json.
    # The server process is managed by the lobster-mcp-local systemd service, so
    # its lifetime is decoupled from the Claude Code process — CC restarts no
    # longer kill the MCP server.
    #
    # stdio mode is the legacy default; it remains available for local dev and
    # fallback scenarios.
    use_http = (
        "--http" in sys.argv
        or os.environ.get("MCP_TRANSPORT", "").lower() == "http"
    )

    if use_http:
        import contextlib
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        # Determine port (--port N or MCP_HTTP_PORT env, default 8766)
        port = int(os.environ.get("MCP_HTTP_PORT", 8766))
        if "--port" in sys.argv:
            try:
                port = int(sys.argv[sys.argv.index("--port") + 1])
            except (ValueError, IndexError):
                pass

        session_manager = StreamableHTTPSessionManager(app=server, stateless=False)

        # Publish the session_manager reference so tool handlers (and /dispatcher-session)
        # can inspect live session state without importing from main().
        global _http_session_manager
        _http_session_manager = session_manager

        @contextlib.asynccontextmanager
        async def _lifespan(app: Starlette):
            async with session_manager.run():
                log.info(f"[http-transport] Lobster MCP server listening on http://localhost:{port}/mcp")
                yield

        async def _mcp_handler(scope, receive, send):
            request = Request(scope, receive)
            path = request.url.path
            if path == "/health":
                response = Response('{"ok":true}', status_code=200, media_type="application/json")
                await response(scope, receive, send)
            elif path == "/mcp":
                await session_manager.handle_request(scope, receive, send)
            elif path == "/dispatcher-session":
                # Returns the currently tagged dispatcher session ID and whether
                # that session is still active in the session manager.
                # Used by health checks and the reconciler to verify the dispatcher
                # is connected.  Returns 200 with JSON payload in all cases;
                # callers should check the "active" field.
                dsid = _dispatcher_session_id
                active = (
                    dsid is not None
                    and dsid in getattr(session_manager, "_server_instances", {})
                )
                payload = json.dumps({"session_id": dsid, "active": active})
                response = Response(payload, status_code=200, media_type="application/json")
                await response(scope, receive, send)
            else:
                response = Response("Not Found", status_code=404)
                await response(scope, receive, send)

        _inner_app = Starlette(lifespan=_lifespan)

        async def _asgi_app(scope, receive, send):
            if scope["type"] == "lifespan":
                await _inner_app(scope, receive, send)
            elif scope["type"] == "http":
                await _mcp_handler(scope, receive, send)

        config = uvicorn.Config(
            _asgi_app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
        http_server = uvicorn.Server(config)
        await http_server.serve()
    else:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
