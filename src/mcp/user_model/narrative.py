"""
Narrative Arc Tracking: manage life storylines bridged from projects and conversations.

Arcs are the user model's representation of ongoing projects, life themes, and
tracked storylines. They are primarily populated by the bridges module (from
canonical memory project files) and enriched by observation signals.

Depends on: schema.py, db.py only.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import (
    get_active_narrative_arcs,
    get_recent_observations,
    upsert_narrative_arc,
)
from .schema import NarrativeArc


def create_arc(
    conn: sqlite3.Connection,
    title: str,
    description: str,
    themes: list[str] | None = None,
    status: str = "active",
) -> str:
    """Create a new narrative arc and return its ID."""
    arc = NarrativeArc(
        id=None,
        title=title,
        description=description,
        themes=themes or [],
        status=status,
        started_at=datetime.now(timezone.utc),
        last_updated=datetime.now(timezone.utc),
    )
    return upsert_narrative_arc(conn, arc)


def update_arc(
    conn: sqlite3.Connection,
    arc_id: str,
    description: str | None = None,
    status: str | None = None,
    resolution: str | None = None,
) -> None:
    """Update an existing narrative arc."""
    import json
    rows = conn.execute(
        "SELECT * FROM um_narrative_arcs WHERE id = ?", (arc_id,)
    ).fetchone()
    if not rows:
        return
    arc = NarrativeArc(
        id=rows["id"],
        title=rows["title"],
        description=description or rows["description"],
        themes=json.loads(rows["themes"]),
        status=status or rows["status"],
        started_at=datetime.fromisoformat(rows["started_at"]),
        last_updated=datetime.now(timezone.utc),
        resolution=resolution or rows["resolution"],
    )
    upsert_narrative_arc(conn, arc)


def refresh_arcs_from_observations(
    conn: sqlite3.Connection,
    hours: int = 24,
) -> dict[str, Any]:
    """
    Scan recent observations for topic signals that match existing arcs.
    When a topic mention aligns with an arc's title or themes, update the
    arc's last_updated timestamp (keeping it "warm").

    Also detects arcs that haven't been mentioned in a while and marks them
    as potentially paused.

    Returns summary of arc activity.
    """
    arcs = get_active_narrative_arcs(conn)
    if not arcs:
        return {"active": 0, "warmed": 0, "cooled": 0}

    # Get recent topic observations
    topic_obs = get_recent_observations(conn, hours=hours, signal_type="topic", limit=100)
    recent_topics = {obs.content.lower() for obs in topic_obs}
    # Also check message contexts for topic keywords
    for obs in topic_obs:
        if obs.context:
            recent_topics.add(obs.context.lower())

    warmed = 0
    cooled = 0
    now = datetime.now(timezone.utc)

    for arc in arcs:
        # Build keyword set from arc title and themes
        arc_keywords = set()
        for word in re.split(r'[\s\-_/]+', arc.title.lower()):
            if len(word) > 2:
                arc_keywords.add(word)
        for theme in arc.themes:
            for word in re.split(r'[\s\-_/]+', theme.lower()):
                if len(word) > 2:
                    arc_keywords.add(word)

        # Check if any recent topics mention this arc
        matched = arc_keywords & recent_topics
        if matched:
            # Warm the arc — update last_updated
            update_arc(conn, arc.id, status="active")
            warmed += 1
        elif (now - arc.last_updated) > timedelta(days=14):
            # Arc hasn't been mentioned in 2 weeks — mark as cooling
            if arc.status == "active":
                update_arc(conn, arc.id, status="paused")
                cooled += 1

    return {"active": len(arcs), "warmed": warmed, "cooled": cooled}


def get_arc_for_topic(
    conn: sqlite3.Connection,
    topic: str,
) -> NarrativeArc | None:
    """Find the best-matching active arc for a given topic string."""
    arcs = get_active_narrative_arcs(conn)
    topic_lower = topic.lower()

    best_match: NarrativeArc | None = None
    best_score = 0

    for arc in arcs:
        score = 0
        # Title word match
        for word in re.split(r'[\s\-_/]+', arc.title.lower()):
            if len(word) > 2 and word in topic_lower:
                score += 2
        # Theme match
        for theme in arc.themes:
            if theme.lower() in topic_lower:
                score += 1

        if score > best_score:
            best_score = score
            best_match = arc

    return best_match if best_score > 0 else None


def format_active_arcs_markdown(conn: sqlite3.Connection) -> str:
    """Format active narrative arcs as markdown for the file layer."""
    arcs = get_active_narrative_arcs(conn)
    lines = ["# Active Narrative Arcs\n"]

    if not arcs:
        lines.append("*No active arcs tracked yet.*")
        return "\n".join(lines)

    now = datetime.now(timezone.utc)
    for arc in arcs:
        themes_str = ", ".join(arc.themes) if arc.themes else "none"
        staleness = (now - arc.last_updated).days

        lines.append(f"## {arc.title}")
        lines.append(f"- **Status:** {arc.status}")
        lines.append(f"- **Themes:** {themes_str}")
        lines.append(f"- **Started:** {arc.started_at.strftime('%Y-%m-%d')}")
        lines.append(f"- **Last active:** {arc.last_updated.strftime('%Y-%m-%d')} ({staleness}d ago)")
        if arc.description:
            lines.append(f"\n{arc.description}")
        if arc.resolution:
            lines.append(f"\n*Resolution: {arc.resolution}*")
        lines.append("")

    return "\n".join(lines)
