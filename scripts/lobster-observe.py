#!/usr/bin/env python3
"""lobster-observe.py — Write a system observation to the Lobster inbox.

This helper lets bash scripts (e.g. dispatch-job.sh) emit structured
observations via the inbox API without needing direct MCP access.  It
produces the exact same payload format as `handle_write_observation` in
inbox_server.py so the dispatcher handles it identically.

For `system_error` observations, this script also appends a line directly to
``~/lobster-workspace/logs/observations.log`` as a durability fallback — the
same fallback the MCP server performs.  If the dispatcher is down when the
cron job fires, the alert still lands in the ops log.  The ``source`` field
in the log entry is set to ``"cron-direct"`` to distinguish these entries
from MCP-written ones.  Worst case: two log entries for the same event
(acceptable — no deduplication is needed).

Usage:
    uv run scripts/lobster-observe.py \\
        --category system_error \\
        --text "Job 'foo' was auto-disabled because its task file is missing."

Environment variables:
    LOBSTER_MESSAGES    — parent of the inbox/ dir (default: ~/messages)
    LOBSTER_WORKSPACE   — workspace root used to locate logs/ (default: ~/lobster-workspace)

Exit codes:
    0 — observation written successfully
    1 — argument error
    2 — I/O error writing to inbox
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

VALID_CATEGORIES = frozenset(["user_context", "system_context", "system_error"])


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def build_observation_payload(
    text: str,
    category: str,
    chat_id: int,
    source: str,
    task_id: str | None = None,
) -> dict:
    """Return an observation message dict matching inbox_server's format (pure)."""
    now = datetime.now(timezone.utc)
    ts_ms = int(now.timestamp() * 1000)
    message_id = f"{ts_ms}_observation_{uuid.uuid4().hex[:8]}"

    payload: dict = {
        "id": message_id,
        "type": "subagent_observation",
        "source": source,
        "chat_id": chat_id,
        "text": text,
        "category": category,
        "timestamp": now.isoformat(),
    }
    if task_id:
        payload["task_id"] = task_id
    return payload


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------


def write_observation_to_inbox(inbox_dir: Path, payload: dict) -> None:
    """Atomically write the observation payload to the inbox (side effect)."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = inbox_dir / f"{payload['id']}.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(dest)


def resolve_inbox_dir() -> Path:
    """Return the inbox directory from env or default path (pure-ish)."""
    messages = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    return Path(messages) / "inbox"


def resolve_obs_log() -> Path:
    """Return the observations.log path from env or default workspace (pure-ish)."""
    workspace = os.environ.get(
        "LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace")
    )
    return Path(workspace) / "logs" / "observations.log"


def append_to_obs_log(obs_log: Path, payload: dict) -> None:
    """Append a JSON line to observations.log as a durability fallback (side effect).

    Mirrors the belt-and-suspenders write the MCP server performs in
    ``handle_write_observation``.  The ``source`` field is set to
    ``"cron-direct"`` so log readers can distinguish these entries from
    MCP-written ones.  Only called for ``system_error`` observations.
    """
    obs_log.parent.mkdir(parents=True, exist_ok=True)
    log_entry: dict = {
        "ts": payload["timestamp"],
        "category": payload["category"],
        "content": payload["text"],
        "source": "cron-direct",
    }
    if "task_id" in payload:
        log_entry["task_id"] = payload["task_id"]
    with obs_log.open("a") as f:
        f.write(json.dumps(log_entry) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write a system observation to the Lobster inbox.",
    )
    parser.add_argument(
        "--category",
        required=True,
        choices=sorted(VALID_CATEGORIES),
        help="Observation category: system_error | system_context | user_context",
    )
    parser.add_argument(
        "--text",
        required=True,
        help="Observation text.",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=0,
        help="Target chat_id (default: 0, meaning dispatcher resolves the principal).",
    )
    parser.add_argument(
        "--source",
        default="telegram",
        help="Message source tag (default: telegram).",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="Optional task_id for correlation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    inbox_dir = resolve_inbox_dir()
    payload = build_observation_payload(
        text=args.text,
        category=args.category,
        chat_id=args.chat_id,
        source=args.source,
        task_id=args.task_id,
    )

    try:
        write_observation_to_inbox(inbox_dir, payload)
    except OSError as exc:
        print(f"Error writing observation to inbox: {exc}", file=sys.stderr)
        return 2

    # Durability fallback: for system_error, also append directly to
    # observations.log in case the dispatcher is down when this fires.
    if args.category == "system_error":
        obs_log = resolve_obs_log()
        try:
            append_to_obs_log(obs_log, payload)
        except OSError as exc:
            # Non-fatal: the inbox write succeeded; log the failure but don't
            # change the exit code so cron doesn't retry the entire observation.
            print(f"Warning: could not write to {obs_log}: {exc}", file=sys.stderr)

    print(f"Observation written: {payload['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
