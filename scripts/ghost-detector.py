#!/usr/bin/env python3
"""Ghost agent detector — finds agents that may have died without calling write_result.

A "ghost agent" is a background subagent registered in agent_sessions.db with
status=running that never completed (never called write_result). This tool
queries the DB, checks output file liveness, and classifies each stale session.

It also scans the filesystem for agent output files that exist but have no
corresponding DB entry — these "unregistered agents" indicate registration
failures and would otherwise be invisible to monitoring.

It also detects COMPLETED_NOT_UPDATED agents: DB entries still showing
status=running even though the agent's transcript confirms write_result was
called. These are reported but NOT corrected — if they appear, it means the
SubagentStop hook (PR #418) isn't working. Auto-correcting would hide the bug.

Usage:
    uv run scripts/ghost-detector.py
    uv run scripts/ghost-detector.py --threshold-minutes 60
    uv run scripts/ghost-detector.py --output-file-threshold-minutes 5
    uv run scripts/ghost-detector.py --alert
    uv run scripts/ghost-detector.py --mark-failed
    uv run scripts/ghost-detector.py --no-fs-scan

Exit codes:
    0 — no GHOST_CONFIRMED or UNREGISTERED agents found
    1 — one or more GHOST_CONFIRMED or UNREGISTERED agents found
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from glob import glob
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

# Glob pattern for Claude Code session task directories.
# The middle component is a session UUID that changes per Claude Code invocation.
AGENT_OUTPUT_GLOB = "/tmp/claude-1000/-home-lobster-lobster-workspace/*/tasks/"

# Pattern for agent JSONL symlink filenames: agent-<hex_id>.jsonl
AGENT_SYMLINK_PATTERN = re.compile(r"^agent-([0-9a-f]+)\.jsonl$")

# Age threshold for treating an unregistered output file as "active" (minutes)
UNREGISTERED_ACTIVE_THRESHOLD_MINUTES = 30.0


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


@dataclass(frozen=True)
class UnregisteredAgent:
    """An agent found via filesystem scan with no corresponding DB entry."""

    agent_id: str
    output_file: str
    output_file_age_minutes: float
    is_active: bool  # True if modified within UNREGISTERED_ACTIVE_THRESHOLD_MINUTES


@dataclass(frozen=True)
class CompletedNotUpdatedAgent:
    """A DB entry still showing status=running whose transcript confirms write_result was called.

    These are type-2 divergences: the agent completed normally but the DB was
    never updated from 'running' to 'completed'. Reported for observability but
    NOT auto-corrected — their presence signals a SubagentStop hook failure.
    """

    agent_id: str
    task_id: str | None
    description: str
    spawned_at: str
    output_file: str


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


def check_transcript_for_write_result(output_file: str) -> bool:
    """Return True if the agent's transcript shows write_result was called as a tool.

    Scans the JSONL output file for evidence of an actual
    mcp__lobster-inbox__write_result tool_use block. Looks for the tool name
    inside a JSON "tool_use" block to avoid false positives from the subagent
    bootup instructions, which mention the tool name verbatim in plain text.

    A line is counted only when it contains both '"type": "tool_use"' (or
    '"type":"tool_use"') and '"mcp__lobster-inbox__write_result"' within the
    same JSON object. This is more precise than a plain substring scan.
    """
    if not output_file or not os.path.exists(output_file):
        return False
    try:
        with open(output_file, "r") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                # Fast pre-filter: both substrings must be present on this line
                if "mcp__lobster-inbox__write_result" not in line:
                    continue
                if "tool_use" not in line:
                    continue
                # Parse the line to verify this is an actual tool_use record
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # Check inside the 'message' field (JSONL entry format) or
                # directly at the top level (tool_result / assistant message format).
                def _has_write_result_tool_use(node: object) -> bool:
                    if isinstance(node, dict):
                        if node.get("type") == "tool_use" and node.get("name") == "mcp__lobster-inbox__write_result":
                            return True
                        for v in node.values():
                            if _has_write_result_tool_use(v):
                                return True
                    elif isinstance(node, list):
                        for item in node:
                            if _has_write_result_tool_use(item):
                                return True
                    return False

                if _has_write_result_tool_use(obj):
                    return True
        return False
    except Exception:
        return False


def detect_completed_not_updated(
    rows: list[AgentRow],
) -> list[CompletedNotUpdatedAgent]:
    """Return DB-running agents whose transcripts confirm write_result was called.

    Pure function — only reads filesystem, no writes.
    """
    return [
        CompletedNotUpdatedAgent(
            agent_id=row.agent_id,
            task_id=row.task_id,
            description=row.description,
            spawned_at=row.spawned_at,
            output_file=row.output_file or "",
        )
        for row in rows
        if row.output_file and check_transcript_for_write_result(row.output_file)
    ]


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
# Filesystem scan — pure data collection, no side effects
# ---------------------------------------------------------------------------


def find_agent_symlinks(tasks_dir: Path) -> list[Path]:
    """Return all agent JSONL symlinks in a Claude Code tasks directory."""
    if not tasks_dir.is_dir():
        return []
    return [
        p
        for p in tasks_dir.iterdir()
        if p.is_symlink() and AGENT_SYMLINK_PATTERN.match(p.name)
    ]


def extract_agent_id_from_symlink(symlink: Path) -> str | None:
    """Extract the hex agent_id from an agent-<id>.jsonl symlink filename."""
    m = AGENT_SYMLINK_PATTERN.match(symlink.name)
    return m.group(1) if m else None


def compute_symlink_target_age_minutes(symlink: Path, now: datetime) -> float | None:
    """Return minutes since the symlink *target* was last modified.

    We stat the resolved target (the actual .jsonl file) rather than the
    symlink itself, since symlink mtime is typically set at creation time.
    """
    try:
        resolved = symlink.resolve()
        if not resolved.exists():
            return None
        mtime = datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc)
        return (now - mtime).total_seconds() / 60
    except OSError:
        return None


def scan_task_dirs(glob_base: str = AGENT_OUTPUT_GLOB) -> list[Path]:
    """Return all task directories matching the Claude Code session glob."""
    return [Path(p) for p in glob(glob_base)]


def discover_filesystem_agents(
    now: datetime,
    known_agent_ids: set[str],
    active_threshold_minutes: float = UNREGISTERED_ACTIVE_THRESHOLD_MINUTES,
    glob_base: str = AGENT_OUTPUT_GLOB,
) -> list[UnregisteredAgent]:
    """Scan the filesystem for agent output files not present in the DB.

    Returns UnregisteredAgent entries for any agent JSONL symlink whose
    agent_id is not in known_agent_ids. Broken symlinks (missing targets)
    are skipped.

    All I/O is read-only — no side effects.
    """
    task_dirs = scan_task_dirs(glob_base)
    unregistered: list[UnregisteredAgent] = []

    for task_dir in task_dirs:
        for symlink in find_agent_symlinks(task_dir):
            agent_id = extract_agent_id_from_symlink(symlink)
            if agent_id is None:
                continue
            if agent_id in known_agent_ids:
                continue  # Already tracked in DB

            file_age = compute_symlink_target_age_minutes(symlink, now)
            if file_age is None:
                continue  # Broken symlink or unreadable — skip

            unregistered.append(
                UnregisteredAgent(
                    agent_id=agent_id,
                    output_file=str(symlink),
                    output_file_age_minutes=file_age,
                    is_active=file_age <= active_threshold_minutes,
                )
            )

    return unregistered


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


def load_all_known_agent_ids(db_path: Path) -> set[str]:
    """Return all agent IDs from agent_sessions.db, regardless of status.

    Used to cross-reference filesystem discoveries against the DB so we don't
    surface completed/failed agents as "unregistered".
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT id FROM agent_sessions").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


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


def format_completed_not_updated_line(agent: CompletedNotUpdatedAgent) -> str:
    short_id = agent.agent_id[:16]
    task = agent.task_id or "(no task_id)"
    desc = agent.description[:60]
    return f"  - agent_id: {short_id}... | {task} — {desc} | output: {agent.output_file}"


def format_unregistered_line(agent: UnregisteredAgent) -> str:
    short_id = agent.agent_id[:16]
    file_age_str = f"{agent.output_file_age_minutes:.0f}m ago"
    status = "ACTIVE" if agent.is_active else "STALE"
    return f"  - agent_id: {short_id}... | file: {file_age_str:>12} | [{status}] {agent.output_file}"


def build_report(
    classified: list[ClassifiedAgent],
    unregistered: list[UnregisteredAgent],
    now: datetime,
    threshold_minutes: float,
    output_file_threshold_minutes: float,
    completed_not_updated: list[CompletedNotUpdatedAgent] | None = None,
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

    completed_not_updated = completed_not_updated or []

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

    # COMPLETED_NOT_UPDATED — DB running but transcript confirms write_result called
    if completed_not_updated:
        lines.append(
            f"COMPLETED_NOT_UPDATED ({len(completed_not_updated)}) — DB=running but transcript confirms write_result:"
        )
        lines.append("  (diagnostic only — SubagentStop hook may not be working)")
        for c in completed_not_updated:
            lines.append(format_completed_not_updated_line(c))
        lines.append("")

    # Unregistered agents — filesystem-only discoveries
    if unregistered:
        active_count = sum(1 for u in unregistered if u.is_active)
        stale_count = len(unregistered) - active_count
        lines.append(f"UNREGISTERED ({len(unregistered)}) — found on filesystem, not in DB:")
        lines.append(
            f"  (active: {active_count} modified within {UNREGISTERED_ACTIVE_THRESHOLD_MINUTES:.0f}m"
            f" | stale: {stale_count})"
        )
        for u in unregistered:
            lines.append(format_unregistered_line(u))
        lines.append("")

    ghost_count = (
        len(by_class["GHOST_CONFIRMED"])
        + len(by_class["GHOST_SUSPECTED"])
        + len(by_class["STALE_NO_FILE"])
    )
    total = len(classified)
    healthy = len(by_class["HEALTHY"])
    ghost_rate = f"{ghost_count}/{total} = {ghost_count/total*100:.0f}%" if total else "0/0"

    lines.append(
        f"Summary: {ghost_count} ghosts ({len(by_class['GHOST_CONFIRMED'])} confirmed, "
        f"{len(by_class['GHOST_SUSPECTED'])} suspected, {len(by_class['STALE_NO_FILE'])} stale-no-file), "
        f"{healthy} healthy | ghost rate: {ghost_rate}"
    )
    if completed_not_updated:
        lines.append(
            f"         {len(completed_not_updated)} completed-not-updated (DB divergence — SubagentStop hook may be broken)"
        )
    if unregistered:
        active_u = sum(1 for u in unregistered if u.is_active)
        lines.append(
            f"         {len(unregistered)} unregistered ({active_u} active, "
            f"{len(unregistered) - active_u} stale) — likely registration failures"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alert (isolated side effect)
# ---------------------------------------------------------------------------


def send_alert(
    confirmed: list[ClassifiedAgent],
    unregistered: list[UnregisteredAgent],
    report: str,
) -> None:
    """Send Telegram alert if GHOST_CONFIRMED or UNREGISTERED agents found."""
    if not confirmed and not unregistered:
        return

    parts: list[str] = []
    if confirmed:
        agent_lines = "\n".join(
            f"  • {a.row.agent_id[:16]}... | {a.age_minutes:.0f}m old | {a.row.task_id or a.row.description[:40]}"
            for a in confirmed
        )
        parts.append(f"{len(confirmed)} GHOST_CONFIRMED agent(s):\n{agent_lines}")

    if unregistered:
        active_u = [u for u in unregistered if u.is_active]
        unreg_lines = "\n".join(
            f"  • {u.agent_id[:16]}... | {u.output_file_age_minutes:.0f}m old | {'ACTIVE' if u.is_active else 'STALE'}"
            for u in unregistered
        )
        parts.append(f"{len(unregistered)} UNREGISTERED agent(s) ({len(active_u)} active):\n{unreg_lines}")

    alert_text = (
        "Ghost agent alert:\n\n"
        + "\n\n".join(parts)
        + "\n\nRun `uv run scripts/ghost-detector.py` for full report."
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
# Mark-failed remediation (isolated side effects — DB write + inbox drop)
# ---------------------------------------------------------------------------

RELAUNCH_CHAT_ID = 8305714125


def mark_agent_completed(db_path: Path, agent_id: str) -> None:
    """Update a COMPLETED_NOT_UPDATED agent's status to 'completed' in agent_sessions.db."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agent_sessions SET status='completed', result_summary=? WHERE id=?",
            ("auto-corrected by ghost-detector: transcript confirmed write_result was called", agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_agent_failed(db_path: Path, agent_id: str) -> None:
    """Mark a ghost agent as failed in agent_sessions.db."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agent_sessions SET status='failed', result_summary=? WHERE id=?",
            ("marked failed by ghost-detector --mark-failed", agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def build_mark_failed_alert_text(agent: ClassifiedAgent) -> str:
    """Return the Telegram alert string for a single ghost agent being marked failed (pure)."""
    desc = agent.row.description
    age = f"{agent.age_minutes:.0f}"
    file_age = (
        f"{agent.output_file_age_minutes:.0f}"
        if agent.output_file_age_minutes is not None
        else "unknown"
    )
    return (
        f"\u26a0\ufe0f Ghost agent detected \u2014 marking failed:\n"
        f"Agent: {desc}\n"
        f"Age: {age}m | Last output: {file_age}m ago\n\n"
        f"Agent has been marked failed. Dispatcher will be notified."
    )


def build_mark_failed_inbox_message(agent: ClassifiedAgent) -> dict:
    """Return the inbox JSON payload for a ghost mark-failed notification (pure)."""
    agent_id = agent.row.agent_id
    desc = agent.row.description
    short_id = agent_id[:8]
    ts = datetime.now(timezone.utc).isoformat()
    msg_id = f"{int(time.time() * 1000)}_ghost-mark-failed-{short_id}"
    return {
        "id": msg_id,
        "type": "subagent_result",
        "chat_id": RELAUNCH_CHAT_ID,
        "text": (
            f"Ghost agent '{desc}' was detected by ghost-detector.py. "
            f"Agent {agent_id} has been marked failed in the DB. "
            f"The dispatcher has been notified \u2014 manual re-spawn may be needed."
        ),
        "forward": True,
        "task_id": f"ghost-mark-failed-{short_id}",
        "timestamp": ts,
        "source": "telegram",
    }


def drop_inbox_message(payload: dict) -> None:
    """Write a JSON message file to ~/messages/inbox/ for dispatcher pickup."""
    inbox_dir = Path.home() / "messages" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = inbox_dir / f"{payload['id']}.json"
    dest.write_text(json.dumps(payload, indent=2))


def mark_failed_ghost(agent: ClassifiedAgent, db_path: Path) -> None:
    """Execute all mark-failed side effects for one confirmed ghost agent.

    Side-effect sequence (isolated at this boundary):
      1. Write Telegram alert to inbox — dispatcher forwards to user
      2. Mark agent failed in DB
      3. Write mark-failed notification to inbox — dispatcher forwards result
    """
    agent_id = agent.row.agent_id

    # 1. Immediate Telegram alert
    alert_text = build_mark_failed_alert_text(agent)
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
    print(f"  [mark-failed] Alert dropped to inbox for agent {agent_id[:16]}...")

    # 2. Mark failed in DB
    mark_agent_failed(db_path, agent_id)
    print(f"  [mark-failed] Marked agent {agent_id[:16]}... as failed in DB")

    # 3. Drop subagent_result notification (dispatcher forwards to user)
    result_payload = build_mark_failed_inbox_message(agent)
    drop_inbox_message(result_payload)
    print(f"  [mark-failed] Notification queued (task_id: {result_payload['task_id']})")


def build_unregistered_mark_failed_payload(agent: UnregisteredAgent) -> dict:
    """Return an inbox JSON payload for a dead unregistered agent notification (pure)."""
    short_id = agent.agent_id[:8]
    ts = datetime.now(timezone.utc).isoformat()
    msg_id = f"{int(time.time() * 1000)}_ghost-unregistered-{short_id}"
    return {
        "id": msg_id,
        "type": "subagent_result",
        "chat_id": RELAUNCH_CHAT_ID,
        "text": (
            f"Unregistered dead agent {agent.agent_id} detected by ghost-detector.py. "
            f"Output file last modified {agent.output_file_age_minutes:.0f}m ago: {agent.output_file}. "
            f"This agent was never registered in agent_sessions.db — likely a registration failure."
        ),
        "forward": True,
        "task_id": f"ghost-unregistered-{short_id}",
        "timestamp": ts,
        "source": "telegram",
    }


def mark_failed_unregistered(agent: UnregisteredAgent) -> None:
    """Queue a dispatcher notification for an unregistered dead agent.

    Since the agent has no DB row, we cannot update agent_sessions.db.
    Instead we drop an inbox notification so the dispatcher is aware of
    the registration gap.
    """
    payload = build_unregistered_mark_failed_payload(agent)
    drop_inbox_message(payload)
    print(f"  [mark-failed] Notification queued for unregistered agent {agent.agent_id[:16]}...")


def mark_failed_all_ghosts(confirmed: list[ClassifiedAgent], db_path: Path) -> None:
    """Iterate confirmed ghosts and mark each one failed, reporting outcomes."""
    if not confirmed:
        print("\nNo GHOST_CONFIRMED agents to mark failed.")
        return

    print(f"\nMarking {len(confirmed)} ghost agent(s) as failed...")
    for agent in confirmed:
        label = agent.row.task_id or agent.row.description[:50]
        print(f"\n  Ghost: {agent.row.agent_id[:16]}... | {label}")
        mark_failed_ghost(agent, db_path)

    print(f"\nDone. {len(confirmed)} agent(s) marked failed; alerts queued for dispatcher.")


def auto_correct_completed_not_updated(
    agents: list[CompletedNotUpdatedAgent], db_path: Path
) -> None:
    """Update all COMPLETED_NOT_UPDATED agents to status=completed in the DB.

    Always runs unconditionally — transcript evidence makes this safe regardless
    of --mark-failed flag.
    """
    if not agents:
        return

    print(f"\nAuto-correcting {len(agents)} COMPLETED_NOT_UPDATED agent(s) to status=completed...")
    for agent in agents:
        label = agent.task_id or agent.description[:50]
        mark_agent_completed(db_path, agent.agent_id)
        print(f"  [auto-correct] {agent.agent_id[:16]}... | {label} → completed")

    print(f"\nDone. {len(agents)} agent(s) corrected.")


def mark_failed_unregistered_dead(unregistered: list[UnregisteredAgent]) -> None:
    """Queue inbox notifications for dead unregistered agents (used with --relaunch).

    Only acts on stale (non-active) unregistered agents. Active ones may still
    be running — notifying the dispatcher about them would create false positives.
    """
    dead = [u for u in unregistered if not u.is_active]
    if not dead:
        print("\nNo dead unregistered agents to notify about.")
        return

    print(f"\nNotifying dispatcher of {len(dead)} dead unregistered agent(s)...")
    for agent in dead:
        print(f"\n  Unregistered dead: {agent.agent_id[:16]}... | {agent.output_file_age_minutes:.0f}m stale")
        mark_failed_unregistered(agent)

    print(f"\nDone. {len(dead)} notification(s) queued for dispatcher.")


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
        "--mark-failed",
        action="store_true",
        help=(
            "For each GHOST_CONFIRMED agent: send a Telegram alert, mark the agent "
            "as failed in agent_sessions.db, and queue a notification for the "
            "dispatcher. For dead UNREGISTERED agents: queue a dispatcher notification. "
            "The detector does not spawn a new Claude process directly — "
            "it alerts the dispatcher who can decide whether to re-spawn."
        ),
    )
    parser.add_argument(
        "--no-fs-scan",
        action="store_true",
        help="Disable filesystem scan; only use agent_sessions.db (legacy behavior)",
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

    # Detect type-2 divergence: DB=running but transcript confirms write_result called.
    # These are extracted before classification so they can be excluded from ghost logic.
    completed_not_updated = detect_completed_not_updated(running_agents)
    completed_agent_ids = {c.agent_id for c in completed_not_updated}

    # Classify remaining running agents (exclude confirmed-completed ones)
    classified = [
        classify_agent(row, now, args.threshold_minutes, args.output_file_threshold_minutes)
        for row in running_agents
        if row.agent_id not in completed_agent_ids
    ]

    # Filesystem scan — discover unregistered agents unless opted out
    unregistered: list[UnregisteredAgent] = []
    if not args.no_fs_scan:
        all_known_ids = load_all_known_agent_ids(db_path)
        unregistered = discover_filesystem_agents(now, all_known_ids)

    report = build_report(
        classified,
        unregistered,
        now,
        args.threshold_minutes,
        args.output_file_threshold_minutes,
        completed_not_updated=completed_not_updated,
    )
    print(report)

    confirmed = [a for a in classified if a.classification == "GHOST_CONFIRMED"]

    if args.mark_failed:
        mark_failed_all_ghosts(confirmed, db_path)
        mark_failed_unregistered_dead(unregistered)
    elif args.alert and (confirmed or unregistered):
        send_alert(confirmed, unregistered, report)

    return 1 if (confirmed or unregistered) else 0


if __name__ == "__main__":
    sys.exit(main())
