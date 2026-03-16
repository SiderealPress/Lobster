#!/usr/bin/env python3
"""OOM Kill Monitor — detects when the Linux OOM killer kills Lobster/Claude.

Scans the kernel journal for OOM kill events involving claude/python/lobster
processes. On detection, writes an alert to the Lobster inbox so the dispatcher
can notify the user via whatever messaging platform is active. Uses a state file
to avoid duplicate alerts for the same kill event.

**Debug-mode gate:** This monitor only runs when LOBSTER_DEBUG=true. If that
environment variable is absent or not "true", the script exits immediately
with code 0 (no-op). This prevents unnecessary journal scanning in production
unless debug monitoring is explicitly enabled.

Usage:
    LOBSTER_DEBUG=true uv run scripts/oom-monitor.py
    LOBSTER_DEBUG=true uv run scripts/oom-monitor.py --since-minutes 10
    LOBSTER_DEBUG=true uv run scripts/oom-monitor.py --dry-run

Design:
    - Pure functions for parsing and classification; side effects isolated at edges
    - State file tracks seen events (keyed by timestamp+pid) to prevent duplicates
    - Alert path: inbox JSON drop for dispatcher pickup (platform-agnostic)

Exit codes:
    0 — no OOM kills detected (or LOBSTER_DEBUG not set)
    1 — OOM kill(s) detected (alert sent)
    2 — error (journalctl unavailable, config missing, etc.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Processes we care about — any of these appearing in an OOM kill message
# indicates the Lobster system was affected.
LOBSTER_PROCESS_NAMES = frozenset(
    [
        "claude",
        "python",
        "python3",
        "node",  # MCP servers
        "lobster",
        "uv",
    ]
)

# State file: tracks seen OOM event IDs to prevent duplicate alerts
STATE_FILE = Path.home() / "lobster-workspace" / "data" / "oom-monitor-state.json"

# Inbox directory for dispatcher pickup
INBOX_DIR = Path.home() / "messages" / "inbox"

# Log file
LOG_FILE = Path.home() / "lobster-workspace" / "logs" / "oom-monitor.log"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OomKillEvent:
    """A single OOM kill event parsed from the kernel journal."""

    event_id: str          # stable hash of timestamp+pid for deduplication
    timestamp: str         # ISO 8601 UTC
    pid: int
    process_name: str
    message: str           # raw kernel log line
    is_lobster_process: bool


# ---------------------------------------------------------------------------
# Pure parsing functions
# ---------------------------------------------------------------------------

# Matches kernel OOM kill lines, e.g.:
#   "Out of memory: Killed process 1234 (claude) total-vm:..."
#   "Out of memory: Kill process 1234 (python3) score 900 or sacrifice child"
#   "Memory cgroup out of memory: Killed process 1234 (uv) total-vm:..."
_OOM_KILLED_RE = re.compile(
    r"(?:Out of memory|Memory cgroup out of memory)[^:]*:\s+Kill(?:ed)?\s+process\s+(\d+)\s+\(([^)]+)\)",
    re.IGNORECASE,
)

# Matches oom_reaper lines (confirmation the process was reaped), e.g.:
#   "oom_reaper: reaped process 1234 (claude), now anon-rss:0kB"
_OOM_REAPER_RE = re.compile(
    r"oom_reaper:\s+reaped\s+process\s+(\d+)\s+\(([^)]+)\)",
    re.IGNORECASE,
)


def parse_oom_event(timestamp: str, message: str) -> OomKillEvent | None:
    """Parse a single kernel log line into an OomKillEvent, or return None.

    Matches both 'Killed process' and 'oom_reaper' lines so we catch either
    confirmation that a process was killed. We prefer 'Killed process' lines
    as primary events; reaper lines are secondary confirmation.
    """
    for pattern in (_OOM_KILLED_RE, _OOM_REAPER_RE):
        m = pattern.search(message)
        if m:
            pid = int(m.group(1))
            proc = m.group(2).strip()
            event_id = _stable_event_id(timestamp, pid)
            is_lobster = proc.lower() in LOBSTER_PROCESS_NAMES
            return OomKillEvent(
                event_id=event_id,
                timestamp=timestamp,
                pid=pid,
                process_name=proc,
                message=message.strip(),
                is_lobster_process=is_lobster,
            )
    return None


def _stable_event_id(timestamp: str, pid: int) -> str:
    """Return a stable 16-char hex ID for a (timestamp, pid) pair."""
    raw = f"{timestamp}:{pid}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_lobster_affected(events: list[OomKillEvent]) -> bool:
    """Return True if any event involves a Lobster-related process."""
    return any(e.is_lobster_process for e in events)


def filter_new_events(
    events: list[OomKillEvent], seen_ids: set[str]
) -> list[OomKillEvent]:
    """Return only events whose event_id is not in seen_ids."""
    return [e for e in events if e.event_id not in seen_ids]


# ---------------------------------------------------------------------------
# Journal scanning (isolated side effect)
# ---------------------------------------------------------------------------


def scan_journal(since_minutes: int) -> tuple[list[OomKillEvent], str | None]:
    """Query journalctl for OOM events in the last N minutes.

    Returns (events, error_message). On success, error_message is None.
    On failure, events is empty and error_message describes what went wrong.
    """
    since_arg = f"{since_minutes} minutes ago"
    cmd = [
        "journalctl",
        "-k",                     # kernel messages only
        "--since", since_arg,
        "--output=json",
        "--no-pager",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return [], "journalctl not found — systemd not available on this host"
    except subprocess.TimeoutExpired:
        return [], "journalctl timed out after 30 seconds"

    if result.returncode not in (0, 1):
        # Exit 1 from journalctl typically means "no entries" — not an error
        stderr = result.stderr.strip()
        if stderr:
            return [], f"journalctl error (rc={result.returncode}): {stderr}"

    events: list[OomKillEvent] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("MESSAGE", "")
        if not message:
            continue
        # journalctl JSON uses __REALTIME_TIMESTAMP in microseconds since epoch
        ts_us = entry.get("__REALTIME_TIMESTAMP")
        if ts_us:
            ts = datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc).isoformat()
        else:
            ts = datetime.now(tz=timezone.utc).isoformat()

        event = parse_oom_event(ts, message)
        if event is not None:
            events.append(event)

    return events, None


# ---------------------------------------------------------------------------
# State file (isolated side effects)
# ---------------------------------------------------------------------------


def load_state(state_file: Path) -> set[str]:
    """Load previously seen event IDs from the state file."""
    if not state_file.exists():
        return set()
    try:
        data = json.loads(state_file.read_text())
        return set(data.get("seen_event_ids", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_state(state_file: Path, seen_ids: set[str]) -> None:
    """Persist seen event IDs to the state file (atomic write)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen_event_ids": sorted(seen_ids),
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    # Atomic write via temp file
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(state_file)


# ---------------------------------------------------------------------------
# Alert formatting (pure)
# ---------------------------------------------------------------------------


def format_telegram_alert(events: list[OomKillEvent]) -> str:
    """Build a Telegram alert message for OOM kill events (pure)."""
    lobster_events = [e for e in events if e.is_lobster_process]
    other_events = [e for e in events if not e.is_lobster_process]

    lines: list[str] = [
        "*OOM Kill Detected*",
        "",
        f"The Linux OOM killer has killed {len(events)} process(es).",
    ]

    if lobster_events:
        lines.append("")
        lines.append(f"*Lobster/Claude process(es) killed ({len(lobster_events)}):*")
        for e in lobster_events:
            ts = e.timestamp.replace("T", " ").replace("+00:00", " UTC")
            lines.append(f"  • `{e.process_name}` (PID {e.pid}) at {ts}")
        lines.append("")
        lines.append("Subagents may have become ghosts. The reconciler will clean up stale sessions.")

    if other_events:
        lines.append("")
        lines.append(f"*Other process(es) killed ({len(other_events)}):*")
        for e in other_events:
            lines.append(f"  • `{e.process_name}` (PID {e.pid})")

    lines.append("")
    lines.append("Run `uv run scripts/ghost-detector.py` to check for ghost agents.")

    return "\n".join(lines)


def format_inbox_message(events: list[OomKillEvent]) -> dict:
    """Build an inbox JSON message payload for dispatcher pickup (pure).

    The payload uses type "observation" so the dispatcher treats it as a
    system-generated alert rather than a user message. The dispatcher is
    responsible for routing to the active messaging platform — no Telegram
    chat_id is hardcoded here.
    """
    lobster_count = sum(1 for e in events if e.is_lobster_process)
    process_names = ", ".join(
        sorted({e.process_name for e in events if e.is_lobster_process})
    ) or "unknown"

    msg_id = f"{int(time.time() * 1000)}_oom-monitor"
    text = (
        f"OOM kill alert: {len(events)} process(es) killed by the Linux OOM killer. "
        f"{lobster_count} Lobster-related process(es) affected ({process_names}). "
        f"Subagents may have become ghosts — run ghost-detector.py to check."
    )
    return {
        "id": msg_id,
        "type": "observation",
        "category": "oom_kill",
        "text": text,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Alert delivery (isolated side effects)
# ---------------------------------------------------------------------------


def write_inbox_message(inbox_dir: Path, payload: dict) -> bool:
    """Write alert payload as JSON to the inbox directory. Returns True on success."""
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        dest = inbox_dir / f"{payload['id']}.json"
        dest.write_text(json.dumps(payload, indent=2))
        return True
    except OSError:
        return False


def log_event(log_file: Path, message: str) -> None:
    """Append a timestamped line to the log file (best-effort)."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(log_file, "a") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run(
    since_minutes: int,
    dry_run: bool,
    state_file: Path,
    inbox_dir: Path,
    log_file: Path,
) -> int:
    """Main logic. Returns exit code (0=clean, 1=OOM detected, 2=error)."""

    # 0. Debug-mode gate — only active when LOBSTER_DEBUG=true
    if os.environ.get("LOBSTER_DEBUG", "").lower() != "true":
        # Silent no-op: log-only so cron output stays clean
        log_event(log_file, "LOBSTER_DEBUG not set — OOM monitor is disabled. Set LOBSTER_DEBUG=true to enable.")
        return 0

    # 1. Scan the journal
    events, scan_error = scan_journal(since_minutes)
    if scan_error:
        log_event(log_file, f"ERROR: {scan_error}")
        print(f"Error: {scan_error}", file=sys.stderr)
        return 2

    if not events:
        log_event(log_file, f"Scanned last {since_minutes}m — no OOM events found.")
        return 0

    # 2. Filter to new events (deduplication)
    seen_ids = load_state(state_file)
    new_events = filter_new_events(events, seen_ids)

    if not new_events:
        log_event(log_file, f"Scanned last {since_minutes}m — {len(events)} OOM event(s) found but all already alerted.")
        return 0

    # 3. Focus on Lobster-relevant events for alert severity
    lobster_events = [e for e in new_events if e.is_lobster_process]
    log_event(
        log_file,
        f"OOM kill(s) detected: {len(new_events)} new event(s), "
        f"{len(lobster_events)} Lobster-related.",
    )

    # 4. Build alert text
    alert_text = format_telegram_alert(new_events)

    if dry_run:
        print("=== DRY RUN — no alerts sent ===")
        print(f"New OOM events: {len(new_events)}")
        for e in new_events:
            print(f"  [{e.timestamp}] pid={e.pid} proc={e.process_name} lobster={e.is_lobster_process}")
        print("\n--- Alert message ---")
        print(alert_text)
        return 1

    # 5. Write inbox observation for dispatcher pickup (platform-agnostic routing)
    payload = format_inbox_message(new_events)
    if write_inbox_message(inbox_dir, payload):
        log_event(log_file, f"Inbox observation written: {payload['id']}.json")
    else:
        log_event(log_file, "WARNING: Failed to write inbox observation.")

    # 6. Persist seen event IDs to prevent re-alerting
    updated_seen = seen_ids | {e.event_id for e in new_events}
    save_state(state_file, updated_seen)

    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor kernel journal for OOM kills affecting Lobster/Claude.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--since-minutes",
        type=int,
        default=10,
        metavar="N",
        help="Scan journal for OOM events in the last N minutes (default: 10). "
             "Match your cron interval to avoid gaps.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be alerted without sending anything.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=STATE_FILE,
        help=f"Path to OOM monitor state file (default: {STATE_FILE})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run(
        since_minutes=args.since_minutes,
        dry_run=args.dry_run,
        state_file=args.state_file,
        inbox_dir=INBOX_DIR,
        log_file=LOG_FILE,
    )


if __name__ == "__main__":
    sys.exit(main())
