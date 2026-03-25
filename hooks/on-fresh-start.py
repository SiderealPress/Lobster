#!/usr/bin/env python3
"""
SessionStart hook: on a fresh dispatcher restart, immediately mark all
"running" agent sessions as failed.

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

# If the compaction state file was written within this window, treat the
# current SessionStart as a compaction restart rather than a fresh restart.
COMPACTION_RECENCY_SECONDS = 60


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
    sys.exit(0)


if __name__ == "__main__":
    main()
