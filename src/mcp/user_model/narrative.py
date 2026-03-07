"""
Narrative Arc Tracking: detect and manage life storylines in user conversations.

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime
from typing import Any

from .db import get_active_narrative_arcs, upsert_narrative_arc
from .schema import NarrativeArc


def create_arc(
    conn: sqlite3.Connection,
    title: str,
    description: str,
    themes: list[str] | None = None,
) -> str:
    """Create a new narrative arc and return its ID."""
    arc = NarrativeArc(
        id=None,
        title=title,
        description=description,
        themes=themes or [],
        status="active",
        started_at=datetime.utcnow(),
        last_updated=datetime.utcnow(),
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
    rows = conn.execute(
        "SELECT * FROM um_narrative_arcs WHERE id = ?", (arc_id,)
    ).fetchone()
    if not rows:
        return
    import json
    arc = NarrativeArc(
        id=rows["id"],
        title=rows["title"],
        description=description or rows["description"],
        themes=json.loads(rows["themes"]),
        status=status or rows["status"],
        started_at=datetime.fromisoformat(rows["started_at"]),
        last_updated=datetime.utcnow(),
        resolution=resolution or rows["resolution"],
    )
    upsert_narrative_arc(conn, arc)


def format_active_arcs_markdown(conn: sqlite3.Connection) -> str:
    """Format active narrative arcs as markdown for the file layer."""
    arcs = get_active_narrative_arcs(conn)
    lines = ["# Active Narrative Arcs\n"]

    if not arcs:
        lines.append("*No active arcs tracked yet.*")
        return "\n".join(lines)

    for arc in arcs:
        themes_str = ", ".join(arc.themes) if arc.themes else "none"
        lines.append(f"## {arc.title}")
        lines.append(f"- **Status:** {arc.status}")
        lines.append(f"- **Themes:** {themes_str}")
        lines.append(f"- **Started:** {arc.started_at.strftime('%Y-%m-%d')}")
        lines.append(f"- **Last updated:** {arc.last_updated.strftime('%Y-%m-%d')}")
        if arc.description:
            lines.append(f"\n{arc.description}")
        lines.append("")

    return "\n".join(lines)
