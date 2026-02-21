"""
Data collectors for the Lobster Dashboard.

Each collector gathers a specific category of system/Lobster information
and returns it as a plain dict suitable for JSON serialization.
"""

import json
import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


# --- Directories -----------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))
_LOBSTER_SRC = Path(os.environ.get("LOBSTER_SRC", _HOME / "lobster"))
_SCHEDULED_TASKS = _LOBSTER_SRC / "scheduled-tasks" / "tasks"
_MEMORY_DB = _WORKSPACE / "data" / "memory.db"

INBOX_DIR = _MESSAGES / "inbox"
OUTBOX_DIR = _MESSAGES / "outbox"
PROCESSED_DIR = _MESSAGES / "processed"
PROCESSING_DIR = _MESSAGES / "processing"
FAILED_DIR = _MESSAGES / "failed"
DEAD_LETTER_DIR = _MESSAGES / "dead-letter"
SENT_DIR = _MESSAGES / "sent"
TASK_OUTPUTS_DIR = _MESSAGES / "task-outputs"
TASKS_FILE = _MESSAGES / "tasks.json"


def _count_files(directory: Path) -> int:
    """Count JSON files in a directory (non-recursive)."""
    if not directory.is_dir():
        return 0
    return sum(1 for f in directory.iterdir() if f.suffix == ".json")


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file, returning None on failure."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _recent_files(directory: Path, limit: int = 10) -> list[dict]:
    """Return the most recent JSON files in a directory, newest first."""
    if not directory.is_dir():
        return []
    files = sorted(
        (f for f in directory.iterdir() if f.suffix == ".json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    results = []
    for f in files[:limit]:
        data = _read_json(f)
        if data is not None:
            results.append(data)
    return results


# ---------------------------------------------------------------------------
# Collector: System Info
# ---------------------------------------------------------------------------

def collect_system_info() -> dict:
    """Collect host-level system information."""
    boot_time = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    cpu_percent = psutil.cpu_percent(interval=0)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    return {
        "hostname": platform.node(),
        "platform": platform.system(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "boot_time": datetime.fromtimestamp(boot_time, tz=timezone.utc).isoformat(),
        "uptime_seconds": int(uptime_secs),
        "cpu": {
            "count": psutil.cpu_count(),
            "percent": cpu_percent,
            "load_avg": list(os.getloadavg()),
        },
        "memory": {
            "total_mb": round(mem.total / (1024 * 1024)),
            "used_mb": round(mem.used / (1024 * 1024)),
            "available_mb": round(mem.available / (1024 * 1024)),
            "percent": mem.percent,
        },
        "disk": {
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round(disk.used / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
            "percent": round(disk.percent, 1),
        },
    }


# ---------------------------------------------------------------------------
# Collector: Claude Code Sessions
# ---------------------------------------------------------------------------

def collect_sessions() -> list[dict]:
    """Detect running Claude Code (claude) processes."""
    sessions = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            cmdline = info.get("cmdline") or []
            name = info.get("name", "")

            # Claude Code appears as a node process with 'claude' in the command
            cmdline_str = " ".join(cmdline)
            if "claude" in name.lower() or "claude" in cmdline_str.lower():
                # Filter out things that are clearly not Claude Code sessions
                if any(skip in cmdline_str for skip in ["chrome", "chromium", "electron"]):
                    continue
                mem_info = info.get("memory_info")
                sessions.append({
                    "pid": info["pid"],
                    "name": name,
                    "cmdline": cmdline_str[:200],  # truncate long cmdlines
                    "started": datetime.fromtimestamp(
                        info["create_time"], tz=timezone.utc
                    ).isoformat() if info.get("create_time") else None,
                    "cpu_percent": info.get("cpu_percent", 0),
                    "memory_mb": round(mem_info.rss / (1024 * 1024), 1) if mem_info else 0,
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sessions


# ---------------------------------------------------------------------------
# Collector: Message Queues
# ---------------------------------------------------------------------------

def collect_message_queues() -> dict:
    """Collect message queue counts and recent messages."""
    return {
        "inbox": {
            "count": _count_files(INBOX_DIR),
            "recent": _recent_files(INBOX_DIR, limit=5),
        },
        "processing": {
            "count": _count_files(PROCESSING_DIR),
        },
        "processed": {
            "count": _count_files(PROCESSED_DIR),
        },
        "sent": {
            "count": _count_files(SENT_DIR),
        },
        "outbox": {
            "count": _count_files(OUTBOX_DIR),
        },
        "failed": {
            "count": _count_files(FAILED_DIR),
        },
        "dead_letter": {
            "count": _count_files(DEAD_LETTER_DIR),
        },
    }


# ---------------------------------------------------------------------------
# Collector: Tasks
# ---------------------------------------------------------------------------

def collect_tasks() -> dict:
    """Read the tasks.json file and return task info."""
    data = _read_json(TASKS_FILE)
    if data is None:
        return {"tasks": [], "next_id": 0}
    tasks = data.get("tasks", [])
    return {
        "tasks": tasks,
        "next_id": data.get("next_id", 0),
        "summary": {
            "total": len(tasks),
            "pending": sum(1 for t in tasks if t.get("status") == "pending"),
            "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
            "completed": sum(1 for t in tasks if t.get("status") == "completed"),
        },
    }


# ---------------------------------------------------------------------------
# Collector: Scheduled Jobs
# ---------------------------------------------------------------------------

def collect_scheduled_jobs() -> list[dict]:
    """List scheduled job definitions."""
    jobs = []
    if not _SCHEDULED_TASKS.is_dir():
        return jobs
    for f in sorted(_SCHEDULED_TASKS.iterdir()):
        if f.suffix == ".md":
            jobs.append({
                "name": f.stem,
                "file": str(f),
                "size_bytes": f.stat().st_size,
                "modified": datetime.fromtimestamp(
                    f.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            })
    return jobs


# ---------------------------------------------------------------------------
# Collector: Task Outputs (recent)
# ---------------------------------------------------------------------------

def collect_task_outputs(limit: int = 10) -> list[dict]:
    """Return the most recent task output files."""
    return _recent_files(TASK_OUTPUTS_DIR, limit=limit)


# ---------------------------------------------------------------------------
# Collector: Memory Events (recent, from SQLite)
# ---------------------------------------------------------------------------

def collect_recent_memory(hours: int = 24, limit: int = 20) -> list[dict]:
    """Query recent memory events from the SQLite database."""
    if not _MEMORY_DB.is_file():
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(str(_MEMORY_DB))
        conn.row_factory = sqlite3.Row
        cutoff = datetime.now(tz=timezone.utc).timestamp() - (hours * 3600)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        cursor = conn.execute(
            """
            SELECT id, timestamp, type, source, project, content, metadata, consolidated
            FROM events
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (cutoff_iso, limit),
        )
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "type": row["type"],
                "source": row["source"],
                "project": row["project"],
                "content": row["content"][:300],  # truncate for dashboard
                "consolidated": bool(row["consolidated"]),
            }
            for row in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Collector: Conversation Activity
# ---------------------------------------------------------------------------

def collect_conversation_activity() -> dict:
    """Compute conversation activity metrics."""
    now = time.time()
    one_hour_ago = now - 3600
    one_day_ago = now - 86400

    def count_since(directory: Path, since: float) -> int:
        if not directory.is_dir():
            return 0
        return sum(
            1 for f in directory.iterdir()
            if f.suffix == ".json" and f.stat().st_mtime >= since
        )

    return {
        "messages_received_1h": count_since(PROCESSED_DIR, one_hour_ago) + count_since(INBOX_DIR, one_hour_ago),
        "messages_received_24h": count_since(PROCESSED_DIR, one_day_ago) + count_since(INBOX_DIR, one_day_ago),
        "replies_sent_1h": count_since(SENT_DIR, one_hour_ago),
        "replies_sent_24h": count_since(SENT_DIR, one_day_ago),
        "failed_1h": count_since(FAILED_DIR, one_hour_ago),
        "failed_24h": count_since(FAILED_DIR, one_day_ago),
    }


# ---------------------------------------------------------------------------
# Collector: File System Overview
# ---------------------------------------------------------------------------

def collect_filesystem_overview() -> list[dict]:
    """Report on key Lobster directories and their sizes."""
    dirs_to_check = [
        ("messages/inbox", INBOX_DIR),
        ("messages/outbox", OUTBOX_DIR),
        ("messages/processed", PROCESSED_DIR),
        ("messages/processing", PROCESSING_DIR),
        ("messages/sent", SENT_DIR),
        ("messages/failed", FAILED_DIR),
        ("messages/dead-letter", DEAD_LETTER_DIR),
        ("messages/task-outputs", TASK_OUTPUTS_DIR),
        ("lobster/src", _LOBSTER_SRC / "src"),
        ("lobster/scheduled-tasks", _LOBSTER_SRC / "scheduled-tasks"),
        ("lobster/scripts", _LOBSTER_SRC / "scripts"),
        ("workspace/data", _WORKSPACE / "data"),
        ("workspace/memory", _WORKSPACE / "memory"),
        ("workspace/logs", _WORKSPACE / "logs"),
    ]
    result = []
    for label, path in dirs_to_check:
        if path.is_dir():
            file_count = sum(1 for _ in path.iterdir())
            result.append({
                "path": label,
                "absolute_path": str(path),
                "file_count": file_count,
                "exists": True,
            })
        else:
            result.append({
                "path": label,
                "absolute_path": str(path),
                "file_count": 0,
                "exists": False,
            })
    return result


# ---------------------------------------------------------------------------
# Collector: Heartbeat / Health
# ---------------------------------------------------------------------------

def collect_health() -> dict:
    """Check Lobster health indicators."""
    heartbeat_file = _WORKSPACE / "logs" / "claude-heartbeat"
    heartbeat_age = None
    if heartbeat_file.is_file():
        heartbeat_age = int(time.time() - heartbeat_file.stat().st_mtime)

    # Check if the Telegram bot process is running
    telegram_bot_running = False
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "telegram" in cmdline.lower() and "bot" in cmdline.lower():
                telegram_bot_running = True
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat_stale": heartbeat_age is not None and heartbeat_age > 300,
        "telegram_bot_running": telegram_bot_running,
    }


# ---------------------------------------------------------------------------
# Full Snapshot
# ---------------------------------------------------------------------------

def collect_full_snapshot() -> dict:
    """Gather all collectors into a single snapshot payload."""
    return {
        "system": collect_system_info(),
        "sessions": collect_sessions(),
        "message_queues": collect_message_queues(),
        "tasks": collect_tasks(),
        "scheduled_jobs": collect_scheduled_jobs(),
        "task_outputs": collect_task_outputs(limit=5),
        "recent_memory": collect_recent_memory(hours=24, limit=10),
        "conversation_activity": collect_conversation_activity(),
        "filesystem": collect_filesystem_overview(),
        "health": collect_health(),
    }
