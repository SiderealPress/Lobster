#!/usr/bin/env python3
"""Ghost agent detector — finds agents that may have died without calling write_result.

A "ghost agent" is a background subagent registered in agent_sessions.db with
status=running that never completed (never called write_result). This tool
queries the DB, checks output file liveness, and classifies each stale session.

Usage:
    uv run scripts/ghost-detector.py
    uv run scripts/ghost-detector.py --threshold-minutes 60
    uv run scripts/ghost-detector.py --output-file-threshold-minutes 5
    uv run scripts/ghost-detector.py --alert

Exit codes:
    0 — no GHOST_CONFIRMED agents found
    1 — one or more GHOST_CONFIRMED agents found
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Classification = Literal[
    "GHOST_CONFIRMED",
    "GHOST_SUSPECTED",
    "STALE_NO_FILE",
    "HEALTHY",
]

DB_PATH = Path.home() / "messages" / "config" / "agent_sessions.db"


@dataclass(frozen=True)
class AgentRow:
    agent_id: str
    task_id: str | None
    description: str
    chat_id: str
    status: str
    spawned_at: str
    output_file: str | None
    last_seen_at: str | None


@dataclass(frozen=True)
class ClassifiedAgent:
    row: AgentRow
    classification: Classification
    age_minutes: float
    output_file_age_minutes: float | None  # None if no file or file missing


# ---------------------------------------------------------------------------
# Pure data functions
# ---------------------------------------------------------------------------


def parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string into a timezone-aware UTC datetime."""
    # Python 3.10 fromisoformat doesn't handle trailing Z; normalize it.
    normalized = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_age_minutes(spawned_at: str, now: datetime) -> float:
    return (now - parse_iso_utc(spawned_at)).total_seconds() / 60


def compute_output_file_age_minutes(output_file: str | None, now: datetime) -> float | None:
    """Return minutes since output_file was last modified, or None if unavailable."""
    if not output_file:
        return None
    path = Path(output_file)
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (now - mtime).total_seconds() / 60


def classify(
    age_minutes: float,
    output_file: str | None,
    output_file_age_minutes: float | None,
    threshold_minutes: float,
    output_file_threshold_minutes: float,
) -> Classification:
    """Classify a running agent given age and output file liveness.

    Logic (all thresholds configurable):
      - age < threshold             → HEALTHY (too young to worry about)
      - age >= threshold, no file   → STALE_NO_FILE (can't check liveness)
      - age >= threshold, file recent → GHOST_SUSPECTED (still writing, maybe slow)
      - age >= threshold, file old/missing → GHOST_CONFIRMED (likely dead)
    """
    if age_minutes < threshold_minutes:
        return "HEALTHY"

    # Agent is stale — now check output file liveness
    if output_file is None:
        return "STALE_NO_FILE"

    if output_file_age_minutes is None:
        # File path recorded but file doesn't exist → no heartbeat ever written
        return "GHOST_CONFIRMED"

    if output_file_age_minutes <= output_file_threshold_minutes:
        return "GHOST_SUSPECTED"

    return "GHOST_CONFIRMED"


def classify_agent(
    row: AgentRow,
    now: datetime,
    threshold_minutes: float,
    output_file_threshold_minutes: float,
) -> ClassifiedAgent:
    age = compute_age_minutes(row.spawned_at, now)
    file_age = compute_output_file_age_minutes(row.output_file, now)
    label = classify(age, row.output_file, file_age, threshold_minutes, output_file_threshold_minutes)
    return ClassifiedAgent(
        row=row,
        classification=label,
        age_minutes=age,
        output_file_age_minutes=file_age,
    )


# ---------------------------------------------------------------------------
# DB query (isolated side effect)
# ---------------------------------------------------------------------------


def load_running_agents(db_path: Path) -> list[AgentRow]:
    """Query agent_sessions.db for all running agents."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, task_id, description, chat_id, status,
                   spawned_at, output_file, last_seen_at
            FROM agent_sessions
            WHERE status = 'running'
            ORDER BY spawned_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    return [
        AgentRow(
            agent_id=row["id"],
            task_id=row["task_id"],
            description=row["description"] or "(no description)",
            chat_id=row["chat_id"],
            status=row["status"],
            spawned_at=row["spawned_at"],
            output_file=row["output_file"],
            last_seen_at=row["last_seen_at"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Report formatting (pure)
# ---------------------------------------------------------------------------


def format_agent_line(agent: ClassifiedAgent) -> str:
    short_id = agent.row.agent_id[:16]
    task = agent.row.task_id or "(no task_id)"
    desc = agent.row.description[:60]
    age_str = f"{agent.age_minutes:.0f}m"
    file_age_str = (
        f"{agent.output_file_age_minutes:.0f}m ago"
        if agent.output_file_age_minutes is not None
        else "file missing" if agent.row.output_file else "no file recorded"
    )
    return f"  - agent_id: {short_id}... | age: {age_str:>5} | file: {file_age_str:>15} | {task} — {desc}"


def build_report(
    classified: list[ClassifiedAgent],
    now: datetime,
    threshold_minutes: float,
    output_file_threshold_minutes: float,
) -> str:
    order: list[Classification] = [
        "GHOST_CONFIRMED",
        "GHOST_SUSPECTED",
        "STALE_NO_FILE",
        "HEALTHY",
    ]
    by_class: dict[Classification, list[ClassifiedAgent]] = {k: [] for k in order}
    for agent in classified:
        by_class[agent.classification].append(agent)

    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        f"Ghost Agent Report — {timestamp}",
        "==========================================",
        f"(stale threshold: {threshold_minutes:.0f}m | output-file threshold: {output_file_threshold_minutes:.0f}m)",
        "",
    ]

    for label in order:
        agents = by_class[label]
        if not agents:
            continue
        lines.append(f"{label} ({len(agents)}):")
        for a in agents:
            lines.append(format_agent_line(a))
        lines.append("")

    ghost_count = len(by_class["GHOST_CONFIRMED"]) + len(by_class["GHOST_SUSPECTED"]) + len(by_class["STALE_NO_FILE"])
    total = len(classified)
    healthy = len(by_class["HEALTHY"])
    ghost_rate = f"{ghost_count}/{total} = {ghost_count/total*100:.0f}%" if total else "0/0"

    lines.append(
        f"Summary: {ghost_count} ghosts ({len(by_class['GHOST_CONFIRMED'])} confirmed, "
        f"{len(by_class['GHOST_SUSPECTED'])} suspected, {len(by_class['STALE_NO_FILE'])} stale-no-file), "
        f"{healthy} healthy | ghost rate: {ghost_rate}"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alert (isolated side effect)
# ---------------------------------------------------------------------------


def send_alert(confirmed: list[ClassifiedAgent], report: str) -> None:
    """Send Telegram alert via lobster-inbox MCP if GHOST_CONFIRMED agents found."""
    if not confirmed:
        return

    # Build a condensed alert rather than the full report
    agent_lines = "\n".join(
        f"  • {a.row.agent_id[:16]}... | {a.age_minutes:.0f}m old | {a.row.task_id or a.row.description[:40]}"
        for a in confirmed
    )
    alert_text = (
        f"Ghost agent alert: {len(confirmed)} GHOST_CONFIRMED agent(s) detected.\n\n"
        f"{agent_lines}\n\n"
        f"Run `uv run scripts/ghost-detector.py` for full report."
    )

    # The MCP server is not available as a subprocess; use the lobster-inbox
    # HTTP API directly if configured, or print a warning.
    mcp_socket = os.environ.get("LOBSTER_MCP_SOCKET") or os.environ.get("LOBSTER_INBOX_SOCKET")
    if mcp_socket:
        # Future: implement socket-based MCP call here
        print(f"[alert] MCP socket found at {mcp_socket} — alert delivery not yet implemented via socket.")
        print(f"[alert] Alert text:\n{alert_text}")
    else:
        # Fallback: attempt to invoke ghost-alert via the scripts/alert.sh helper
        alert_sh = Path(__file__).parent / "alert.sh"
        if alert_sh.exists():
            result = subprocess.run(
                ["bash", str(alert_sh), alert_text],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"[alert] alert.sh failed: {result.stderr}", file=sys.stderr)
            else:
                print(f"[alert] Alert sent via alert.sh.")
        else:
            print(
                "[alert] --alert flag set but no delivery method available.\n"
                "        Set LOBSTER_MCP_SOCKET or ensure scripts/alert.sh exists.\n"
                f"        Alert text:\n{alert_text}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect ghost agents — running sessions that never called write_result.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"Path to agent_sessions.db (default: {DB_PATH})",
    )
    parser.add_argument(
        "--threshold-minutes",
        type=float,
        default=30.0,
        metavar="N",
        help="Age in minutes before a running agent is considered stale (default: 30)",
    )
    parser.add_argument(
        "--output-file-threshold-minutes",
        type=float,
        default=10.0,
        metavar="N",
        help="Output file must have been modified within this many minutes to count as alive (default: 10)",
    )
    parser.add_argument(
        "--alert",
        action="store_true",
        help="Send Telegram alert if GHOST_CONFIRMED count > 0",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    db_path: Path = args.db
    if not db_path.exists():
        print(f"Error: agent_sessions.db not found at {db_path}", file=sys.stderr)
        print("Is Lobster installed? Expected path: ~/messages/config/agent_sessions.db", file=sys.stderr)
        return 2

    now = datetime.now(tz=timezone.utc)

    running_agents = load_running_agents(db_path)

    classified = [
        classify_agent(row, now, args.threshold_minutes, args.output_file_threshold_minutes)
        for row in running_agents
    ]

    report = build_report(classified, now, args.threshold_minutes, args.output_file_threshold_minutes)
    print(report)

    confirmed = [a for a in classified if a.classification == "GHOST_CONFIRMED"]

    if args.alert and confirmed:
        send_alert(confirmed, report)

    return 1 if confirmed else 0


if __name__ == "__main__":
    sys.exit(main())
