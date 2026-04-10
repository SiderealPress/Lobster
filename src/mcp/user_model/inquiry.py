"""
Active Inquiry: budget-constrained question generation woven into conversation.

Generates clarifying questions when the model has gaps or uncertainty.
Questions are budget-constrained (max 1 per 24h) to avoid making the user
feel interrogated. The single question is chosen to maximize model learning.

Sources for questions (priority order):
1. Unresolved contradictions — "You said X but also Y, which matters more?"
2. Low-confidence inferred preferences — "I think you prefer X, is that right?"
3. Stale blind spots — patterns detected but never surfaced
4. Fading narrative arcs — projects going cold without explicit pause

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import (
    get_active_contradictions,
    get_active_narrative_arcs,
    get_all_preference_nodes,
    get_blind_spots,
    get_emotional_baseline,
)
from .schema import NodeSource, NodeType


# Default: at most 1 question every 24 hours
_DEFAULT_BUDGET_HOURS = 24


def should_ask_question(
    conn: sqlite3.Connection,
    budget_hours: int = _DEFAULT_BUDGET_HOURS,
) -> bool:
    """Check if the inquiry budget allows asking a question right now."""
    row = conn.execute(
        "SELECT value FROM um_metadata WHERE key = 'last_inquiry_at'"
    ).fetchone()
    if not row:
        return True
    last_inquiry = datetime.fromisoformat(row["value"])
    # Rows written before the tz-aware migration are naive; attach UTC so the
    # subtraction below never raises TypeError on an upgraded installation.
    if last_inquiry.tzinfo is None:
        last_inquiry = last_inquiry.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_inquiry > timedelta(hours=budget_hours)


def record_inquiry(conn: sqlite3.Connection) -> None:
    """Record that a question was asked (update budget tracker)."""
    conn.execute(
        "INSERT OR REPLACE INTO um_metadata (key, value) VALUES ('last_inquiry_at', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()


def _pick_contradiction_question(conn: sqlite3.Connection) -> str | None:
    """Generate a question from unresolved contradictions."""
    contradictions = get_active_contradictions(conn)
    unresolved = [c for c in contradictions if not c.resolved]
    if not unresolved:
        return None

    # Pick highest tension
    target = max(unresolved, key=lambda c: c.tension_score)

    # Get the node names for readable question
    nodes = get_all_preference_nodes(conn, min_confidence=0.0)
    node_map = {n.id: n.name for n in nodes}
    name_a = node_map.get(target.node_id_a, "one preference")
    name_b = node_map.get(target.node_id_b, "another preference")

    return (
        f"I've noticed a tension between '{name_a}' and '{name_b}' — "
        f"when these conflict, which one wins for you?"
    )


def _pick_low_confidence_question(conn: sqlite3.Connection) -> str | None:
    """Generate a question from low-confidence inferred preferences."""
    nodes = get_all_preference_nodes(conn, min_confidence=0.0)
    candidates = [
        n for n in nodes
        if n.confidence < 0.5 and n.source == NodeSource.INFERRED
    ]
    if not candidates:
        return None

    # Sort by lowest confidence — biggest gap in our understanding
    candidates.sort(key=lambda n: n.confidence)
    target = candidates[0]

    templates = {
        NodeType.VALUE: (
            f"I've been picking up that '{target.name}' might be important to you "
            f"as a core value — is that accurate, or am I off?"
        ),
        NodeType.PREFERENCE: (
            f"I have a soft inference that you prefer: {target.description[:100]}. "
            f"Does that sound right?"
        ),
        NodeType.CONSTRAINT: (
            f"I think there might be a constraint around '{target.name}' — "
            f"is this something you'd consider a hard rule?"
        ),
        NodeType.PRINCIPLE: (
            f"I've inferred a principle: '{target.name}'. "
            f"Is this something you'd explicitly endorse?"
        ),
    }

    return templates.get(target.node_type, f"How important is '{target.name}' to you right now?")


def _pick_blind_spot_question(conn: sqlite3.Connection) -> str | None:
    """Surface an unseen blind spot as a gentle question."""
    spots = get_blind_spots(conn, surfaced_only=False)
    unsurfaced = [s for s in spots if not s.surfaced and s.confidence >= 0.6]
    if not unsurfaced:
        return None

    target = max(unsurfaced, key=lambda s: s.confidence)
    return (
        f"I've noticed a pattern ({target.category}): {target.description[:120]}. "
        f"Is this something you're aware of?"
    )


def _pick_fading_arc_question(conn: sqlite3.Connection) -> str | None:
    """Ask about narrative arcs that are going cold."""
    arcs = get_active_narrative_arcs(conn)
    now = datetime.now(timezone.utc)

    fading = [
        arc for arc in arcs
        if arc.status == "active" and (now - arc.last_updated).days > 7
    ]
    if not fading:
        return None

    # Pick the most stale active arc
    target = max(fading, key=lambda a: (now - a.last_updated).days)
    days = (now - target.last_updated).days

    return (
        f"You haven't mentioned '{target.title}' in {days} days — "
        f"is this still active, or should I deprioritize it?"
    )


def generate_clarifying_question(
    conn: sqlite3.Connection,
    context: str = "",
) -> str | None:
    """
    Generate a clarifying question based on model gaps.
    Returns None if no question is warranted or budget is exhausted.

    Priority order:
    1. Unresolved contradictions (highest signal-to-noise)
    2. Low-confidence inferred preferences (biggest model gap)
    3. Unsurfaced blind spots (self-knowledge expansion)
    4. Fading arcs (project tracking hygiene)
    """
    if not should_ask_question(conn):
        return None

    # Try each source in priority order
    generators = [
        _pick_contradiction_question,
        _pick_low_confidence_question,
        _pick_blind_spot_question,
        _pick_fading_arc_question,
    ]

    for gen in generators:
        q = gen(conn)
        if q:
            return q

    return None


def get_inquiry_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return current inquiry budget status and available question sources."""
    row = conn.execute(
        "SELECT value FROM um_metadata WHERE key = 'last_inquiry_at'"
    ).fetchone()

    if not row:
        can_ask = True
        last = None
        hours_remaining = 0.0
    else:
        last = datetime.fromisoformat(row["value"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - last
        can_ask = elapsed > timedelta(hours=_DEFAULT_BUDGET_HOURS)
        hours_remaining = max(0, _DEFAULT_BUDGET_HOURS - elapsed.total_seconds() / 3600)

    # Count available question sources
    sources: dict[str, int] = {}
    try:
        contradictions = get_active_contradictions(conn)
        sources["unresolved_contradictions"] = sum(1 for c in contradictions if not c.resolved)
    except Exception:
        pass
    try:
        nodes = get_all_preference_nodes(conn, min_confidence=0.0)
        sources["low_confidence_inferred"] = sum(
            1 for n in nodes if n.confidence < 0.5 and n.source == NodeSource.INFERRED
        )
    except Exception:
        pass
    try:
        spots = get_blind_spots(conn, surfaced_only=False)
        sources["unsurfaced_blind_spots"] = sum(1 for s in spots if not s.surfaced)
    except Exception:
        pass

    return {
        "can_ask": can_ask,
        "last_inquiry": last.isoformat() if last else None,
        "hours_until_next": round(hours_remaining, 1),
        "available_sources": sources,
    }
