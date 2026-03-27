"""
Activity Rhythm: track hourly/daily message patterns.

Builds a model of when the user is most active, most responsive, and most engaged.
This is used by the inference engine to adjust attention scoring and response
style hints based on time-of-day context.

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime
from typing import Any

from .db import get_activity_rhythm, get_peak_activity_hours, update_activity_rhythm


# ---------------------------------------------------------------------------
# Rhythm update (called on every observed message)
# ---------------------------------------------------------------------------

def record_message_rhythm(
    conn: sqlite3.Connection,
    message_ts: datetime,
    message_length: int,
    latency_ms: int | None = None,
) -> None:
    """
    Update the activity rhythm for this message's hour and day of week.
    Fast, O(1) per call. Should be called from observe_message().
    """
    hour = message_ts.hour
    day = message_ts.weekday()  # 0=Monday, 6=Sunday
    update_activity_rhythm(conn, hour, day, message_length, latency_ms)


# ---------------------------------------------------------------------------
# Rhythm analysis
# ---------------------------------------------------------------------------

def get_current_activity_level(
    conn: sqlite3.Connection,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Return the expected activity level for the current hour/day.
    Returns a dict with: message_count, avg_length, avg_latency, relative_level.

    relative_level: 'very_high' | 'high' | 'normal' | 'low' | 'unknown'
    """
    now = now or datetime.utcnow()
    hour = now.hour
    day = now.weekday()

    rhythm = get_activity_rhythm(conn)
    if not rhythm:
        return {"relative_level": "unknown", "message_count": 0}

    # Find the entry for this hour/day
    current = next(
        (r for r in rhythm if r.hour_of_day == hour and r.day_of_week == day),
        None,
    )

    if not current:
        return {"relative_level": "unknown", "message_count": 0}

    # Compute average activity across all slots for comparison
    total_count = sum(r.message_count for r in rhythm)
    avg_count = total_count / len(rhythm) if rhythm else 0

    # Relative level
    ratio = current.message_count / avg_count if avg_count > 0 else 0
    if ratio > 2.0:
        level = "very_high"
    elif ratio > 1.5:
        level = "high"
    elif ratio > 0.5:
        level = "normal"
    else:
        level = "low"

    avg_length = current.total_length / current.message_count if current.message_count > 0 else 0
    avg_latency = (
        current.total_latency / current.latency_count
        if current.latency_count > 0
        else None
    )

    return {
        "hour": hour,
        "day_of_week": day,
        "message_count": current.message_count,
        "avg_length": round(avg_length),
        "avg_latency_ms": round(avg_latency) if avg_latency else None,
        "relative_level": level,
        "ratio_vs_avg": round(ratio, 2),
    }


def get_active_hours_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a summary of peak activity hours for context injection."""
    peak_hours = get_peak_activity_hours(conn, top_n=3)
    if not peak_hours:
        return {"peak_hours": [], "description": "insufficient data"}

    def _hour_label(h: int) -> str:
        suffix = "am" if h < 12 else "pm"
        display = h if h <= 12 else h - 12
        display = 12 if display == 0 else display
        return f"{display}{suffix}"

    labels = [_hour_label(h) for h in peak_hours]
    return {
        "peak_hours": peak_hours,
        "description": f"Most active at {', '.join(labels)}",
    }


def format_rhythm_markdown(conn: sqlite3.Connection) -> str:
    """Format activity rhythm as a simple markdown summary."""
    summary = get_active_hours_summary(conn)
    current = get_current_activity_level(conn)

    lines = ["# Activity Rhythm\n"]
    lines.append(f"- **Peak hours:** {summary.get('description', 'unknown')}")
    lines.append(f"- **Current slot activity:** {current.get('relative_level', 'unknown')}")

    if current.get("avg_latency_ms"):
        avg_sec = current["avg_latency_ms"] / 1000
        lines.append(f"- **Typical response latency (this hour):** {avg_sec:.0f}s")

    return "\n".join(lines)
