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
    "message",    # DEPRECATED alias — normalizes to "text" on ingest
    "photo",      # image/photo attachment
    "image",      # DEPRECATED alias — normalizes to "photo" on ingest (Slack producer)
    "voice",      # voice/audio message (needs transcription)
    "audio",      # DEPRECATED alias — normalizes to "voice" on ingest
    "video",      # video attachment
    "document",   # file/document attachment
    "sticker",    # sticker message
    "location",   # location pin
    "callback",   # inline keyboard button press
    "reaction",   # Telegram emoji reaction (fields: emoji, reacted_to_text, telegram_message_id)
})

# ---------------------------------------------------------------------------
# System-generated types  (source = system)
# ---------------------------------------------------------------------------
INBOX_SYSTEM_TYPES: frozenset[str] = frozenset({
    "self_check",             # periodic health/reminder injection
    "subagent_result",        # subagent completed work (fields: task_id, payload, artifacts?)
    "subagent_error",         # subagent failed (fields: task_id, error, retry_count)
    "subagent_ack",           # subagent already sent reply via send_reply (no re-delivery); canonical name
    "subagent_notification",  # DEPRECATED alias — use subagent_ack; kept for backward compat
    "subagent_observation",   # subagent noticed something in passing (debug/context)
    "subagent_stale_check",   # dispatch registry found agent with stale heartbeat
    "subagent_recovered",     # subagent fallback recovery event (chat_id unknown; dispatcher handles, never relay directly)
    "agent_failed",           # reconciler/agent-monitor detected dead agent (chat_id=0; dispatcher decides re-queue vs escalate vs drop)
    "compact_group",          # grouped compact messages (internal, produced by check_inbox)
    "compact_reminder",       # on-compact hook reminder (hooks/on-compact.py)
    "cron_reminder",          # DEPRECATED alias — normalizes to "scheduled_reminder" on ingest
    "scheduled_reminder",     # scheduled reminder (scripts/post-reminder.sh, scheduled-tasks/post-reminder.sh)
    "update_notification",    # system update available (scripts/check-updates.sh)
    "consolidation",          # nightly consolidation result
    "observation",            # OOM-monitor or similar system observation
    "health_check",           # health check output (replaces "task-output" and "system" from health check scripts)
    "system",                 # DEPRECATED alias — normalizes to "health_check" on ingest (from health check scripts)
    "task-output",            # DEPRECATED alias — normalizes to "health_check" on ingest (scripts/daily-health-check.sh)
    "debug_observation",      # debug output from inbox_server.py internals; excluded from skill processing
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
