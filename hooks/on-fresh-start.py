#!/usr/bin/env python3
"""
SessionStart hook: on a fresh dispatcher restart, immediately mark all
"running" agent sessions as failed and ensure a compact-reminder is queued
if the catchup state is stale.

## When it fires

Registered under `SessionStart` with an empty matcher (fires on every
SessionStart). Filters itself to:

1. Sessions that inherit LOBSTER_MAIN_SESSION=1 (Lobster-managed sessions only).
2. The dispatcher session, not subagent sessions (detected via session_role).
3. Fresh restarts only — NOT context compaction events. Compaction is
   identified by checking whether ``on-compact.py`` recently updated
   ``compaction-state.json`` (within the last 60 seconds). On compaction,
   background subagents are still running; marking them failed would be wrong.

## Why this is needed

When Claude Code is killed and restarted (OOM, systemd restart, lobster stop),
every session in agent_sessions.db with status="running" is dead — the process
that owned them no longer exists. The normal reconciler applies the same
120-minute grace threshold regardless of whether a restart occurred, so dead
sessions linger for up to 2 hours before being garbage-collected.

A fresh restart is distinguishable from compaction because:
  - Fresh restart: new CC process, new session_id, previous dispatcher JSONL gone.
  - Compaction: same CC process, new session_id (compaction assigns a new one),
    but subagents are still alive in background Task() threads.

Running `agent-monitor.py --mark-failed` immediately at startup clears all
stale "running" sessions so monitoring is accurate from the moment the
dispatcher enters its main loop.

## Distinguishing restart from compact

On every compaction, ``on-compact.py`` atomically writes
``last_compaction_ts`` to ``~/lobster-workspace/data/compaction-state.json``.
We detect a compaction restart by checking whether that file was modified
within the last 60 seconds.  This is reliable regardless of whether Claude
Code populates ``hook_name`` in the SessionStart payload (it does not always
do so).  If the file is absent or older than 60 seconds, we treat the
SessionStart as a genuine fresh restart and run ``--mark-failed``.

## Stale-catchup compact-reminder injection

Issue #909: the dispatcher may exit after a compaction without spawning
compact-catchup (e.g. it reads the compact-reminder via check_inbox but then
exits before calling wait_for_messages again). On the next boot, the startup
protocol already always spawns compact-catchup, but only if the dispatcher
correctly follows the instructions.

To provide a code-level safety net: on fresh restart, if ``last_catchup_ts``
in compaction-state.json is more than STALE_CATCHUP_THRESHOLD_SECONDS old and
no compact-reminder is already queued in the inbox, this hook writes one.
This guarantees the dispatcher sees a compact-reminder in its WFM queue even
if the post-compaction compact-reminder was consumed without catchup running.

The injected message uses the same format as on-compact.py and sorts before
real user messages (ts_ms=1 rather than ts_ms=0, which is reserved for the
on-compact.py reminder).

## settings.json configuration

Add this to ~/.claude/settings.json under "hooks" → "SessionStart":

    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "python3 $HOME/lobster/hooks/on-fresh-start.py",
          "timeout": 30
        }
      ]
    }

Place this entry AFTER write-dispatcher-session-id.py so the marker file is
already written when this hook runs. (Both have empty matchers and will fire
on every SessionStart; ordering matters only for the marker file dependency.)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Allow imports from the hooks directory (session_role).
sys.path.insert(0, str(Path(__file__).parent))

import session_role  # noqa: E402 — path insert must precede this

AGENT_MONITOR = Path(os.path.expanduser("~/lobster/scripts/agent-monitor.py"))

# on-compact.py writes last_compaction_ts to this file on every compaction.
COMPACTION_STATE_FILE = Path(
    os.environ.get(
        "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/data/compaction-state.json"),
    )
)

INBOX_DIR = Path(os.path.expanduser("~/messages/inbox"))
PROCESSING_DIR = Path(os.path.expanduser("~/messages/processing"))

# If the compaction state file was written within this window, treat the
# current SessionStart as a compaction restart rather than a fresh restart.
COMPACTION_RECENCY_SECONDS = 60

# If last_catchup_ts is older than this, inject a compact-reminder on startup
# so the dispatcher is forced to run compact-catchup via its WFM handler.
# This is the code-level safety net for issue #909.
STALE_CATCHUP_THRESHOLD_SECONDS = 30 * 60  # 30 minutes

STARTUP_COMPACT_REMINDER_TEXT = (
    "COMPACT REMINDER \u2014 RE-ORIENT NOW (injected by on-fresh-start.py)\n\n"
    "A previous session ended without completing compact-catchup. "
    "Your catchup state is stale.\n\n"
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
    "last session (see sys.dispatcher.bootup.md \u2192 'Handling compact-reminder').\n"
    "Then resume your main loop by calling wait_for_messages(timeout=1800, hibernate_on_timeout=True)."
)


def _is_compact_event(data: dict) -> bool:  # noqa: ARG001 — data unused; kept for API compat
    """Return True if the hook input indicates a context compaction event.

    Rather than relying on a ``hook_name`` field in the SessionStart payload
    (which Claude Code does not reliably populate), we check whether
    ``on-compact.py`` recently updated the compaction state file.  That file is
    written atomically by the companion hook on every compaction, so its mtime
    is the authoritative signal.

    If the compaction state file was modified within COMPACTION_RECENCY_SECONDS
    (60 s), we treat this SessionStart as a compaction restart — subagents are
    still alive, so ``--mark-failed`` must not run.

    Falls back to False (treat as fresh start) when the file is absent or
    unreadable, which is the safe default — running --mark-failed on a genuine
    fresh start is correct and harmless.
    """
    try:
        mtime = COMPACTION_STATE_FILE.stat().st_mtime
        age_seconds = time.time() - mtime
        return age_seconds <= COMPACTION_RECENCY_SECONDS
    except OSError:
        # File absent or unreadable — no recent compaction, treat as fresh start.
        return False


def _is_catchup_stale() -> bool:
    """Return True if last_catchup_ts in compaction-state.json is older than
    STALE_CATCHUP_THRESHOLD_SECONDS, or if the field is absent.

    When stale, the dispatcher may be starting up without having run
    compact-catchup after the last compaction — a safety-net compact-reminder
    should be injected into the inbox.
    """
    try:
        data = json.loads(COMPACTION_STATE_FILE.read_text())
        ts_str = data.get("last_catchup_ts")
        if not ts_str:
            return True
        # Parse ISO 8601 UTC timestamp (Z suffix).
        ts_str_clean = ts_str.rstrip("Z").replace("+00:00", "")
        import datetime
        ts = datetime.datetime.fromisoformat(ts_str_clean).replace(
            tzinfo=datetime.timezone.utc
        )
        age_seconds = time.time() - ts.timestamp()
        return age_seconds > STALE_CATCHUP_THRESHOLD_SECONDS
    except (OSError, KeyError, ValueError, AttributeError):
        # File absent, unreadable, or field missing — treat as stale.
        return True


def _compact_reminder_already_queued() -> bool:
    """Return True if a compact-reminder message is already in inbox/ or processing/.

    Checks both directories so that a reminder being actively processed by the
    dispatcher (moved to processing/ by mark_processing) is not counted as absent,
    which would cause a duplicate to be written on startup.
    """
    for search_dir in (INBOX_DIR, PROCESSING_DIR):
        try:
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
        except OSError:
            continue
    return False


def _inject_compact_reminder() -> None:
    """Write a startup-injected compact-reminder into the inbox.

    Uses ts_ms=1 so it sorts after the on-compact.py reminder (ts_ms=0) but
    before any real user message (ts_ms = current epoch milliseconds).
    Idempotent: skips if a compact-reminder is already queued.
    Silent on any failure — must not crash the hook.
    """
    if _compact_reminder_already_queued():
        print(
            "[on-fresh-start] compact-reminder already queued — skipping injection",
            file=sys.stderr,
        )
        return

    try:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        # Use ts_ms=0 so the filename sorts before any real user message
        # (same convention as on-compact.py's "0_compact.json").
        # A distinct message_id avoids clobbering the on-compact.py reminder
        # if both happen to coexist.
        ts_ms = 0
        message_id = f"{ts_ms}_startup_compact"
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
            "text": STARTUP_COMPACT_REMINDER_TEXT,
            "timestamp": timestamp,
        }

        dest = INBOX_DIR / f"{message_id}.json"
        dest.write_text(json.dumps(message, indent=2) + "\n")
        print(
            f"[on-fresh-start] injected stale-catchup compact-reminder: {dest}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[on-fresh-start] failed to inject compact-reminder: {exc}",
            file=sys.stderr,
        )


def _schedule_reflection_prompt(trigger: str) -> None:
    """In debug mode, write a reflection-prompt message to the inbox.

    When LOBSTER_DEBUG=true, drops a message asking the dispatcher to reflect
    on the bootup/compaction experience and file GitHub issues with observations.
    Written immediately — the dispatcher processes inbox messages in order so it
    will reach this after the compact-reminder and catchup handling.

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

        # Use current epoch_ms so this sorts after the startup compact-reminder
        # (ts_ms=0/1) and after any queued user messages.
        ts_ms = int(ts * 1000)
        msg_path = INBOX_DIR / f"{ts_ms}_reflection_{trigger}.json"
        tmp_path = msg_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(msg, indent=2) + "\n")
        tmp_path.rename(msg_path)
        print(
            f"[on-fresh-start] debug: wrote reflection prompt to {msg_path}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[on-fresh-start] debug: failed to write reflection prompt: {exc}",
            file=sys.stderr,
        )


def _mark_all_running_failed() -> None:
    """Run agent-monitor.py --mark-failed via uv.

    Uses subprocess so this works regardless of the current Python environment.
    Logs to stderr on failure but never raises — must not crash the hook or
    block the dispatcher from starting.
    """
    try:
        result = subprocess.run(
            ["uv", "run", str(AGENT_MONITOR), "--mark-failed"],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if result.returncode != 0:
            # Non-zero exit is expected when no sessions are found (exit 0 =
            # none stale; exit 1 = some stale but already marked). Either is
            # fine — log stderr only if there's meaningful output.
            if result.stderr.strip():
                print(
                    f"[on-fresh-start] agent-monitor --mark-failed stderr:\n{result.stderr.strip()}",
                    file=sys.stderr,
                )
        if result.stdout.strip():
            print(
                f"[on-fresh-start] agent-monitor --mark-failed output:\n{result.stdout.strip()}",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print(
            "[on-fresh-start] agent-monitor --mark-failed timed out after 25s",
            file=sys.stderr,
        )
    except FileNotFoundError as exc:
        print(
            f"[on-fresh-start] could not run agent-monitor: {exc}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[on-fresh-start] unexpected error running agent-monitor: {exc}",
            file=sys.stderr,
        )


def main() -> None:
    # Only fire for Lobster-managed sessions.
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}

    # Skip compaction events — subagents are still alive on compaction.
    if _is_compact_event(data):
        sys.exit(0)

    # Skip subagent sessions — only the dispatcher should run this.
    if not session_role.is_dispatcher(data):
        sys.exit(0)

    if not AGENT_MONITOR.exists():
        print(
            f"[on-fresh-start] agent-monitor not found at {AGENT_MONITOR}; skipping",
            file=sys.stderr,
        )
        sys.exit(0)

    _mark_all_running_failed()

    # Safety net for issue #909: if catchup state is stale (last_catchup_ts is
    # > 30 min old or absent), inject a compact-reminder into the inbox. This
    # guarantees the dispatcher will process a compact-reminder via
    # wait_for_messages — even if a previous session consumed the original
    # compact-reminder without running compact-catchup and then exited.
    if _is_catchup_stale():
        _inject_compact_reminder()

    _schedule_reflection_prompt("bootup")

    sys.exit(0)


if __name__ == "__main__":
    main()
