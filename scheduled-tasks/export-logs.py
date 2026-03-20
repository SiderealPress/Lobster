#!/usr/bin/env python3
"""
Scheduled job: export recent log entries to ~/lobster-workspace/logs/archive/
Runs daily. Copies observations.log, lobster.log, and audit.jsonl snapshots
with date-stamped directory names.

Designed to be the foundation for future remote forwarding once an endpoint
is chosen (see GitHub issue #730).
"""

import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Pure helpers — no side effects
# ---------------------------------------------------------------------------

def resolve_paths() -> dict:
    """Return all relevant paths as an immutable data structure."""
    home = Path.home()
    logs_dir = home / "lobster-workspace" / "logs"
    archive_base = logs_dir / "archive"
    messages_dir = Path(os.environ.get("LOBSTER_MESSAGES", home / "messages"))
    task_outputs_dir = messages_dir / "task-outputs"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "logs_dir": logs_dir,
        "archive_dir": archive_base / today,
        "task_outputs_dir": task_outputs_dir,
        "date_label": today,
        "source_files": [
            logs_dir / "observations.log",
            logs_dir / "lobster.log",
            logs_dir / "audit.jsonl",
        ],
    }


def file_stats(path: Path) -> dict:
    """Return size and line count for a file; empty dict if missing."""
    if not path.exists():
        return {"exists": False, "size_bytes": 0, "line_count": 0}
    size = path.stat().st_size
    try:
        with path.open("rb") as fh:
            line_count = sum(1 for _ in fh)
    except OSError:
        line_count = 0
    return {"exists": True, "size_bytes": size, "line_count": line_count}


def count_recent_errors(path: Path, within_hours: int = 24) -> int:
    """
    Count ERROR-level entries from the past `within_hours`.

    Handles two formats:
    - JSON lines (observations.log, audit.jsonl): checks "level", "severity", "lvl" fields.
    - Plain text: looks for lines containing the literal string "ERROR".

    Returns 0 if the file does not exist or cannot be parsed.
    """
    if not path.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    count = 0

    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    level = (
                        entry.get("level", "")
                        or entry.get("severity", "")
                        or entry.get("lvl", "")
                    ).upper()
                    if level != "ERROR":
                        continue
                    ts_raw = (
                        entry.get("timestamp")
                        or entry.get("ts")
                        or entry.get("time")
                    )
                    if ts_raw:
                        try:
                            ts = datetime.fromisoformat(
                                str(ts_raw).replace("Z", "+00:00")
                            )
                            if ts < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass  # include entry if timestamp is unparseable
                    count += 1
                except (json.JSONDecodeError, ValueError):
                    # Plain-text fallback
                    if "ERROR" in line:
                        count += 1
    except OSError:
        pass

    return count


def copy_to_archive(source: Path, dest_dir: Path) -> dict:
    """
    Copy source file to dest_dir and return a structured result dict.
    Never raises; errors are captured in the result.
    """
    if not source.exists():
        return {
            "file": source.name,
            "status": "skipped",
            "reason": "source not found",
        }
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    try:
        shutil.copy2(str(source), str(dest))
        stats = file_stats(dest)
        return {
            "file": source.name,
            "status": "copied",
            "dest": str(dest),
            "size_bytes": stats["size_bytes"],
            "line_count": stats["line_count"],
        }
    except OSError as exc:
        return {"file": source.name, "status": "error", "reason": str(exc)}


def build_summary(paths: dict, copy_results: list, error_counts: dict) -> str:
    """
    Compose a human-readable summary string from structured data.
    Pure function: no I/O.
    """
    lines = [
        f"Log export complete for {paths['date_label']}",
        f"Archive: {paths['archive_dir']}",
        "",
        "Files:",
    ]

    for result in copy_results:
        name = result["file"]
        status = result["status"]
        if status == "copied":
            size_kb = result["size_bytes"] / 1024
            line_count = result["line_count"]
            errors_24h = error_counts.get(name, 0)
            error_note = f", {errors_24h} ERROR(s) in past 24h" if errors_24h else ""
            lines.append(
                f"  {name}: {line_count} lines, {size_kb:.1f} KB{error_note}"
            )
        elif status == "skipped":
            lines.append(f"  {name}: skipped ({result.get('reason', 'unknown')})")
        else:
            lines.append(
                f"  {name}: FAILED — {result.get('reason', 'unknown')}"
            )

    total_errors = sum(error_counts.values())
    lines.append("")
    if total_errors:
        lines.append(
            f"Total ERROR entries in past 24h: {total_errors}"
        )
    else:
        lines.append("No ERROR entries in past 24h.")

    lines.append("")
    lines.append(
        "Remote forwarding not yet configured (see issue #730). "
        "Add an endpoint and extend this script to push archived files."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Side-effecting boundary functions
# ---------------------------------------------------------------------------

def write_task_output(task_outputs_dir: Path, job_name: str, summary: str, status: str) -> None:
    """
    Write job output to the task-outputs directory in the same format as the
    write_task_output MCP tool, so the dispatcher can pick it up via
    check_task_outputs.
    """
    task_outputs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d-%H%M%S")
    output_data = {
        "job_name": job_name,
        "timestamp": now.isoformat(),
        "status": status,
        "output": summary,
    }
    output_file = task_outputs_dir / f"{timestamp_str}-{job_name}.json"
    with open(output_file, "w") as fh:
        json.dump(output_data, fh, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    paths = resolve_paths()

    copy_results = [
        copy_to_archive(src, paths["archive_dir"])
        for src in paths["source_files"]
    ]

    error_counts = {
        src.name: count_recent_errors(src)
        for src in paths["source_files"]
    }

    any_failure = any(r["status"] == "error" for r in copy_results)
    status = "failed" if any_failure else "success"

    summary = build_summary(paths, copy_results, error_counts)

    # Write to task-outputs so dispatcher can see it via check_task_outputs
    write_task_output(
        paths["task_outputs_dir"],
        job_name="export-logs",
        summary=summary,
        status=status,
    )

    # Also print to stdout for cron log capture
    print(summary)

    return 1 if any_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
