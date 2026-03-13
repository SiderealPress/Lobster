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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


# --- Directories -----------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))
_USER_CONFIG = Path(os.environ.get("LOBSTER_USER_CONFIG", _HOME / "lobster-user-config"))
_LOBSTER_SRC = Path(os.environ.get("LOBSTER_SRC", _HOME / "lobster"))
_SCHEDULED_TASKS = _LOBSTER_SRC / "scheduled-tasks" / "tasks"
_MEMORY_DB = _WORKSPACE / "data" / "memory.db"
_PENDING_AGENTS_FILE = _MESSAGES / "config" / "pending-agents.json"
_TASK_OUTPUTS_DIR = Path(f"/tmp/claude-1000/-home-admin-lobster-workspace/tasks")
_MEMORY_CANONICAL_DIR = _USER_CONFIG / "memory" / "canonical"

# Cache for parsed JSONL stats: maps (path_str, mtime_float) -> stats dict
_JSONL_CACHE: dict[tuple[str, float], dict] = {}

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
        ("workspace/logs", _WORKSPACE / "logs"),
        ("user-config/memory", _USER_CONFIG / "memory"),
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
            if "lobster_bot" in cmdline or ("telegram" in cmdline.lower() and "bot" in cmdline.lower()):
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
# Collector: Subagent List
# ---------------------------------------------------------------------------

def _parse_jsonl_task(path: Path) -> dict:
    """Parse a JSONL task output file and return runtime statistics.

    Caches results by (path, mtime) to avoid re-parsing unchanged files.
    Returns a dict with keys: agent_id, turns, input_tokens, output_tokens,
    tool_uses, top_tools, first_timestamp, last_timestamp, last_activity_seconds_ago, stale.
    """
    global _JSONL_CACHE
    try:
        stat = path.stat()
        mtime = stat.st_mtime
    except OSError:
        return {}

    cache_key = (str(path), mtime)
    if cache_key in _JSONL_CACHE:
        # Re-compute time-sensitive fields from cached base data
        cached = _JSONL_CACHE[cache_key].copy()
        last_ts = cached.get("_last_ts_epoch")
        if last_ts is not None:
            ago = int(time.time() - last_ts)
            cached["last_activity_seconds_ago"] = ago
            cached["stale"] = ago > 120
        return {k: v for k, v in cached.items() if not k.startswith("_")}

    turns = 0
    input_tokens = 0
    output_tokens = 0
    tool_use_counts: Counter = Counter()
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    last_ts_epoch: float | None = None
    agent_id: str | None = None

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                ts_str: str | None = obj.get("timestamp")
                if ts_str:
                    if first_timestamp is None:
                        first_timestamp = ts_str
                    last_timestamp = ts_str
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        last_ts_epoch = dt.timestamp()
                    except ValueError:
                        pass

                if agent_id is None:
                    agent_id = obj.get("agentId")

                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue

                role = msg.get("role")
                if role == "assistant":
                    turns += 1
                    usage = msg.get("usage") or {}
                    input_tokens += usage.get("input_tokens", 0) or 0
                    input_tokens += usage.get("cache_read_input_tokens", 0) or 0
                    input_tokens += usage.get("cache_creation_input_tokens", 0) or 0
                    output_tokens += usage.get("output_tokens", 0) or 0

                    content = msg.get("content") or []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_use_counts[tool_name] += 1
    except OSError:
        return {}

    top_tools = dict(tool_use_counts.most_common(5))
    total_tool_uses = sum(tool_use_counts.values())

    now = time.time()
    last_activity_ago: int | None = None
    stale = False
    if last_ts_epoch is not None:
        last_activity_ago = int(now - last_ts_epoch)
        stale = last_activity_ago > 120

    result = {
        "agent_id": agent_id or "",
        "turns": turns,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_uses": total_tool_uses,
        "top_tools": top_tools,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "last_activity_seconds_ago": last_activity_ago,
        "stale": stale,
    }

    # Store epoch privately for cache refresh of time-sensitive fields
    cacheable = result.copy()
    cacheable["_last_ts_epoch"] = last_ts_epoch

    # Evict stale entries to keep cache bounded (~100 entries max)
    if len(_JSONL_CACHE) > 100:
        _JSONL_CACHE.clear()
    _JSONL_CACHE[cache_key] = cacheable

    return result


def _collect_running_tasks() -> list[dict]:
    """Scan the task output directory and return runtime stats for all .output files."""
    if not _TASK_OUTPUTS_DIR.is_dir():
        return []
    tasks = []
    for output_file in _TASK_OUTPUTS_DIR.iterdir():
        if output_file.suffix == ".output":
            stats = _parse_jsonl_task(output_file)
            if stats:
                stats["file"] = output_file.name
                tasks.append(stats)
    return tasks


def _iso_to_epoch(iso_str: str) -> float | None:
    """Convert an ISO 8601 string to a Unix epoch float. Returns None on failure."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


def collect_subagent_list() -> dict:
    """Collect the list of pending Lobster agents and their runtime stats.

    Reads the pending-agents.json file for declared agents and correlates
    each with its JSONL task output file by matching start-time (within a
    10-second window).

    Returns a dict with keys: pending_count, agents, running_tasks.
    """
    # 1. Read pending agents
    pending_agents: list[dict] = []
    if _PENDING_AGENTS_FILE.is_file():
        raw = _read_json(_PENDING_AGENTS_FILE)
        if isinstance(raw, dict):
            pending_agents = raw.get("agents", [])

    # 2. Collect runtime stats for all task output files
    running_tasks = _collect_running_tasks()

    # 3. Match pending agents to runtime tasks by start-time proximity
    now = time.time()
    matched_files: set[str] = set()
    enriched_agents = []

    for agent in pending_agents:
        started_at = agent.get("started_at", "")
        agent_epoch = _iso_to_epoch(started_at) if started_at else None

        # Find the best-matching runtime task within a 10-second window
        best_runtime: dict | None = None
        if agent_epoch is not None:
            for task in running_tasks:
                first_ts = task.get("first_timestamp")
                if first_ts is None:
                    continue
                task_epoch = _iso_to_epoch(first_ts)
                if task_epoch is None:
                    continue
                if abs(task_epoch - agent_epoch) <= 10.0:
                    best_runtime = task
                    matched_files.add(task.get("file", ""))
                    break

        elapsed = int(now - agent_epoch) if agent_epoch is not None else None

        # Determine status from runtime data
        status = "unknown"
        if best_runtime is not None:
            ago = best_runtime.get("last_activity_seconds_ago")
            if ago is not None:
                status = "stale" if best_runtime.get("stale") else "running"
        elif elapsed is not None:
            status = "running"

        enriched_agents.append({
            "id": agent.get("id", ""),
            "description": agent.get("description", ""),
            "chat_id": agent.get("chat_id"),
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "status": status,
            "runtime": best_runtime,
        })

    # Include unmatched running tasks (no pending agent entry)
    unmatched_tasks = [t for t in running_tasks if t.get("file", "") not in matched_files]

    return {
        "pending_count": len(pending_agents),
        "agents": enriched_agents,
        "running_tasks": unmatched_tasks,
    }


# ---------------------------------------------------------------------------
# Collector: Memory Stats
# ---------------------------------------------------------------------------

def collect_memory_stats() -> dict:
    """Collect memory system statistics from SQLite and canonical markdown files.

    Queries the events table for totals, type breakdowns, project list, and
    recent events (with tags from the metadata JSON column). Also enumerates
    canonical memory files.

    Returns a dict with keys: total_events, unconsolidated_count,
    event_type_counts, projects, recent_events, consolidations.
    """
    total_events = 0
    unconsolidated_count = 0
    event_type_counts: dict[str, int] = {}
    projects: list[str] = []
    recent_events: list[dict] = []

    if _MEMORY_DB.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(str(_MEMORY_DB))
            conn.row_factory = sqlite3.Row

            # Aggregate counts
            row = conn.execute("SELECT COUNT(*) as n FROM events").fetchone()
            total_events = row["n"] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) as n FROM events WHERE consolidated = 0 OR consolidated IS NULL"
            ).fetchone()
            unconsolidated_count = row["n"] if row else 0

            # Type breakdown
            for row in conn.execute(
                "SELECT type, COUNT(*) as cnt FROM events GROUP BY type ORDER BY cnt DESC"
            ).fetchall():
                event_type_counts[row["type"] or "unknown"] = row["cnt"]

            # Distinct projects (non-null)
            for row in conn.execute(
                "SELECT DISTINCT project FROM events WHERE project IS NOT NULL AND project != '' ORDER BY project"
            ).fetchall():
                projects.append(row["project"])

            # Recent events with tags
            cursor = conn.execute(
                """
                SELECT id, timestamp, type, source, project, content, metadata, consolidated
                FROM events
                ORDER BY timestamp DESC
                LIMIT 10
                """
            )
            for row in cursor.fetchall():
                # Extract tags from metadata JSON
                tags: list[str] = []
                metadata_raw = row["metadata"]
                if metadata_raw:
                    try:
                        meta = json.loads(metadata_raw)
                        if isinstance(meta, dict):
                            tags = meta.get("tags", []) or []
                    except (json.JSONDecodeError, TypeError):
                        pass

                recent_events.append({
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "type": row["type"],
                    "source": row["source"],
                    "project": row["project"],
                    "content": (row["content"] or "")[:300],
                    "tags": tags,
                    "consolidated": bool(row["consolidated"]),
                })

            conn.close()
        except Exception:
            pass

    # Canonical memory files
    canonical_files: list[dict] = []
    last_consolidation_at: str | None = None
    latest_mtime: float = 0.0

    if _MEMORY_CANONICAL_DIR.is_dir():
        for md_file in sorted(_MEMORY_CANONICAL_DIR.rglob("*.md")):
            try:
                stat = md_file.stat()
                mtime = stat.st_mtime
                modified_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                # Compute relative path from user-config root for display
                try:
                    rel_path = str(md_file.relative_to(_USER_CONFIG))
                except ValueError:
                    rel_path = str(md_file)

                canonical_files.append({
                    "name": md_file.name,
                    "path": rel_path,
                    "modified": modified_iso,
                    "size_bytes": stat.st_size,
                })

                if mtime > latest_mtime:
                    latest_mtime = mtime
                    last_consolidation_at = modified_iso
            except OSError:
                continue

    return {
        "total_events": total_events,
        "unconsolidated_count": unconsolidated_count,
        "event_type_counts": event_type_counts,
        "projects": projects,
        "recent_events": recent_events,
        "consolidations": {
            "last_consolidation_at": last_consolidation_at,
            "canonical_files": canonical_files,
        },
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
        "subagent_list": collect_subagent_list(),
        "memory": collect_memory_stats(),
        "conversation_activity": collect_conversation_activity(),
        "filesystem": collect_filesystem_overview(),
        "health": collect_health(),
    }
