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

Place this entry AFTER inject-bootup-context.py so the startup flag is
already consumed when this hook runs. (Both have empty matchers and will fire
on every SessionStart; ordering matters only for the startup flag dependency.)
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
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
PROCESSED_DIR = Path(os.path.expanduser("~/messages/processed"))

# inflight-work.jsonl path — append-only log of subagent spawns and completions.
# On startup, stale "running" entries with no corresponding "done" are compacted out.
# Override via env var for testability.
INFLIGHT_WORK_FILE = Path(
    os.environ.get(
        "LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE",
        os.path.expanduser("~/lobster-workspace/data/inflight-work.jsonl"),
    )
)

# Pointer to the current session file (written by compact-catchup / session management).
# If this file exists and was modified recently, there is a prior session worth catching up.
CURRENT_SESSION_FILE_POINTER = Path(
    os.environ.get(
        "LOBSTER_CURRENT_SESSION_FILE_OVERRIDE",
        "/tmp/lobster-current-session-file",
    )
)

# agent_sessions.db path — same resolution as claims.py in the MCP server.
_MESSAGES_DIR = Path(
    os.environ.get("LOBSTER_MESSAGES", os.path.expanduser("~/messages"))
)
AGENT_SESSIONS_DB = _MESSAGES_DIR / "config" / "agent_sessions.db"

# If the compaction state file was written within this window, treat the
# current SessionStart as a compaction restart rather than a fresh restart.
COMPACTION_RECENCY_SECONDS = 60

# If last_catchup_ts is older than this, inject a compact-reminder on startup
# so the dispatcher is forced to run compact-catchup via its WFM handler.
# This is the code-level safety net for issue #909.
STALE_CATCHUP_THRESHOLD_SECONDS = 30 * 60  # 30 minutes

# If a session file exists and was modified within this window, inject a
# compact-reminder even if last_catchup_ts appears recent. This handles the
# case where the dispatcher was restarted while actively working — there may
# be in-flight activity the new session should recover even though catchup
# ran successfully at the start of the previous session.
SESSION_FILE_RECENCY_SECONDS = 4 * 60 * 60  # 4 hours

# "running" entries in inflight-work.jsonl older than this with no matching
# "done" entry are dropped on startup compaction (issue #1997).
INFLIGHT_STALE_THRESHOLD_HOURS = 6

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
    "Then resume your main loop by calling wait_for_messages()."
)


def _compact_inflight_entries(
    entries: list[dict],
    now: datetime,
) -> list[dict]:
    """Return entries with stale orphaned 'running' entries removed.

    An entry is dropped when ALL of the following hold:
    - status == "running"
    - No entry with the same task_id has status == "done"
    - The entry's timestamp ("ts" field) is older than INFLIGHT_STALE_THRESHOLD_HOURS,
      or the "ts" field is absent/unparseable (treated conservatively as stale)

    All other entries (done entries, fresh running entries, running entries that
    have a matching done entry) are preserved unchanged.

    Pure function — no I/O, no side effects. Input is not mutated.
    """
    threshold_seconds = INFLIGHT_STALE_THRESHOLD_HOURS * 3600

    # Build the set of task_ids that have at least one 'done' entry.
    done_task_ids: set[str] = {
        e["task_id"]
        for e in entries
        if e.get("status") == "done" and "task_id" in e
    }

    result: list[dict] = []
    for entry in entries:
        # Non-running entries (done, etc.) are always preserved.
        if entry.get("status") != "running":
            result.append(dict(entry))
            continue

        task_id = entry.get("task_id")

        # Running entries with a matching done are preserved regardless of age.
        if task_id in done_task_ids:
            result.append(dict(entry))
            continue

        # Running entry with no matching done: check age.
        # Prefer "ts" (legacy field) but fall back to "started_at" (current field).
        ts_str = entry.get("ts") or entry.get("started_at")
        if not ts_str:
            # No timestamp → cannot determine age → treat as stale, drop.
            print(
                f"[on-fresh-start] dropping stale inflight entry (no ts): task_id={task_id!r}",
                file=sys.stderr,
            )
            continue

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_seconds = (now - ts).total_seconds()
        except (ValueError, AttributeError):
            # Unparseable timestamp → treat as stale, drop.
            print(
                f"[on-fresh-start] dropping stale inflight entry (unparseable ts {ts_str!r}): "
                f"task_id={task_id!r}",
                file=sys.stderr,
            )
            continue

        if age_seconds > threshold_seconds:
            print(
                f"[on-fresh-start] dropping stale inflight entry "
                f"({age_seconds / 3600:.1f}h old, threshold {INFLIGHT_STALE_THRESHOLD_HOURS}h): "
                f"task_id={task_id!r}",
                file=sys.stderr,
            )
            continue

        # Running entry within threshold — preserve.
        result.append(dict(entry))

    return result


def _compact_inflight_work() -> None:
    """Compact inflight-work.jsonl on startup, dropping stale orphaned 'running' entries.

    Reads the JSONL file, removes 'running' entries older than
    INFLIGHT_STALE_THRESHOLD_HOURS (6h) that have no corresponding 'done' entry,
    then rewrites the file atomically.

    This prevents months-old 'running' entries from causing false-positive
    "lost subagent" alerts on every startup (issue #1997).

    Silent on all errors — must not crash the hook or block dispatcher start.
    """
    if not INFLIGHT_WORK_FILE.exists():
        return

    try:
        now = datetime.now(timezone.utc)

        # Parse all entries, skipping malformed lines.
        entries: list[dict] = []
        malformed_count = 0
        for line in INFLIGHT_WORK_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                malformed_count += 1
                print(
                    f"[on-fresh-start] skipping malformed inflight-work line: {line[:80]}",
                    file=sys.stderr,
                )

        compacted = _compact_inflight_entries(entries, now)
        dropped = len(entries) - len(compacted)

        if dropped == 0 and malformed_count == 0:
            # Nothing to do — avoid touching the file unnecessarily.
            return

        # Rewrite the file when there are dropped entries or malformed lines.
        # Malformed lines are intentionally excluded from the rewrite: they cannot
        # be parsed, so they carry no meaningful state — keeping them would
        # silently corrupt the JSONL and trigger spurious errors on future reads.

        # Atomic rewrite: write to a temp file in the same directory, then rename.
        dir_ = str(INFLIGHT_WORK_FILE.parent)
        content = "\n".join(json.dumps(e, ensure_ascii=False) for e in compacted)
        if content:
            content += "\n"

        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".inflight-compact-", suffix=".jsonl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(INFLIGHT_WORK_FILE))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        print(
            f"[on-fresh-start] compacted inflight-work.jsonl: "
            f"dropped {dropped} stale orphaned running entr{'y' if dropped == 1 else 'ies'}, "
            f"{len(compacted)} entries remain",
            file=sys.stderr,
        )

    except Exception as exc:  # noqa: BLE001
        print(
            f"[on-fresh-start] failed to compact inflight-work.jsonl: {exc}",
            file=sys.stderr,
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


def _has_recent_session_file() -> bool:
    """Return True if /tmp/lobster-current-session-file points to a session file
    that was modified within SESSION_FILE_RECENCY_SECONDS.

    This catches the case where the dispatcher was restarted mid-session while
    actively working. Even if last_catchup_ts is recent, there may be new
    activity in the session file that the fresh session should catch up on.
    """
    try:
        if not CURRENT_SESSION_FILE_POINTER.exists():
            return False
        session_path_str = CURRENT_SESSION_FILE_POINTER.read_text().strip()
        if not session_path_str:
            return False
        session_path = Path(session_path_str)
        if not session_path.exists():
            return False
        age_seconds = time.time() - session_path.stat().st_mtime
        return age_seconds <= SESSION_FILE_RECENCY_SECONDS
    except OSError:
        return False


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


def _clear_stale_claim(message_id: str) -> None:
    """Delete any stale message_claims row for message_id.

    When on-fresh-start.py re-injects a deterministic system message (e.g.
    0_startup_compact), the new dispatcher must be able to call mark_processing
    on it.  The claim table uses INSERT OR FAIL on a UNIQUE PRIMARY KEY, so any
    leftover row from a previous session — regardless of its status — will cause
    mark_processing to return already_claimed.

    This function removes the row unconditionally before injection so the new
    dispatcher can claim the message cleanly.  It is idempotent: a missing row
    or absent DB is silently ignored.

    Operates directly on SQLite (no MCP dependency) because this hook runs
    before the MCP server is connected.
    """
    if not AGENT_SESSIONS_DB.exists():
        return
    try:
        conn = sqlite3.connect(str(AGENT_SESSIONS_DB))
        try:
            conn.execute(
                "DELETE FROM message_claims WHERE message_id=?",
                (message_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[on-fresh-start] failed to clear stale claim for {message_id!r}: {exc}",
            file=sys.stderr,
        )


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

        # Clear any stale message_claims row so the new dispatcher can claim
        # this message via mark_processing.  The claims table uses INSERT OR FAIL
        # on a UNIQUE PRIMARY KEY — a row left from a previous session (even with
        # status='processed') will cause already_claimed on the next startup.
        # See issue #1398.
        _clear_stale_claim(message_id)

        # Also remove any stale processing/ file for this message_id.  The MCP
        # server moves the file from inbox/ to processing/ when mark_processing
        # is called, and only removes it on mark_processed/mark_failed.  A
        # crashed or compacted session may leave the file in processing/ with no
        # active dispatcher to clear it, causing the next startup's mark_processing
        # call to fail because the file already exists at the destination path.
        stale_processing_file = PROCESSING_DIR / f"{message_id}.json"
        if stale_processing_file.exists():
            stale_processing_file.unlink()
            print(
                f"[on-fresh-start] removed stale processing file: {stale_processing_file}",
                file=sys.stderr,
            )

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
    """In debug mode, write a reflection-prompt message to the inbox.

    When LOBSTER_DEBUG=true, drops a message asking the dispatcher to reflect
    on the bootup/compaction experience and file GitHub issues with observations.
    Written immediately — the dispatcher processes inbox messages in order so it
    will reach this after the compact-reminder and catchup handling.

    Dedup: computes the msg_id first and checks inbox/, processing/, and
    processed/ for an existing file with the same ID.  Concurrent hook
    invocations within the same second will share the same ID (1-second
    precision) but write different filenames (millisecond-precision).  The
    first invocation wins; subsequent ones skip silently.

    Silent on any failure — must never crash the hook.
    """
    if os.environ.get("LOBSTER_DEBUG", "false").lower() != "true":
        return

    try:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

        ts = time.time()
        msg_id = f"reflection_{trigger}_{int(ts)}"

        # Dedup: skip if a reflection with this ID already exists.
        if _reflection_already_exists(msg_id):
            print(
                f"[on-fresh-start] debug: reflection prompt {msg_id!r} already exists — skipping",
                file=sys.stderr,
            )
            return

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

    # Compact inflight-work.jsonl: drop stale orphaned 'running' entries so
    # compact-catchup does not report false-positive "lost subagent" alerts for
    # sessions that crashed months ago (issue #1997).
    _compact_inflight_work()

    _mark_all_running_failed()

    # Safety net for issue #909: if catchup state is stale (last_catchup_ts is
    # > 30 min old or absent), inject a compact-reminder into the inbox. This
    # guarantees the dispatcher will process a compact-reminder via
    # wait_for_messages — even if a previous session consumed the original
    # compact-reminder without running compact-catchup and then exited.
    #
    # Also inject when a recent session file exists (< 4h old), even if
    # last_catchup_ts is recent. A mid-session restart can leave in-flight
    # activity in the session file that the new session needs to recover,
    # regardless of when catchup last ran.
    if _is_catchup_stale() or _has_recent_session_file():
        _inject_compact_reminder()

    _schedule_reflection_prompt("bootup")

    sys.exit(0)


if __name__ == "__main__":
    main()
