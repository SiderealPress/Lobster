#!/usr/bin/env python3
"""
SessionStop hook: write DEAD state for dispatcher and fallback context-handoff.json.

Responsibilities:
1. Write DEAD state to dispatcher-state.json (issue #1918 — 5-state liveness machine)
   so the health check can immediately restart.
2. Write a lightweight context-handoff.json when the dispatcher session ends without
   a graceful wind-down (issue #1977).

## context-handoff.json fallback (issue #1977)

The graceful wind-down path (context_warning handler in sys.dispatcher.bootup.md,
step 5) writes context-handoff.json with rich data: pending_tasks, last_user_message,
etc. But sessions that hit the hard context limit before reaching 70%, or that crash,
bypass this entirely — the LLM never gets to act.

This hook is the safety net: if context-handoff.json does not already exist when the
Stop hook fires, we write a minimal version so the next session always has at least:
  - triggered_at: current UTC ISO timestamp
  - context_pct: last known context % from the session transcript (None if unavailable)
  - in_flight_agents: list of running tasks from inflight-work.jsonl without completion
  - note: "Stop hook wind-down"

If context-handoff.json already exists (written by the graceful LLM wind-down), we do
NOT overwrite it — the graceful version has richer data. This hook is strictly a
fallback.

## Dispatcher detection

Uses both is_dispatcher() and is_dispatcher_session() from session_role.py, combined
with `or`. This is necessary because:

1. is_dispatcher() checks the startup flag file written by the launcher. That flag is
   consumed (deleted) by inject-bootup-context.py during SessionStart — so by the time
   SessionStop fires, the flag is gone and is_dispatcher() returns False for the actual
   dispatcher session.
2. is_dispatcher_session() provides the fallback: it uses state files and a process-tree
   walk, which remain valid throughout the session lifetime.

The combination `is_dispatcher() or is_dispatcher_session()` is therefore correct for
SessionStop hooks: is_dispatcher() handles the rare case where the flag was not yet
consumed (e.g., very short sessions), and is_dispatcher_session() covers the normal case.

Silent on all errors — must never block session stop.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(_HOOKS_DIR))

import session_role  # noqa: E402

_LOBSTER_DIR = _HOOKS_DIR.parent
sys.path.insert(0, str(_LOBSTER_DIR / "src"))
import state_machine  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HANDOFF_NOTE = "Stop hook wind-down"

# Known model context window sizes (same table as context-monitor.py).
# Matched by prefix so versioned IDs resolve correctly.
_MODEL_CONTEXT_SIZES: list[tuple[str, int]] = [
    ("claude-sonnet-4-6", 200_000),
    ("claude-opus-4-6", 200_000),
    ("claude-haiku-4-5", 200_000),
]
_DEFAULT_CONTEXT_SIZE = 200_000


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _model_max_context(model: str) -> int:
    """Return the max context window size for a known model ID.

    Matches by prefix to handle versioned IDs. Falls back to
    _DEFAULT_CONTEXT_SIZE for unknown models.
    """
    for prefix, size in _MODEL_CONTEXT_SIZES:
        if model.startswith(prefix):
            return size
    return _DEFAULT_CONTEXT_SIZE


def _read_context_pct_from_transcript(transcript_path: str | None) -> float | None:
    """Return the last known context usage percentage from the session transcript.

    Reads the transcript JSONL file line-by-line and returns the usage % from
    the last assistant turn that contains a usage block. Returns None if the
    transcript is unavailable or contains no usage data.

    This reuses the same logic as context-monitor.py's _read_transcript_usage
    but is a pure standalone function to avoid coupling.
    """
    if not transcript_path:
        return None

    path = Path(transcript_path)
    if not path.exists():
        return None

    last_usage: dict | None = None
    last_model: str = "unknown"

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if msg.get("role") == "assistant" and "usage" in msg:
                        last_usage = msg["usage"]
                        last_model = msg.get("model", "unknown")
    except OSError:
        return None

    if last_usage is None:
        return None

    input_tokens = last_usage.get("input_tokens", 0) or 0
    cache_create = last_usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = last_usage.get("cache_read_input_tokens", 0) or 0
    total_used = input_tokens + cache_create + cache_read

    model_max = _model_max_context(last_model)
    return (total_used / model_max) * 100.0


def _read_in_flight_agents(inflight_path: Path) -> list[dict]:
    """Return a list of in-flight agents from inflight-work.jsonl.

    An agent is in-flight if its last status entry is "running" (not "done").
    The log is append-only, so entries are processed in order:
    - "running" entry: marks the task as in-flight (removes from done_ids to
      handle retries — a new "running" after a "done" means the task was retried
      with the same task_id and the retry is in-flight).
    - "done" entry: marks the task as completed (removes from running dict).

    The final state is determined by whichever of "running" or "done" appeared
    last for each task_id.

    Returns an empty list if the file is absent, unreadable, or has no in-flight
    entries. Silent on all errors.
    """
    if not inflight_path.exists():
        return []

    try:
        running: dict[str, dict] = {}
        done_ids: set[str] = set()

        with open(inflight_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                task_id = entry.get("task_id")
                if not task_id:
                    continue

                status = entry.get("status", "")
                if status == "done":
                    done_ids.add(task_id)
                    running.pop(task_id, None)
                elif status == "running":
                    # Remove from done_ids: a new "running" entry after a "done"
                    # means the task was retried with the same task_id. The retry
                    # is in-flight until its own "done" entry appears.
                    done_ids.discard(task_id)
                    running[task_id] = entry

        # Return only tasks still in running state (not completed).
        return list(running.values())
    except Exception:  # noqa: BLE001
        return []


def _resolve_inflight_path() -> Path:
    """Return the inflight-work.jsonl path, resolved from LOBSTER_WORKSPACE."""
    workspace = Path(
        os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
    )
    return workspace / "data" / "inflight-work.jsonl"


def _build_handoff_payload(context_pct: float | None, in_flight_agents: list[dict]) -> dict:
    """Return the minimal context-handoff.json payload for a Stop hook wind-down."""
    return {
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "context_pct": context_pct,
        "in_flight_agents": in_flight_agents,
        "note": _HANDOFF_NOTE,
    }


def _write_handoff_if_absent(handoff_path: Path, payload: dict) -> None:
    """Write context-handoff.json atomically if it does not already exist.

    Uses a tmp-file + os.replace() for atomic write. Does not overwrite an
    existing file — the graceful wind-down version has richer data.

    Silent on all errors.
    """
    if handoff_path.exists():
        return  # Graceful wind-down already wrote this — preserve it.

    try:
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = handoff_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(str(tmp_path), str(handoff_path))
    except Exception:  # noqa: BLE001
        pass  # Never interrupt session stop


def _resolve_handoff_path() -> Path:
    """Return the context-handoff.json path, resolved from LOBSTER_WORKSPACE."""
    workspace = Path(
        os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
    )
    return workspace / "data" / "context-handoff.json"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, EOFError):
        hook_input = {}

    # For SessionStop, use is_dispatcher() which checks the startup flag file.
    # Note: by SessionStop, the flag may already have been consumed by SessionStart.
    # Fall back to is_dispatcher_session() as a secondary check.
    is_disp = session_role.is_dispatcher(hook_input) or session_role.is_dispatcher_session(hook_input)
    if not is_disp:
        sys.exit(0)

    session_id = hook_input.get("session_id", "")

    # --- 1. Write DEAD state (existing behaviour, issue #1918) ---
    try:
        state_machine.write_state(state_machine.DEAD, session_id=session_id)
    except Exception:
        pass

    # --- 2. Write fallback context-handoff.json (issue #1977) ---
    try:
        transcript_path = hook_input.get("transcript_path")
        context_pct = _read_context_pct_from_transcript(transcript_path)
        in_flight_agents = _read_in_flight_agents(_resolve_inflight_path())
        payload = _build_handoff_payload(context_pct, in_flight_agents)
        handoff_path = _resolve_handoff_path()
        _write_handoff_if_absent(handoff_path, payload)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
