#!/usr/bin/env python3
"""
Lobster Status Report Generator

Produces a concise, Telegram-friendly status report by querying:
- Dashboard collectors (system info, message queues, agents, health)
- Health check log (last health level and timestamp)
- Lobster state file (current lifecycle mode)
- Scheduled jobs registry

Output is pre-formatted text ready to send via send_reply().
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add Lobster src to path for dashboard collectors
_LOBSTER_SRC = Path(os.environ.get("LOBSTER_SRC", Path.home() / "lobster"))
sys.path.insert(0, str(_LOBSTER_SRC / "src" / "dashboard"))
sys.path.insert(0, str(_LOBSTER_SRC / "src"))

from collectors import (
    collect_conversation_activity,
    collect_health,
    collect_message_queues,
    collect_scheduled_jobs,
    collect_subagent_list,
    collect_system_info,
    collect_tasks,
)

# --- Directories ---
_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))
_STATE_FILE = _MESSAGES / "config" / "lobster-state.json"
_HEALTH_LOG = _WORKSPACE / "logs" / "health-check.log"
_JOBS_FILE = _WORKSPACE / "scheduled-jobs" / "jobs.json"


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        h = int(hours)
        m = int(minutes % 60)
        return f"{h}h {m}m" if m else f"{h}h"
    days = int(hours / 24)
    h = int(hours % 24)
    return f"{days}d {h}h" if h else f"{days}d"


def _get_lifecycle_state() -> dict:
    """Read the current Lobster lifecycle state."""
    try:
        data = json.loads(_STATE_FILE.read_text())
        return {
            "mode": data.get("mode", "unknown"),
            "detail": data.get("detail", ""),
            "updated_at": data.get("updated_at", ""),
            "pid": data.get("pid"),
        }
    except Exception:
        return {"mode": "unknown", "detail": "", "updated_at": "", "pid": None}


def _get_last_health_check() -> dict:
    """Parse the last health check result from the log file."""
    result = {"level": "UNKNOWN", "mode": "", "timestamp": "", "age_seconds": None}
    try:
        # Read last 20 lines to find the most recent completion line
        lines = _HEALTH_LOG.read_text().splitlines()[-20:]
        for line in reversed(lines):
            # Match: [timestamp] [INFO] === Health check v3 complete (level=GREEN, mode=active) ===
            m = re.search(
                r"\[(\d{4}-\d{2}-\d{2}T[\d:+]+)\].*complete \(level=(\w+), mode=(\w+)\)",
                line,
            )
            if m:
                ts_str, level, mode = m.groups()
                result["level"] = level
                result["mode"] = mode
                result["timestamp"] = ts_str
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    result["age_seconds"] = (
                        datetime.now(timezone.utc) - ts
                    ).total_seconds()
                except Exception:
                    pass
                break
    except Exception:
        pass
    return result


def _get_scheduled_jobs_count() -> dict:
    """Count scheduled jobs by enabled/disabled status."""
    try:
        data = json.loads(_JOBS_FILE.read_text())
        jobs = data.get("jobs", {})
        # jobs.json stores jobs as a dict keyed by name, or a list
        if isinstance(jobs, dict):
            total = len(jobs)
            # Dict format: each value has job metadata but no "enabled" field
            # All registered jobs are considered enabled
            return {"total": total, "enabled": total, "disabled": 0}
        elif isinstance(jobs, list):
            enabled = sum(1 for j in jobs if j.get("enabled", True))
            return {"total": len(jobs), "enabled": enabled, "disabled": len(jobs) - enabled}
        return {"total": 0, "enabled": 0, "disabled": 0}
    except Exception:
        return {"total": 0, "enabled": 0, "disabled": 0}


def _health_emoji(level: str) -> str:
    """Map health level to emoji indicator."""
    return {
        "GREEN": "\u2705",   # green check
        "YELLOW": "\u26a0\ufe0f",  # warning
        "RED": "\ud83d\udd34",     # red circle
        "BLACK": "\u26ab",         # black circle
    }.get(level.upper(), "\u2753")  # question mark


def _mode_display(mode: str) -> str:
    """Human-readable lifecycle mode."""
    return {
        "active": "Active",
        "starting": "Starting",
        "restarting": "Restarting",
        "hibernate": "Hibernating",
        "backoff": "Backoff (cooling down)",
        "stopped": "Stopped",
        "waking": "Waking up",
    }.get(mode, mode.capitalize())


def generate_report() -> str:
    """Generate the full status report."""
    # Collect all data
    system = collect_system_info()
    queues = collect_message_queues()
    activity = collect_conversation_activity()
    health = collect_health()
    agents = collect_subagent_list()
    tasks = collect_tasks()

    state = _get_lifecycle_state()
    last_hc = _get_last_health_check()
    jobs = _get_scheduled_jobs_count()

    # --- Format the report ---
    lines = []

    # Header: health level + state
    hc_level = last_hc["level"]
    emoji = _health_emoji(hc_level)
    lines.append(f"{emoji} Lobster Status")
    lines.append("")

    # Tier 1: Operational
    mode = _mode_display(state["mode"])
    uptime = _format_duration(system["uptime_seconds"])
    lines.append(f"State: {mode}")
    lines.append(f"System uptime: {uptime}")

    # Health check status
    if last_hc["age_seconds"] is not None:
        hc_ago = _format_duration(last_hc["age_seconds"])
        lines.append(f"Last health check: {hc_level} ({hc_ago} ago)")
    else:
        lines.append(f"Last health check: {hc_level}")

    # Heartbeat
    if health["heartbeat_age_seconds"] is not None:
        hb_ago = _format_duration(health["heartbeat_age_seconds"])
        stale_marker = " (STALE)" if health["heartbeat_stale"] else ""
        lines.append(f"Heartbeat: {hb_ago} ago{stale_marker}")

    lines.append(f"Telegram bot: {'running' if health['telegram_bot_running'] else 'NOT RUNNING'}")
    lines.append("")

    # Tier 3: Message throughput
    lines.append("\U0001f4e8 Messages")
    lines.append(
        f"  Last 1h: {activity['messages_received_1h']} in / {activity['replies_sent_1h']} out"
    )
    lines.append(
        f"  Last 24h: {activity['messages_received_24h']} in / {activity['replies_sent_24h']} out"
    )

    # Queue status
    inbox_count = queues["inbox"]["count"]
    processing_count = queues["processing"]["count"]
    failed_count = queues["failed"]["count"]
    dead_letter_count = queues["dead_letter"]["count"]

    queue_parts = []
    if inbox_count > 0:
        queue_parts.append(f"inbox: {inbox_count}")
    if processing_count > 0:
        queue_parts.append(f"processing: {processing_count}")
    if failed_count > 0:
        queue_parts.append(f"failed: {failed_count}")
    if dead_letter_count > 0:
        queue_parts.append(f"dead-letter: {dead_letter_count}")

    if queue_parts:
        lines.append(f"  Queued: {', '.join(queue_parts)}")
    else:
        lines.append("  Queues: clear")

    if activity["failed_24h"] > 0:
        lines.append(f"  Failed (24h): {activity['failed_24h']}")

    lines.append("")

    # Agents
    lines.append("\U0001f916 Agents")
    pending = agents["pending_count"]
    running_tasks = agents.get("running_tasks", [])
    # Filter to non-stale running tasks
    active_tasks = [t for t in running_tasks if not t.get("stale", True)]
    stale_tasks = [t for t in running_tasks if t.get("stale", True)]

    if pending > 0 or active_tasks:
        agent_parts = []
        if pending > 0:
            agent_parts.append(f"{pending} tracked")
        if active_tasks:
            agent_parts.append(f"{len(active_tasks)} active background tasks")
        lines.append(f"  {', '.join(agent_parts)}")

        # Show brief details for tracked agents
        for a in agents.get("agents", [])[:5]:
            desc = a.get("description", "")[:40]
            status = a.get("status", "unknown")
            elapsed = a.get("elapsed_seconds")
            elapsed_str = f" ({_format_duration(elapsed)})" if elapsed else ""
            if desc:
                lines.append(f"    - {desc} [{status}{elapsed_str}]")
    else:
        lines.append("  No active agents")

    if stale_tasks:
        lines.append(f"  ({len(stale_tasks)} stale background tasks)")

    lines.append("")

    # Tasks
    task_summary = tasks.get("summary", {})
    task_total = task_summary.get("total", 0)
    if task_total > 0:
        lines.append("\U0001f4cb Tasks")
        parts = []
        if task_summary.get("pending", 0):
            parts.append(f"{task_summary['pending']} pending")
        if task_summary.get("in_progress", 0):
            parts.append(f"{task_summary['in_progress']} in progress")
        if task_summary.get("completed", 0):
            parts.append(f"{task_summary['completed']} done")
        lines.append(f"  {' | '.join(parts)}")
        lines.append("")

    # Scheduled jobs
    if jobs["total"] > 0:
        lines.append(f"\u23f0 Scheduled Jobs: {jobs['enabled']} active")
        if jobs["disabled"] > 0:
            lines[-1] += f", {jobs['disabled']} disabled"
        lines.append("")

    # System resources
    lines.append("\U0001f4bb System")
    cpu = system["cpu"]
    mem = system["memory"]
    disk = system["disk"]
    lines.append(
        f"  CPU: {cpu['percent']}% | "
        f"RAM: {mem['percent']}% ({mem['used_mb']} MB / {mem['total_mb']} MB) | "
        f"Disk: {disk['percent']}%"
    )

    # Warnings for resource pressure
    if mem["percent"] > 80:
        lines.append(f"  \u26a0\ufe0f Memory pressure: {mem['percent']}%")
    if disk["percent"] > 90:
        lines.append(f"  \u26a0\ufe0f Disk pressure: {disk['percent']}%")

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_report())
