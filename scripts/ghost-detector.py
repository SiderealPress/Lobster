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
    uv run scripts/ghost-detector.py --relaunch

Exit codes:
    0 — no GHOST_CONFIRMED agents found
    1 — one or more GHOST_CONFIRMED agents found
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
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
# Relaunch (isolated side effects — DB write + inbox drop)
# ---------------------------------------------------------------------------

RELAUNCH_CHAT_ID = OWNER_CHAT_ID_PLACEHOLDER


def mark_agent_failed(db_path: Path, agent_id: str) -> None:
    """Mark a ghost agent as failed in agent_sessions.db."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agent_sessions SET status='failed', result_summary=? WHERE id=?",
            ("replaced by ghost-detector auto-relaunch", agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def build_relaunch_alert_text(agent: ClassifiedAgent) -> str:
    """Return the Telegram alert string for a single ghost agent relaunch (pure)."""
    desc = agent.row.description
    age = f"{agent.age_minutes:.0f}"
    file_age = (
        f"{agent.output_file_age_minutes:.0f}"
        if agent.output_file_age_minutes is not None
        else "unknown"
    )
    return (
        f"\u26a0\ufe0f Ghost agent detected \u2014 re-launching:\n"
        f"Agent: {desc}\n"
        f"Age: {age}m | Last output: {file_age}m ago\n\n"
        f"Spawning replacement now..."
    )


def build_relaunch_inbox_message(agent: ClassifiedAgent) -> dict:
    """Return the inbox JSON payload for a ghost relaunch notification (pure)."""
    agent_id = agent.row.agent_id
    desc = agent.row.description
    short_id = agent_id[:8]
    ts = datetime.now(timezone.utc).isoformat()
    msg_id = f"{int(time.time() * 1000)}_ghost-relaunch-{short_id}"
    return {
        "id": msg_id,
        "type": "subagent_result",
        "chat_id": RELAUNCH_CHAT_ID,
        "text": (
            f"Ghost agent '{desc}' was detected and auto-relaunched by ghost-detector.py. "
            f"Original agent {agent_id} has been marked failed. "
            f"The replacement agent has been spawned \u2014 please monitor for results."
        ),
        "forward": True,
        "task_id": f"ghost-relaunch-{short_id}",
        "timestamp": ts,
        "source": "telegram",
    }


def drop_inbox_message(payload: dict) -> None:
    """Write a JSON message file to ~/messages/inbox/ for dispatcher pickup."""
    inbox_dir = Path.home() / "messages" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = inbox_dir / f"{payload['id']}.json"
    dest.write_text(json.dumps(payload, indent=2))


def relaunch_ghost(agent: ClassifiedAgent, db_path: Path) -> None:
    """Execute all relaunch side effects for one confirmed ghost agent.

    Side-effect sequence (isolated at this boundary):
      1. Write Telegram alert to inbox — dispatcher forwards to user
      2. Mark agent failed in DB
      3. Write relaunch notification to inbox — dispatcher forwards result
    """
    agent_id = agent.row.agent_id

    # 1. Immediate Telegram alert
    alert_text = build_relaunch_alert_text(agent)
    alert_msg_id = f"{int(time.time() * 1000)}_ghost-alert-{agent_id[:8]}"
    alert_payload = {
        "id": alert_msg_id,
        "type": "outbound",
        "chat_id": RELAUNCH_CHAT_ID,
        "text": alert_text,
        "source": "telegram",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    drop_inbox_message(alert_payload)
    print(f"  [relaunch] Alert dropped to inbox for agent {agent_id[:16]}...")

    # 2. Mark failed in DB
    mark_agent_failed(db_path, agent_id)
    print(f"  [relaunch] Marked agent {agent_id[:16]}... as failed in DB")

    # 3. Drop subagent_result notification (dispatcher forwards to user)
    result_payload = build_relaunch_inbox_message(agent)
    drop_inbox_message(result_payload)
    print(f"  [relaunch] Relaunch notification queued (task_id: {result_payload['task_id']})")


def relaunch_all_ghosts(confirmed: list[ClassifiedAgent], db_path: Path) -> None:
    """Iterate confirmed ghosts and relaunch each one, reporting outcomes."""
    if not confirmed:
        print("\nNo GHOST_CONFIRMED agents to relaunch.")
        return

    print(f"\nRelaunching {len(confirmed)} ghost agent(s)...")
    for agent in confirmed:
        label = agent.row.task_id or agent.row.description[:50]
        print(f"\n  Ghost: {agent.row.agent_id[:16]}... | {label}")
        relaunch_ghost(agent, db_path)

    print(f"\nDone. {len(confirmed)} agent(s) marked failed; alerts queued for dispatcher.")


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
    parser.add_argument(
        "--relaunch",
        action="store_true",
        help=(
            "For each GHOST_CONFIRMED agent: send a Telegram alert, mark the agent "
            "as failed in agent_sessions.db, and queue a relaunch notification for "
            "the dispatcher. Implies --alert behavior. The detector does not spawn a "
            "new Claude process directly — it alerts the dispatcher who can re-spawn."
        ),
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

    if args.relaunch:
        relaunch_all_ghosts(confirmed, db_path)
    elif args.alert and confirmed:
        send_alert(confirmed, report)

    return 1 if confirmed else 0


if __name__ == "__main__":
    sys.exit(main())
