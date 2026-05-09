#!/usr/bin/env python3
"""
Context-compaction hook for Lobster.

Fires on SessionStart — registered with matcher="" so it fires on every
session start.  The script self-gates using _is_compact_event(): it checks the
``source`` field in the CC SessionStart payload (primary) and ``hook_name`` as
a fallback, then exits immediately for non-compact events.

Injects a system message into the Lobster inbox so that the next call to
wait_for_messages() surfaces a reminder to re-read CLAUDE.md and re-orient
from handoff/memory context.

The script is idempotent: if a compact-reminder message already exists in
inbox/ or processing/ it skips writing a duplicate.

Compaction detection (self-gate):
  Primary:  data["source"] == "compact"  (CC-documented field)
  Fallback: data["hook_name"] == "compact"  (observed in some CC versions)
  If neither matches, the script exits immediately (sys.exit(0)).

Notification: always writes a compaction notification to ~/messages/outbox/
(the Lobster outbox pipeline) so the user is notified via Telegram.  The
outbox watcher picks up the file and delivers it to the correct transport.
This matches the architectural pattern used by all other Lobster notifications
and avoids direct Telegram API calls from the hook.

State: always writes compacted_at to lobster-state.json so that the health
check can suppress stale-inbox false-positives during the compaction pause.
Also writes last_compaction_ts to compaction-state.json so that the catch-up
subagent knows which window of history to recover after compaction.

Dispatcher-only: exits immediately for subagent sessions (detected via
_is_dispatcher_compact()).  Subagent compactions must not write compact-reminders
or the sentinel — those signals are only meaningful to the dispatcher.

Dispatcher detection: CC sets source='compact' on post-compact SessionStart
hooks.  Catchup subagents are plain SessionStart events without this signal —
they never carry source='compact'.  So source='compact' (combined with the
startup flag check for fresh starts) is sufficient to gate all dispatcher-only
writes.
"""

import json
import os
import sys
import time
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher


INBOX_DIR = Path(os.path.expanduser("~/messages/inbox"))
PROCESSING_DIR = Path(os.path.expanduser("~/messages/processing"))
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
# Single source of truth for startup cause detection.
# Written here (cause=compaction) before process exit.
# inject-bootup-context.py reads and resets it (cause=restart) on every startup.
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


def write_startup_cause() -> None:
    """
    Write {"cause": "compaction", "ts": "<iso_utc>"} to last-startup-cause.json.

    Called just before the process exits after a compaction.  inject-bootup-context.py
    reads this file on the next startup:
      - If cause == "compaction" and ts is within 5 minutes: startup was a compaction.
      - Otherwise: startup was a plain restart.
    After reading, inject-bootup-context.py resets the file to cause="restart" so
    subsequent startups default to restart unless this hook fires again.

    Uses an atomic rename so the file is never half-written.
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
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.%f", time.localtime()) + "+00:00"

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


def _is_compact_event(data: dict) -> bool:
    """Return True if the hook input indicates a context compaction event.

    Primary check: the ``source`` field in the CC SessionStart payload.
    Claude Code sets source="compact" for compaction-triggered sessions
    (other values: "startup", "resume", "clear").

    Fallback: hook_name == "compact" is undocumented but observed in some
    CC versions.  The fallback is only used when ``source`` is absent from
    the payload — if ``source`` is present but non-compact, the event is
    not a compaction regardless of hook_name.
    """
    source = data.get("source")
    if source is not None:
        return source == "compact"
    # source field absent — fall back to hook_name
    return data.get("hook_name") == "compact"


def _is_dispatcher_compact(data: dict) -> bool:
    """Return True only if this SessionStart is a real dispatcher compaction.

    CC sets source='compact' on post-compact SessionStart hooks.
    Catchup subagents and other subagents have plain SessionStart events
    without this signal, so they are correctly rejected.

    The startup flag check (is_dispatcher) handles fresh non-compact dispatcher
    starts where source='compact' would not be present.
    """
    # Fresh dispatcher start (non-compact): startup flag is still present.
    if is_dispatcher(data):
        return True

    # Post-compact dispatcher: CC sets source='compact' on the new session.
    # Catchup subagents do NOT carry this field — they are plain SessionStart events.
    return data.get("source") == "compact"


def _schedule_reflection_prompt(trigger: str) -> None:
    """In debug mode, write a reflection-prompt message to the inbox.

    When LOBSTER_DEBUG=true, drops a message asking the dispatcher to reflect
    on the bootup/compaction experience and file GitHub issues with observations.
    Written immediately — the dispatcher processes inbox messages in order so it
    will reach this after handling the compact-reminder and catching up.

    Silent on any failure — must never crash the hook.
    """
    if os.environ.get("LOBSTER_DEBUG", "false").lower() != "true":
        return

    try:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

        ts = time.time()
        msg_id = f"reflection_{trigger}_{int(ts)}"
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000"

        content = (
            f"[Debug] {trigger.capitalize()} reflection prompt:\n\n"
            "How was the experience? Were there friction points, gaps, or improvements "
            "worth capturing?\n\n"
            "If you have observations: file or update GitHub issues in SiderealPress/lobster, "
            "or open PRs for straightforward fixes. Capture it while it's fresh."
        )

        msg = {
            "id": msg_id,
            "source": "system",
            "chat_id": 0,
            "user_id": 0,
            "username": "lobster-system",
            "user_name": "System",
            "type": "reflection_prompt",
            "trigger": trigger,
            "text": content,
            "timestamp": timestamp,
        }

        # Use current epoch_ms so this sorts after the compact-reminder (ts_ms=0)
        # and after any queued user messages, but before future messages.
        ts_ms = int(ts * 1000)
        msg_path = INBOX_DIR / f"{ts_ms}_reflection_{trigger}.json"
        tmp_path = msg_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(msg, indent=2) + "\n")
        tmp_path.rename(msg_path)
        print(
            f"[on-compact] debug: wrote reflection prompt to {msg_path}",
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
    # SessionStart event.  Exit immediately for non-compact sessions.
    # Primary check: source="compact" (CC-documented).
    # Fallback: hook_name="compact" (observed in some CC versions).
    if not _is_compact_event(data):
        sys.exit(0)

    # Write cause=compaction BEFORE anything else.  inject-bootup-context.py reads
    # this file on the next startup: if cause==compaction and ts is within 5 minutes,
    # the startup is classified as a compaction-triggered restart rather than a plain
    # restart.  After reading, inject-bootup-context.py resets the file to
    # cause=restart, so subsequent startups default to restart unless this hook fires.
    # Runs for both dispatcher and subagent compactions (the classification only matters
    # for the dispatcher, but writing it early for all compactions is harmless).
    write_startup_cause()

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
    # inbox or write the compact-pending sentinel — those signals are only
    # meaningful to the dispatcher.
    #
    # _is_dispatcher_compact() checks: startup flag for fresh starts, then
    # source='compact' for post-compact sessions.  Catchup subagents are plain
    # SessionStart events and never carry source='compact', so they are rejected.
    if not _is_dispatcher_compact(data):
        sys.exit(0)

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
