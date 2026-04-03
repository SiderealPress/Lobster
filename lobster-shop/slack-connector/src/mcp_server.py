#!/usr/bin/env python3
"""
Slack Connector MCP Server — exposes Slack log search, channel summaries,
thread summaries, and status as MCP tools for Claude Code.

Runs as a standalone stdio MCP server. Registered in ~/.claude.json by
the skill's install.sh.

Design principles:
- Pure query functions composed with thin MCP wrappers
- All reads hit local log files and SQLite indexes — no Slack API calls
- FTS5 keyword search with automatic fallback to JSONL scan
- Immutable data: query results are snapshots, never mutated
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("slack-connector-mcp")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
log.addHandler(_handler)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WORKSPACE = Path(
    os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
)
_SLACK_DIR = _WORKSPACE / "slack-connector"
_LOG_ROOT = _SLACK_DIR / "logs"
_STATE_DIR = _SLACK_DIR / "state"
_CONFIG_DIR = _SLACK_DIR / "config"

# Add skill src to path so we can import sibling modules
_SKILL_SRC = Path(__file__).resolve().parent
if str(_SKILL_SRC) not in sys.path:
    sys.path.insert(0, str(_SKILL_SRC))


# ---------------------------------------------------------------------------
# Lazy imports — these modules live in the same src/ directory.
# Imported at call time to avoid startup failures if log directories
# don't exist yet.
# ---------------------------------------------------------------------------


def _get_log_store():
    """Lazy import and instantiate SlackLogStore."""
    from log_store import SlackLogStore
    return SlackLogStore(log_root=_LOG_ROOT)


def _get_keyword_index():
    """Lazy import and instantiate KeywordIndex. Returns None if unavailable."""
    try:
        from keyword_index import KeywordIndex
        idx = KeywordIndex(state_dir=_STATE_DIR)
        # Verify FTS5 is available by touching the DB
        idx._ensure_db()
        return idx
    except Exception as e:
        log.warning("FTS5 keyword index unavailable: %s", e)
        return None


def _get_channel_config():
    """Lazy import and instantiate ChannelConfig."""
    from channel_config import ChannelConfig
    config_path = _CONFIG_DIR / "channels.yaml"
    return ChannelConfig(config_path=str(config_path))


# ---------------------------------------------------------------------------
# Pure helpers — data transformation with no side effects
# ---------------------------------------------------------------------------


def _today_str() -> str:
    """Current date as YYYY-MM-DD string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_n_days_ago(n: int) -> str:
    """Date N days ago as YYYY-MM-DD string."""
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.strftime("%Y-%m-%d")


def _parse_date_or_default(date_str: str | None, default: str) -> str:
    """Validate a YYYY-MM-DD string, returning default if invalid/missing."""
    if not date_str:
        return default
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        return default


def _filter_messages_by_query(
    messages: list[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    """Filter messages by case-insensitive substring match on text field.

    Pure function — JSONL fallback when FTS5 is unavailable.
    """
    query_lower = query.lower()
    terms = query_lower.split()
    return [
        msg for msg in messages
        if all(term in msg.get("text", "").lower() for term in terms)
    ]


def _format_message_for_display(msg: dict[str, Any]) -> dict[str, Any]:
    """Extract display-relevant fields from a log record.

    Pure function — strips raw event data, keeps only what's needed
    for search results and summaries.
    """
    return {
        "ts": msg.get("ts", ""),
        "channel_id": msg.get("channel_id", ""),
        "channel_name": msg.get("channel_name", ""),
        "user_id": msg.get("user_id", ""),
        "username": msg.get("username", ""),
        "display_name": msg.get("display_name", ""),
        "text": msg.get("text", ""),
        "thread_ts": msg.get("thread_ts"),
    }


def _group_by_thread(
    messages: list[dict[str, Any]],
) -> dict[str | None, list[dict[str, Any]]]:
    """Group messages by thread_ts. Messages without a thread go under None.

    Pure function.
    """
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for msg in messages:
        key = msg.get("thread_ts")
        groups.setdefault(key, []).append(msg)
    return groups


def _build_channel_summary(
    messages: list[dict[str, Any]], channel_id: str, date: str
) -> dict[str, Any]:
    """Build a structured channel summary from a day's messages.

    Pure function — no I/O.
    """
    if not messages:
        return {
            "channel_id": channel_id,
            "date": date,
            "message_count": 0,
            "participants": [],
            "threads": 0,
            "summary": "No messages found for this channel on this date.",
        }

    participants = sorted(set(
        msg.get("username") or msg.get("user_id", "unknown")
        for msg in messages
    ))

    threads = _group_by_thread(messages)
    # Count threads that have at least 2 messages (actual conversations)
    thread_count = sum(
        1 for ts, msgs in threads.items()
        if ts is not None and len(msgs) >= 2
    )

    # Build a text digest of the messages for summarization
    sample_texts = [
        msg.get("text", "")[:200]
        for msg in messages[:50]
        if msg.get("text", "").strip()
    ]

    return {
        "channel_id": channel_id,
        "date": date,
        "message_count": len(messages),
        "participants": participants,
        "participant_count": len(participants),
        "threads": thread_count,
        "messages": [_format_message_for_display(m) for m in messages[:100]],
        "sample_texts": sample_texts[:20],
    }


def _build_thread_summary(
    messages: list[dict[str, Any]], channel_id: str, thread_ts: str
) -> dict[str, Any]:
    """Build a structured thread summary from thread messages.

    Pure function — no I/O.
    """
    # Filter to only messages in this thread
    thread_msgs = [
        msg for msg in messages
        if msg.get("thread_ts") == thread_ts or msg.get("ts") == thread_ts
    ]

    if not thread_msgs:
        return {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "message_count": 0,
            "participants": [],
            "messages": [],
        }

    participants = sorted(set(
        msg.get("username") or msg.get("user_id", "unknown")
        for msg in thread_msgs
    ))

    return {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "message_count": len(thread_msgs),
        "participants": participants,
        "messages": [_format_message_for_display(m) for m in thread_msgs],
    }


def _count_events_today(log_root: Path) -> int:
    """Count total log events across all channels for today.

    Read-only file I/O — counts lines in today's JSONL files.
    """
    today = _today_str()
    total = 0
    for category in ("channels", "dms"):
        category_dir = log_root / category
        if not category_dir.exists():
            continue
        for channel_dir in category_dir.iterdir():
            if not channel_dir.is_dir():
                continue
            log_file = channel_dir / f"{today}.jsonl"
            if log_file.exists():
                with open(log_file) as f:
                    total += sum(1 for line in f if line.strip())
    return total


def _log_size_mb(log_root: Path) -> float:
    """Calculate total log directory size in MB.

    Read-only file I/O.
    """
    total_bytes = 0
    if not log_root.exists():
        return 0.0
    for path in log_root.rglob("*.jsonl"):
        total_bytes += path.stat().st_size
    return round(total_bytes / (1024 * 1024), 2)


def _last_event_timestamp(log_root: Path) -> str | None:
    """Find the timestamp of the most recent logged event.

    Reads the last line of the most recent JSONL file.
    """
    latest_file: Path | None = None
    latest_mtime: float = 0.0

    for category in ("channels", "dms"):
        category_dir = log_root / category
        if not category_dir.exists():
            continue
        for jsonl_file in category_dir.rglob("*.jsonl"):
            mtime = jsonl_file.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_file = jsonl_file

    if latest_file is None:
        return None

    # Read last non-empty line
    try:
        with open(latest_file) as f:
            last_line = ""
            for line in f:
                if line.strip():
                    last_line = line.strip()
            if last_line:
                record = json.loads(last_line)
                return record.get("logged_at") or record.get("ts")
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _count_trigger_rules(config_dir: Path) -> int:
    """Count loaded trigger rule files (.toml) in the rules directory."""
    rules_dir = config_dir / "rules"
    if not rules_dir.exists():
        return 0
    return sum(1 for f in rules_dir.iterdir() if f.suffix == ".toml")


def _build_status(
    log_root: Path,
    config_dir: Path,
    state_dir: Path,
    channels: list[str],
) -> dict[str, Any]:
    """Build a complete status snapshot.

    Combines pure computations with read-only file I/O.
    """
    # Check if slack router service is running
    connected = False
    try:
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-active", "lobster-slack-router"],
            capture_output=True, text=True, timeout=5,
        )
        connected = result.stdout.strip() == "active"
    except Exception:
        pass

    # Detect account type from preferences using proper TOML parsing.
    account_type = "bot"
    prefs_file = Path(__file__).resolve().parent.parent / "preferences" / "defaults.toml"
    if prefs_file.exists():
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(prefs_file, "rb") as _f:
                _prefs = tomllib.load(_f)
            account_type = _prefs.get("account_type", "bot")
        except Exception:
            pass

    return {
        "connected": connected,
        "account_type": account_type,
        "channels_monitored": len(channels),
        "channel_ids": channels,
        "events_logged_today": _count_events_today(log_root),
        "log_size_mb": _log_size_mb(log_root),
        "last_event_at": _last_event_timestamp(log_root),
        "trigger_rules_loaded": _count_trigger_rules(config_dir),
    }


# ---------------------------------------------------------------------------
# Tool handlers — thin wrappers that compose pure functions with I/O
# ---------------------------------------------------------------------------


def _handle_slack_log_search(arguments: dict[str, Any]) -> str:
    """Handle slack_log_search tool call.

    Strategy: try FTS5 first, fall back to JSONL scan.
    """
    query = arguments.get("query", "").strip()
    if not query:
        return json.dumps({"error": "query parameter is required"})

    channel_id = arguments.get("channel_id")
    start_date = _parse_date_or_default(
        arguments.get("start_date"), _date_n_days_ago(7)
    )
    end_date = _parse_date_or_default(
        arguments.get("end_date"), _today_str()
    )
    limit = min(arguments.get("limit", 50), 200)

    # Strategy 1: FTS5 keyword index
    idx = _get_keyword_index()
    if idx is not None:
        try:
            results = idx.search(query=query, channel_id=channel_id, limit=limit)
            if results:
                return json.dumps({
                    "source": "fts5",
                    "query": query,
                    "result_count": len(results),
                    "results": results,
                })
        except Exception as e:
            log.warning("FTS5 search failed, falling back to JSONL: %s", e)
        finally:
            idx.close()

    # Strategy 2: JSONL scan fallback
    # Enforce date-range guard: if no explicit dates were provided, the
    # defaults above already cap to last 7 days — re-check here so that
    # callers who pass explicit None values still get the default window.
    if not arguments.get("start_date"):
        start_date = _date_n_days_ago(7)
    if not arguments.get("end_date"):
        end_date = _today_str()

    store = _get_log_store()
    all_messages: list[dict[str, Any]] = []

    if channel_id:
        channels_to_scan = [channel_id]
    else:
        channels_to_scan = store.list_channels()

    for ch_id in channels_to_scan:
        # Stop scanning additional channels once the cap is reached.
        if len(all_messages) >= limit:
            break
        messages = store.query_range(ch_id, start_date, end_date)
        matching = _filter_messages_by_query(messages, query)
        remaining = limit - len(all_messages)
        all_messages.extend(
            _format_message_for_display(m) for m in matching[:remaining]
        )

    return json.dumps({
        "source": "jsonl_scan",
        "query": query,
        "date_range": {"start": start_date, "end": end_date},
        "result_count": len(all_messages),
        "results": all_messages,
    })


def _handle_slack_channel_summary(arguments: dict[str, Any]) -> str:
    """Handle slack_channel_summary tool call."""
    channel_id = arguments.get("channel_id", "").strip()
    if not channel_id:
        return json.dumps({"error": "channel_id parameter is required"})

    hours = arguments.get("hours", 24)
    date = _parse_date_or_default(arguments.get("date"), _today_str())

    store = _get_log_store()

    # If hours > 24, span multiple days
    if hours > 24:
        days_back = (hours // 24) + 1
        start_date = _date_n_days_ago(days_back)
        messages = store.query_range(channel_id, start_date, date)
    else:
        messages = store.query(channel_id, date)

    summary = _build_channel_summary(messages, channel_id, date)
    return json.dumps(summary)


def _handle_slack_thread_summary(arguments: dict[str, Any]) -> str:
    """Handle slack_thread_summary tool call."""
    channel_id = arguments.get("channel_id", "").strip()
    thread_ts = arguments.get("thread_ts", "").strip()

    if not channel_id or not thread_ts:
        return json.dumps(
            {"error": "channel_id and thread_ts parameters are required"}
        )

    store = _get_log_store()

    # Determine the date from thread_ts (Slack ts = epoch.sequence)
    try:
        epoch = float(thread_ts.split(".")[0])
        thread_date = datetime.fromtimestamp(epoch, tz=timezone.utc)
        date_str = thread_date.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        date_str = _today_str()

    # Search across a few days in case thread spans midnight
    start_date = (
        datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    end_date = (
        datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=3)
    ).strftime("%Y-%m-%d")

    messages = store.query_range(channel_id, start_date, end_date)
    summary = _build_thread_summary(messages, channel_id, thread_ts)
    return json.dumps(summary)


def _handle_slack_status(arguments: dict[str, Any]) -> str:
    """Handle slack_status tool call."""
    store = _get_log_store()
    channels = store.list_channels()
    status = _build_status(_LOG_ROOT, _CONFIG_DIR, _STATE_DIR, channels)
    return json.dumps(status)


def _handle_slack_onboarding_state(arguments: dict[str, Any]) -> str:
    """Handle slack_onboarding_state tool call.

    Reads or writes onboarding state for a given chat_id.
    This lets the dispatcher resume a partially-completed onboarding after
    a session restart.

    Operations:
        get   — returns current onboarding state for chat_id.
        set   — merges provided fields into existing state and saves.
        clear — deletes the state file for chat_id (resets the flow).
    """
    import sys as _sys
    if str(_SKILL_SRC) not in _sys.path:
        _sys.path.insert(0, str(_SKILL_SRC))

    from onboarding import (
        get_onboarding_state,
        save_onboarding_state,
        clear_onboarding_state,
    )

    op = arguments.get("op", "get")
    chat_id = str(arguments.get("chat_id", ""))

    if not chat_id:
        return json.dumps({"error": "chat_id is required"})

    if op == "get":
        state = get_onboarding_state(chat_id, state_dir=_STATE_DIR)
        return json.dumps(state.to_dict())

    if op == "set":
        state = get_onboarding_state(chat_id, state_dir=_STATE_DIR)
        updates = {
            k: v for k, v in arguments.items()
            if k not in ("op", "chat_id")
        }
        # Apply field updates safely — only update known fields
        known_fields = {
            "step", "mode", "bot_token", "app_token", "person_token",
            "workspace_name", "available_channels", "selected_channels",
            "channel_modes", "last_token_message_id",
        }
        state_dict = state.to_dict()
        for field_name, value in updates.items():
            if field_name in known_fields:
                state_dict[field_name] = value

        from onboarding import OnboardingState
        updated_state = OnboardingState.from_dict(state_dict)
        save_onboarding_state(updated_state, state_dir=_STATE_DIR)
        return json.dumps({"ok": True, "state": updated_state.to_dict()})

    if op == "clear":
        clear_onboarding_state(chat_id, state_dir=_STATE_DIR)
        return json.dumps({"ok": True, "cleared": chat_id})

    return json.dumps({"error": f"Unknown op: {op!r}. Use get, set, or clear."})


# ---------------------------------------------------------------------------
# MCP Server definition
# ---------------------------------------------------------------------------

server = Server("slack-connector")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available Slack Connector tools."""
    return [
        Tool(
            name="slack_log_search",
            description=(
                "Search Slack message logs by keyword. Uses FTS5 full-text "
                "index when available, falls back to JSONL file scan. "
                "Returns matching messages with timestamps, authors, and text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keywords, FTS5 syntax supported: AND, OR, NOT, phrases)",
                    },
                    "channel_id": {
                        "type": "string",
                        "description": "Optional: limit search to a specific channel ID",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date (YYYY-MM-DD). Default: 7 days ago",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (YYYY-MM-DD). Default: today",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 50, max 200)",
                        "default": 50,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="slack_channel_summary",
            description=(
                "Generate a structured summary of a Slack channel's activity. "
                "Returns message count, participant list, thread count, and "
                "sample messages for a given date or time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Slack channel ID to summarize",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date to summarize (YYYY-MM-DD). Default: today",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Hours of history to include (default 24). If >24, spans multiple days.",
                        "default": 24,
                    },
                },
                "required": ["channel_id"],
            },
        ),
        Tool(
            name="slack_thread_summary",
            description=(
                "Summarize a specific Slack thread. Returns all messages in "
                "the thread with participants and message count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Channel containing the thread",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "Thread root timestamp (Slack ts format: epoch.sequence)",
                    },
                },
                "required": ["channel_id", "thread_ts"],
            },
        ),
        Tool(
            name="slack_status",
            description=(
                "Get Slack Connector status: connection state, account type, "
                "monitored channels, events logged today, log size, and "
                "trigger rules loaded. No arguments required."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="slack_onboarding_state",
            description=(
                "Read or write Slack Connector onboarding state for a given "
                "Telegram chat_id. Allows the dispatcher to resume a "
                "partially-completed /slack-setup flow after a session restart. "
                "Operations: get (read state), set (update fields), clear (reset)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "description": "Operation: 'get', 'set', or 'clear'",
                        "enum": ["get", "set", "clear"],
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Telegram chat ID of the user running setup",
                    },
                    "step": {
                        "type": "string",
                        "description": "Current onboarding step name (set op only)",
                    },
                    "mode": {
                        "type": "string",
                        "description": "Account mode: 'bot' or 'person' (set op only)",
                    },
                    "bot_token": {
                        "type": "string",
                        "description": "Collected bot token (set op only)",
                    },
                    "app_token": {
                        "type": "string",
                        "description": "Collected app token (set op only)",
                    },
                    "person_token": {
                        "type": "string",
                        "description": "Collected person/user token (set op only)",
                    },
                    "workspace_name": {
                        "type": "string",
                        "description": "Validated workspace name (set op only)",
                    },
                    "available_channels": {
                        "type": "array",
                        "description": "List of available channels from conversations.list (set op only)",
                    },
                    "selected_channels": {
                        "type": "array",
                        "description": "Channel IDs the user selected (set op only)",
                    },
                    "channel_modes": {
                        "type": "object",
                        "description": "channel_id → mode mapping (set op only)",
                    },
                    "last_token_message_id": {
                        "type": "integer",
                        "description": "Telegram message ID of last token message, for deletion (set op only)",
                    },
                },
                "required": ["op", "chat_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls to handlers."""
    dispatch = {
        "slack_log_search": _handle_slack_log_search,
        "slack_channel_summary": _handle_slack_channel_summary,
        "slack_thread_summary": _handle_slack_thread_summary,
        "slack_status": _handle_slack_status,
        "slack_onboarding_state": _handle_slack_onboarding_state,
    }

    handler = dispatch.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        result = handler(arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        log.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    """Run the Slack Connector MCP server over stdio."""
    log.info("Starting slack-connector MCP server")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
