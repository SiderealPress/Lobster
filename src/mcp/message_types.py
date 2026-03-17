"""
Formal message type taxonomy for the Lobster inbox bus (issue #156).

This module is intentionally dependency-free so it can be imported and tested
without pulling in the full inbox_server stack (MCP, SQLite, watchdog, etc.).

Every inbox message carries two required routing fields:
  source — who sent it (telegram, slack, sms, system, …)
  type   — what kind of content (text, voice, subagent_result, …)

The constants here are the single source of truth. inbox_server.py imports
from this module; nothing else should define its own ad-hoc type strings.
"""

# ---------------------------------------------------------------------------
# User-initiated types  (source = telegram | slack | sms | signal | whatsapp | bisque)
# ---------------------------------------------------------------------------
INBOX_USER_TYPES: frozenset[str] = frozenset({
    "text",       # plain text message
    "message",    # generic message (alias used by some connectors)
    "photo",      # image/photo attachment
    "image",      # image (alias)
    "voice",      # voice/audio message (needs transcription)
    "audio",      # audio attachment (alias)
    "video",      # video attachment
    "document",   # file/document attachment
    "sticker",    # sticker message
    "location",   # location pin
    "callback",   # inline keyboard button press
})

# ---------------------------------------------------------------------------
# System-generated types  (source = system)
# ---------------------------------------------------------------------------
INBOX_SYSTEM_TYPES: frozenset[str] = frozenset({
    "self_check",             # periodic health/reminder injection
    "subagent_result",        # subagent completed work (fields: task_id, payload, artifacts?)
    "subagent_error",         # subagent failed (fields: task_id, error, retry_count)
    "subagent_notification",  # subagent already sent reply via send_reply (no re-delivery)
    "subagent_observation",   # subagent noticed something in passing (debug/context)
    "subagent_stale_check",   # dispatch registry found agent with stale heartbeat
    "subagent_recovered",     # subagent fallback recovery event (chat_id unknown; dispatcher handles, never relay directly)
    "compact_group",          # grouped compact messages (internal, produced by check_inbox)
    "compact_reminder",       # on-compact hook reminder (hooks/on-compact.py)
    "cron_reminder",          # scheduled reminder (scheduled-tasks/post-reminder.sh)
    "scheduled_reminder",     # scheduled reminder (scripts/post-reminder.sh)
    "update_notification",    # system update available (scripts/daily-update-check.sh)
    "consolidation",          # nightly consolidation result
    "observation",            # OOM-monitor or similar system observation
    "system",                 # generic system message (whatsapp health check, etc.)
})

# ---------------------------------------------------------------------------
# Combined set — all known types
# ---------------------------------------------------------------------------
INBOX_MESSAGE_TYPES: frozenset[str] = INBOX_USER_TYPES | INBOX_SYSTEM_TYPES

# ---------------------------------------------------------------------------
# Known sources
# ---------------------------------------------------------------------------
INBOX_MESSAGE_SOURCES: frozenset[str] = frozenset({
    "telegram",
    "slack",
    "sms",
    "signal",
    "whatsapp",
    "bisque",
    "system",
})

# ---------------------------------------------------------------------------
# Types that represent direct user-facing messages requiring a reply.
# Used by mark_processed to guard against dropping human messages silently.
# subagent_result / subagent_error are excluded: they are system routing
# messages even though they carry source="telegram" for delivery purposes.
# ---------------------------------------------------------------------------
USER_FACING_TYPES: frozenset[str] = INBOX_USER_TYPES
