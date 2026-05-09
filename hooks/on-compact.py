#!/usr/bin/env python3
"""
Context-compaction hook for Lobster.

Fires on every SessionStart (matcher="") and self-gates to compact events
using _is_compact_event().  The hook used to be registered with
matcher="compact", but investigation showed that Claude Code intermittently
fails to fire matcher="compact" hooks — the "" (always-fires) matcher is
the only reliable trigger for compact events.

Self-detection strategy (layered, most-to-least reliable):
  1. source field in stdin payload equals "compact" (CC-documented primary field)
  2. hook_name field equals "compact" (fallback for older CC versions; only
     checked when source is absent from the payload)
  3. Filesystem fallback: when both source and hook_name are absent, check
     ~/lobster-workspace/logs/dispatcher-heartbeat.  A Unix epoch integer
     written within the last 15 minutes means the dispatcher was actively
     running immediately before this session — a strong signal that the
     compaction interrupted an active session.  A missing file, non-integer
     content, or a stale timestamp (>15 min) → False (fresh start).
     Override via LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE env var (test isolation).

The script injects a system message into the Lobster inbox so that the next
call to wait_for_messages() surfaces a reminder to re-read CLAUDE.md and
re-orient from handoff/memory context.

The script is idempotent: if a compact-reminder message already exists in
inbox/ or processing/ it skips writing a duplicate.

Compaction detection (self-gate): three-tier, see Self-detection strategy above.
  If no tier matches, the script exits immediately (sys.exit(0)).

Notification: always writes a compaction notification to ~/messages/outbox/
(the Lobster outbox pipeline) so the user is notified via Telegram.  The
outbox watcher picks up the file and delivers it to the correct transport.
This matches the architectural pattern used by all other Lobster notifications
and avoids direct Telegram API calls from the hook.

State: always writes compacted_at to lobster-state.json so that the health
check can suppress stale-inbox false-positives during the compaction pause.
Also writes last_compaction_ts to compaction-state.json so that the catch-up
subagent knows which window of history to recover after compaction.

Logging: writes a structured line to ~/lobster-workspace/logs/on-compact.log
on every invocation (both compact and non-compact), with outcome
(skipped/sent/failed) for the Telegram notification.

Restart-reason tracking: writes
~/lobster-workspace/data/last-restart-reason.json with
{"reason": "compaction", "ts": "<ISO UTC>"} when a compact event is detected.

Dispatcher-only: exits immediately for subagent sessions (detected via
_is_dispatcher_compact(), which extends session_role.is_dispatcher() with a
LOBSTER_MAIN_SESSION fallback).  Subagent compactions must not write
compact-reminders or the sentinel — those signals are only meaningful to
the dispatcher.

Compaction session_id change: CC assigns a NEW session_id to the post-compact
session, so the startup flag (consumed by inject-bootup-context.py in the
previous session) is no longer present. _is_dispatcher_compact() falls back to
checking LOBSTER_MAIN_SESSION=1, which the dispatcher launcher sets before
exec-ing claude.
"""

import json
import os
import sys
import time
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import (
    is_dispatcher,
)


INBOX_DIR = Path(os.path.expanduser("~/messages/inbox"))
PROCESSING_DIR = Path(os.path.expanduser("~/messages/processing"))
PROCESSED_DIR = Path(os.path.expanduser("~/messages/processed"))
OUTBOX_DIR = Path(
    os.environ.get(
        "LOBSTER_OUTBOX_DIR_OVERRIDE",
        os.path.expanduser("~/messages/outbox"),
    )
)
CONFIG_ENV = Path(os.path.expanduser("~/lobster-config/config.env"))
STATE_FILE = Path(
    os.environ.get(
        "LOBSTER_STATE_FILE_OVERRIDE",
        os.path.expanduser("~/messages/config/lobster-state.json"),
    )
)
# Compaction state: records last_compaction_ts for catch-up subagent windowing.
COMPACTION_STATE_FILE = Path(
    os.environ.get(
        "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/data/compaction-state.json"),
    )
)
# Simple timestamp file read by health-check-v3.sh for a 10-minute grace period
# after compaction (prevents stale-inbox alerts during post-compaction re-orientation).
LAST_COMPACT_TS_FILE = Path(
    os.environ.get(
        "LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/data/last-compact.ts"),
    )
)
# Log file for on-compact.py invocations — written on every fire.
COMPACT_LOG_FILE = Path(
    os.environ.get(
        "LOBSTER_COMPACT_LOG_FILE_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/logs/on-compact.log"),
    )
)
# Restart-reason tracking: written by both on-compact.py and health-check-v3.sh.
LAST_RESTART_REASON_FILE = Path(
    os.environ.get(
        "LOBSTER_LAST_RESTART_REASON_FILE_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/data/last-restart-reason.json"),
    )
)

# Dispatcher heartbeat file — written by thinking-heartbeat.py (PostToolUse)
# and the WFM heartbeat thread in inbox_server.py every 60s.  Content is a
# Unix epoch integer (e.g. "1713456789\n").  Used as a fallback compaction
# signal when both source and hook_name are absent from the CC payload: if the
# heartbeat was written within DISPATCHER_WFM_RECENCY_SECONDS, the dispatcher
# was actively running immediately before this session — a strong signal that
# this is a compaction rather than a fresh start.
# Override: LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE env var (test isolation,
# matching the existing override used by thinking-heartbeat.py and health-check).
DISPATCHER_HEARTBEAT_FILE = Path(
    os.environ.get(
        "LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/logs/dispatcher-heartbeat"),
    )
)

# Maximum age (seconds) for the dispatcher heartbeat to be treated as "recently
# active" in the tier-3 compaction fallback.  15 minutes is conservative: it
# covers the longest normal idle period (WFM blocking with no messages), while
# being short enough to avoid false-positives after a genuine long-idle restart
# (e.g. Lobster was stopped for an hour and then restarted cleanly).
DISPATCHER_WFM_RECENCY_SECONDS = 900  # 15 minutes

# Startup-cause tracking: written by on-compact.py when a compaction is detected.
# read by inject-bootup-context.py at the start of the next session to
# determine whether the session is post-compact or a fresh start.
# Override: LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE env var (test isolation).
STARTUP_CAUSE_FILE = Path(
    os.environ.get(
        "LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/data/last-startup-cause.json"),
    )
)

REMINDER_TEXT = (
    "COMPACT REMINDER \u2014 RE-ORIENT NOW\n\n"
    "You are Lobster, the always-on dispatcher. Your role has not changed.\n\n"
    "Identity check:\n"
    "- You run in an infinite main loop: wait_for_messages() \u2192 process each message \u2192 repeat\n"
    "- You NEVER exit. You NEVER stop calling wait_for_messages.\n"
    "- You are a stateless dispatcher. Anything >7 seconds goes to a background subagent.\n\n"
    "Read these files now to restore full context:\n"
    "1. ~/lobster-workspace/.claude/sys.dispatcher.bootup.md\n"
    "  \u2190 dispatcher instructions, main loop, 7-second rule\n"
    "2. ~/lobster-user-config/memory/canonical/handoff.md\n"
    "  \u2190 active projects, key people, priorities\n\n"
    "After reading: spawn the compact_catchup subagent to recover context from the\n"
    "last ~30 minutes (see sys.dispatcher.bootup.md \u2192 'Handling compact-reminder').\n"
    "Then resume your main loop by calling wait_for_messages()."
)

SENTINEL_FILE = Path(os.path.expanduser("~/messages/config/compact-pending"))

COMPACTION_TELEGRAM_MESSAGE = "\u267b\ufe0f Context compacted. Re-orienting..."

# Sidecar file for debug reflection prompts (Fix B, issue #1998).
# Written here instead of the inbox to avoid mark_processing/mark_processed overhead.
# The dispatcher reads this file at startup when LOBSTER_DEBUG=true.
BOOTUP_PROMPT_FILE = Path(
    os.environ.get(
        "LOBSTER_BOOTUP_PROMPT_FILE_OVERRIDE",
        os.path.expanduser("~/messages/bootup-prompt.md"),
    )
)


def _wfm_was_active() -> bool:
    """Return True if the dispatcher was recently active before this session started.

    Reads DISPATCHER_HEARTBEAT_FILE (dispatcher-heartbeat).  The file contains
    a single Unix epoch integer written by thinking-heartbeat.py (PostToolUse)
    and the WFM heartbeat thread in inbox_server.py.  If the file exists and
    the recorded timestamp is within DISPATCHER_WFM_RECENCY_SECONDS (15 min),
    the dispatcher was actively running immediately before this session — a
    strong signal that this is a compaction rather than a fresh start.

    A missing file, non-integer content, or a stale timestamp all return False
    (conservative: prefer false-negatives over false-positives for tier-3).

    Used as the third-tier fallback in _is_compact_event() when CC omits both
    source and hook_name from the SessionStart payload.
    """
    try:
        raw = DISPATCHER_HEARTBEAT_FILE.read_text().strip()
        if not raw.isdigit():
            return False
        last_ts = int(raw)
        age_seconds = int(time.time()) - last_ts
        return 0 <= age_seconds < DISPATCHER_WFM_RECENCY_SECONDS
    except OSError:
        return False


def _is_compact_event(data: dict) -> bool:
    """Return True if the hook input indicates a context compaction event.

    Layered detection (most-to-least reliable):

    1. ``source`` field equals "compact" (CC-documented primary field; present
       in most CC versions).  If source is present but non-compact, returns
       False immediately — hook_name and filesystem fallbacks are not used.

    2. ``hook_name`` field equals "compact" (observed in older CC versions).
       Only checked when source is absent from the payload.

    3. Filesystem fallback: when both source and hook_name are absent, check
       whether the dispatcher was actively blocking in wait_for_messages
       (DISPATCHER_HEARTBEAT_FILE contains a recent digit-only Unix timestamp).
       A live heartbeat signal with no payload fields strongly implies CC fired
       a compaction SessionStart without populating the usual identification
       fields.

    This function is the self-gate that replaces reliance on the
    matcher="compact" hook registration (which was found to be intermittently
    non-firing in Claude Code 2.1.x).  The hook is now registered with
    matcher="" (always fires) and uses this function to skip non-compact events.
    """
    source = data.get("source")
    if source is not None:
        # source field present — authoritative; do not fall through to hook_name.
        return source == "compact"

    hook_name = data.get("hook_name")
    if hook_name is not None:
        # source absent but hook_name present — use it.
        return hook_name == "compact"

    # Both source and hook_name absent — filesystem fallback.
    return _wfm_was_active()


def _log_compact_event(event_type: str, detail: str) -> None:
    """Append a structured log line to on-compact.log.

    Format: ISO UTC timestamp | event_type | detail
    event_type examples: "skipped_not_compact", "compact_detected",
                         "telegram_ok", "telegram_failed", "telegram_skipped"
    Silent on any failure \u2014 must never crash the hook.
    """
    try:
        COMPACT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"{ts} | {event_type} | {detail}\n"
        with COMPACT_LOG_FILE.open("a") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


def write_last_restart_reason(reason: str) -> None:
    """Write last-restart-reason.json with reason and ISO UTC timestamp.

    Called by on-compact.py with reason="compaction".
    Also called by health-check-v3.sh with reason="health-check" before
    triggering a systemd restart.

    Silent on any failure \u2014 must never crash the hook.
    """
    try:
        LAST_RESTART_REASON_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "reason": reason,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp_path = LAST_RESTART_REASON_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        tmp_path.replace(LAST_RESTART_REASON_FILE)
    except Exception:  # noqa: BLE001
        pass


def write_startup_cause() -> None:
    """Write last-startup-cause.json with cause="compaction" and ISO UTC timestamp.

    Called when _is_compact_event() returns True, immediately before the hook
    exits.  The next session's inject-bootup-context.py reads this file at
    startup and uses it to classify the session as post-compact vs fresh-start.

    inject-bootup-context.py resets the file to cause="restart" after reading
    it, so the file self-clears after one session.  This function only needs to
    write cause="compaction" — the "fresh/restart" default is handled by the
    reader.

    Uses an atomic tmp-rename to prevent a partial-write from corrupting the
    file between on-compact.py writing and inject-bootup-context.py reading.

    Silent on any failure — must never crash the hook.
    """
    try:
        STARTUP_CAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cause": "compaction",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp_path = STARTUP_CAUSE_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        tmp_path.replace(STARTUP_CAUSE_FILE)  # atomic on Linux (same filesystem)
    except Exception:  # noqa: BLE001
        pass


def already_pending() -> bool:
    """Return True if a compact-reminder message is already in inbox/ or processing/.

    Checks both directories so that a reminder being actively processed by the
    dispatcher (moved to processing/ by mark_processing) is not counted as absent,
    which would cause a duplicate to be written on a rapid second compaction.
    """
    for search_dir in (INBOX_DIR, PROCESSING_DIR):
        if not search_dir.exists():
            continue
        for path in search_dir.iterdir():
            if path.suffix != ".json":
                continue
            try:
                data = json.loads(path.read_text())
                if data.get("subtype") == "compact-reminder":
                    return True
            except (json.JSONDecodeError, OSError):
                continue
    return False


def write_reminder() -> None:
    """Write a compact-reminder system message to the inbox."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)

    # Use ts_ms=0 so the filename ("0_compact.json") sorts lexicographically
    # before any real user-message filename (which starts with the current epoch
    # in milliseconds, e.g. "1741234567890_...").  This guarantees the
    # compact-reminder is the first message the dispatcher sees after
    # compaction, regardless of how many user messages were queued beforehand.
    ts_ms = 0
    message_id = f"{ts_ms}_compact"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000"

    message = {
        "id": message_id,
        "source": "system",
        "chat_id": 0,
        "user_id": 0,
        "username": "lobster-system",
        "user_name": "System",
        "type": "text",
        "subtype": "compact-reminder",
        "text": REMINDER_TEXT,
        "timestamp": timestamp,
    }

    dest = INBOX_DIR / f"{message_id}.json"
    dest.write_text(json.dumps(message, indent=2) + "\n")


def write_sentinel() -> None:
    """
    Write the compact-pending sentinel file.

    The post-compact-gate.py PreToolUse hook checks for this file and blocks
    all tool calls until wait_for_messages() is called. This forces the
    dispatcher back into its main loop before doing anything else.

    Silent on any failure — must never crash the hook.
    """
    try:
        SENTINEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL_FILE.touch()
    except Exception:  # noqa: BLE001
        pass


def write_compaction_state() -> None:
    """
    Write last_compaction_ts to compaction-state.json.

    This timestamp is used by the compact_catchup subagent to determine the
    query window: it fetches messages since max(last_compaction_ts,
    last_restart_ts, last_catchup_ts) to avoid duplicating history across
    multiple rapid compaction or restart events.

    Silent on any failure — must never crash the hook.
    """
    try:
        COMPACTION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state: dict = {}
        if COMPACTION_STATE_FILE.exists():
            try:
                state = json.loads(COMPACTION_STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        state["last_compaction_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp_path = COMPACTION_STATE_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2) + "\n")
        tmp_path.replace(COMPACTION_STATE_FILE)  # atomic on Linux (same filesystem)
    except Exception:  # noqa: BLE001
        pass


def write_last_compact_ts() -> None:
    """
    Write the current Unix timestamp (integer seconds) to last-compact.ts.

    This simple timestamp file is read by health-check-v3.sh to determine
    whether a compaction occurred within the last 10 minutes.  If so, the
    health check skips its inbox staleness alert entirely, giving the dispatcher
    a grace period to re-read bootup files and drain the inbox backlog before
    any staleness alert fires.

    Silent on any failure -- must never crash the hook.
    """
    try:
        LAST_COMPACT_TS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = LAST_COMPACT_TS_FILE.with_suffix(".ts.tmp")
        tmp_path.write_text(str(int(time.time())) + "\n")
        tmp_path.replace(LAST_COMPACT_TS_FILE)  # atomic on Linux (same filesystem)
    except Exception:  # noqa: BLE001
        pass


def write_compacted_at() -> None:
    """
    Record the current UTC timestamp as compacted_at in lobster-state.json.

    Preserves the existing 'mode' field (and any other fields) so that the
    health check can still read lifecycle state correctly. Only adds or
    overwrites the compacted_at field.

    Silent on any failure — must never crash the hook.
    """
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state: dict = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        state["compacted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp_path = STATE_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2) + "\n")
        tmp_path.replace(STATE_FILE)  # atomic on Linux (same filesystem)
    except Exception:  # noqa: BLE001
        pass


def _parse_config_env() -> dict:
    """Parse key=value pairs from config.env, ignoring comments and blank lines."""
    config = {}
    if not CONFIG_ENV.exists():
        return config
    try:
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            # Strip optional surrounding quotes from the value.
            value = value.strip().strip('"').strip("'")
            config[key.strip()] = value
    except OSError:
        pass
    return config


def send_compaction_notify() -> None:
    """
    Notify the owner that a context compaction occurred by writing to the outbox.

    Writes a JSON file to ~/messages/outbox/ using the standard Lobster outbox
    format.  The outbox watcher (lobster_bot.py / outbox.py) picks up the file
    and delivers it via Telegram — identical to how all other Lobster system
    notifications are sent.

    This avoids calling the Telegram Bot API directly from the hook, keeping all
    outbound messages routed through the shared pipeline.

    Always fires when TELEGRAM_ALLOWED_USERS is configured — not gated on
    LOBSTER_DEBUG.  The health-check suppresses its own alerts during the
    compaction window so exactly one notification reaches the user per compaction.

    Silent on any failure — must never crash the hook.
    """
    try:
        config = _parse_config_env()
        allowed_users = config.get("TELEGRAM_ALLOWED_USERS", "").strip()
        if not allowed_users:
            return

        # Take the first user ID from a comma- or space-separated list.
        first_chat_id = allowed_users.replace(",", " ").split()[0]

        ts_ms = int(time.time() * 1000)
        reply_id = f"{ts_ms}_compact_notify_telegram"
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        reply = {
            "id": reply_id,
            "source": "telegram",
            "chat_id": first_chat_id,
            "text": COMPACTION_TELEGRAM_MESSAGE,
            "timestamp": timestamp,
        }

        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        dest = OUTBOX_DIR / f"{reply_id}.json"
        # Atomic write: write to .tmp then rename so the watcher never sees a
        # partial file (mirrors the pattern in src/utils/fs.py:atomic_write_json).
        tmp_dest = dest.with_suffix(".tmp")
        tmp_dest.write_text(json.dumps(reply, indent=2) + "\n")
        tmp_dest.replace(dest)

        print(
            f"[on-compact] compaction notify queued to outbox: {dest}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[on-compact] compaction notify failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _is_dispatcher_compact(data: dict) -> bool:
    """Return True if this compaction event belongs to the dispatcher session.

    Layered strategy:

    1. Primary: session_role.is_dispatcher() — works when the startup flag is
       present with a live PID. This is the normal path for fresh (non-compaction)
       restarts where inject-bootup-context.py hasn't yet consumed the flag.

    2. Fallback: LOBSTER_MAIN_SESSION=1.
       Context compaction assigns a NEW session_id to the post-compact session,
       and the startup flag has already been consumed by inject-bootup-context.py
       in the previous session. In that case, LOBSTER_MAIN_SESSION=1 (set by
       claude-persistent.sh for the dispatcher and inherited by its subagents)
       is the remaining signal.

       Edge case: a subagent that compacts will also have LOBSTER_MAIN_SESSION=1
       and may trigger this path.  In that rare case a false-positive
       compact-reminder would be written.  This is low-cost: the dispatcher will
       receive an extra compact-reminder in its inbox, which is harmless (it will
       re-orient and spawn catchup, then resume normally).  Subagent compactions
       are rare enough that this trade-off is acceptable.
    """
    if is_dispatcher(data):
        return True

    # Fallback: env var set by the dispatcher launcher (claude-persistent.sh).
    return os.environ.get("LOBSTER_MAIN_SESSION", "") == "1"


def _reflection_already_exists(msg_id: str) -> bool:
    """Return True if a reflection message with the given ID already exists.

    Scans inbox/, processing/, and processed/ to prevent concurrent hook
    invocations from writing duplicate reflection files with the same ID.
    Multiple subagent SessionStart events may fire within the same second,
    producing the same msg_id (1-second precision) but different filenames
    (millisecond-precision).  This check short-circuits all but the first
    invocation.

    Silent on all errors — must never crash the hook.
    """
    for directory in (INBOX_DIR, PROCESSING_DIR, PROCESSED_DIR):
        if not directory.exists():
            continue
        for f in directory.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("id") == msg_id:
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _schedule_reflection_prompt(trigger: str) -> None:
    """In debug mode, write a reflection prompt to the bootup-prompt sidecar file.

    When LOBSTER_DEBUG=true, writes a prompt asking the dispatcher to reflect on
    the bootup/compaction experience and file GitHub issues with observations.

    Writes to BOOTUP_PROMPT_FILE (~/messages/bootup-prompt.md) instead of the
    inbox (Fix B, issue #1998).  The dispatcher reads this file at startup
    when LOBSTER_DEBUG=true -- one Read call, no mark_processing/mark_processed
    round-trips.  The file is overwritten on the next restart, so it never
    accumulates stale entries.

    Silent on any failure -- must never crash the hook.
    """
    if os.environ.get("LOBSTER_DEBUG", "false").lower() != "true":
        return

    try:
        BOOTUP_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)

        content = (
            f"[Debug] {trigger.capitalize()} reflection prompt:\n\n"
            "How was the experience? Were there friction points, gaps, or improvements "
            "worth capturing?\n\n"
            "If you have observations: file or update GitHub issues in SiderealPress/lobster, "
            "or open PRs for straightforward fixes. Capture it while it's fresh.\n\n"
            f"trigger: {trigger}\n"
        )

        tmp_path = BOOTUP_PROMPT_FILE.with_suffix(".tmp")
        tmp_path.write_text(content)
        tmp_path.rename(BOOTUP_PROMPT_FILE)
        print(
            f"[on-compact] debug: wrote reflection prompt to {BOOTUP_PROMPT_FILE}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[on-compact] debug: failed to write reflection prompt: {exc}",
            file=sys.stderr,
        )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}

    # Self-gate: this hook is registered with matcher="" so it fires on every
    # SessionStart event.  Three-tier detection: source → hook_name → dispatcher-heartbeat.
    # Exit immediately for non-compact sessions.
    if not _is_compact_event(data):
        _log_compact_event(
            "skipped_not_compact",
            f"source={data.get('source', 'absent')!r} hook_name={data.get('hook_name', 'absent')!r} session_id={data.get('session_id', '')[:12]!r}",
        )
        sys.exit(0)

    session_id_snippet = data.get("session_id", "")[:12]
    _log_compact_event("compact_detected", f"session_id={session_id_snippet!r}")

    # Write cause=compaction BEFORE anything else.  inject-bootup-context.py reads
    # this file on the next startup: if cause==compaction and ts is within 5 minutes,
    # the startup is classified as a compaction-triggered restart rather than a plain
    # restart.  After reading, inject-bootup-context.py resets the file to
    # cause=restart, so subsequent startups default to restart unless this hook fires.
    # Runs for both dispatcher and subagent compactions (the classification only matters
    # for the dispatcher, but writing it early for all compactions is harmless).
    write_startup_cause()

    # Write restart-reason tracking file so the dispatcher can know this session
    # started due to a compaction (not a health-check restart).
    write_last_restart_reason("compaction")

    # Always record compaction timestamp — runs for both dispatcher and subagent
    # compactions.  The health check reads this to suppress false-positive
    # "stale inbox" restarts during any compaction pause window.
    write_compacted_at()

    # Write simple Unix timestamp for the 10-minute post-compaction grace period.
    # health-check-v3.sh reads this file and skips staleness alerts for 10 minutes
    # after a compaction, giving the dispatcher time to re-orient.
    write_last_compact_ts()

    # Always record last_compaction_ts for the catch-up subagent, regardless
    # of whether this is a dispatcher or subagent compaction.  The catch-up
    # subagent uses this to define its query window on next spawn.
    write_compaction_state()

    # Always send the Telegram notification for any compaction (dispatcher or
    # subagent).  This must fire when credentials are available.  The
    # health-check suppresses its own Telegram alerts during the compaction
    # window (COMPACTION_SUPPRESS_SECONDS), so exactly one notification reaches
    # the user per compaction event.
    send_compaction_notify()

    # Guard the inbox reminder and sentinel writes to the dispatcher only.
    # Subagent compactions must not inject compact-reminders into the shared
    # inbox or write the compact-pending sentinel, because those signals are
    # only meaningful to the dispatcher.
    #
    # Uses _is_dispatcher_compact() instead of is_dispatcher() directly because
    # CC assigns a NEW session_id after compaction — the hook input's session_id
    # won't match the stored marker file even for a dispatcher compaction.
    # _is_dispatcher_compact() adds a LOBSTER_MAIN_SESSION + stored-JSONL fallback
    # to handle this case and updates the marker file for subsequent calls.
    if not _is_dispatcher_compact(data):
        return

    if already_pending():
        # Sentinel still needs refreshing even if the inbox reminder is a dupe
        # (double compaction without intervening wait_for_messages). Touch resets
        # the TTL clock so the gate keeps blocking correctly.
        write_sentinel()
        return
    write_sentinel()
    try:
        write_reminder()
    except Exception:  # noqa: BLE001
        pass  # Reminder failure is non-fatal — sentinel is the critical artifact

    _schedule_reflection_prompt("compaction")


if __name__ == "__main__":
    main()
