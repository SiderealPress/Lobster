"""
Self-Knowledge: blind spots, contradictions, growth metrics, life patterns.

Surfaces what the user may not see about themselves — always with their consent.

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import (
    get_active_contradictions,
    get_active_life_patterns,
    get_all_preference_nodes,
    get_blind_spots,
    insert_blind_spot,
    insert_contradiction,
    upsert_life_pattern,
)
from .schema import (
    BlindSpot,
    Contradiction,
    LifePattern,
    NodeFlexibility,
    NodeSource,
)


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

def detect_contradictions(conn: sqlite3.Connection) -> list[Contradiction]:
    """
    Scan preference nodes for contradictions — pairs that conflict.
    Heuristic: nodes in the same context with opposing flexibility (hard vs hard)
    or orthogonal topics at high confidence are flagged.

    Returns newly detected contradictions (not yet in DB).
    """
    nodes = get_all_preference_nodes(conn, min_confidence=0.6)
    existing = get_active_contradictions(conn)
    existing_pairs = {(c.node_id_a, c.node_id_b) for c in existing}

    new_contradictions = []
    for i, node_a in enumerate(nodes):
        for node_b in nodes[i + 1:]:
            pair = (node_a.id, node_b.id)
            rev_pair = (node_b.id, node_a.id)
            if pair in existing_pairs or rev_pair in existing_pairs:
                continue

            tension = _compute_tension(node_a, node_b)
            if tension > 0.5:
                desc = _describe_tension(node_a, node_b)
                c = Contradiction(
                    id=None,
                    node_id_a=node_a.id,
                    node_id_b=node_b.id,
                    description=desc,
                    tension_score=tension,
                    detected_at=datetime.now(timezone.utc),
                )
                new_contradictions.append(c)

    return new_contradictions


def _compute_tension(node_a: Any, node_b: Any) -> float:
    """Compute tension score between two preference nodes."""
    tension = 0.0

    # Both are hard constraints — potential conflict
    if (
        node_a.flexibility == NodeFlexibility.HARD
        and node_b.flexibility == NodeFlexibility.HARD
    ):
        tension += 0.3

    # Overlapping contexts with high strength both
    shared_contexts = set(node_a.contexts) & set(node_b.contexts)
    if shared_contexts and node_a.strength > 0.7 and node_b.strength > 0.7:
        tension += 0.2

    # One is corrected (authoritative) but contradicts inferred
    if node_a.source == NodeSource.CORRECTED and node_b.source == NodeSource.INFERRED:
        if node_a.strength > 0.8 and node_b.strength > 0.7:
            tension += 0.1

    # Keyword-based contradiction heuristic
    name_a = node_a.name.lower()
    name_b = node_b.name.lower()
    if ("concise" in name_a and "detail" in name_b) or (
        "detail" in name_a and "concise" in name_b
    ):
        tension += 0.4
    if ("fast" in name_a and "thorough" in name_b) or (
        "thorough" in name_a and "fast" in name_b
    ):
        tension += 0.3

    return min(1.0, tension)


def _describe_tension(node_a: Any, node_b: Any) -> str:
    return (
        f"Tension between '{node_a.name}' (strength {node_a.strength:.2f}) "
        f"and '{node_b.name}' (strength {node_b.strength:.2f}). "
        f"Both apply in contexts: {list(set(node_a.contexts) & set(node_b.contexts)) or 'overlapping general'}."
    )


def persist_new_contradictions(
    conn: sqlite3.Connection, contradictions: list[Contradiction]
) -> list[str]:
    """Persist newly detected contradictions to DB. Returns inserted IDs."""
    return [insert_contradiction(conn, c) for c in contradictions]


# ---------------------------------------------------------------------------
# Blind spots
# ---------------------------------------------------------------------------

def add_blind_spot(
    conn: sqlite3.Connection,
    category: str,
    description: str,
    evidence: str = "",
    confidence: float = 0.6,
) -> str:
    """Record a new blind spot. Returns ID."""
    spot = BlindSpot(
        id=None,
        category=category,
        description=description,
        evidence=evidence,
        surfaced=False,
        confidence=confidence,
        created_at=datetime.now(timezone.utc),
    )
    return insert_blind_spot(conn, spot)


def surface_blind_spot(conn: sqlite3.Connection, spot_id: str) -> None:
    """Mark a blind spot as surfaced (shown to user)."""
    conn.execute(
        "UPDATE um_blind_spots SET surfaced = 1 WHERE id = ?", (spot_id,)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Life patterns
# ---------------------------------------------------------------------------

def record_life_pattern(
    conn: sqlite3.Connection,
    name: str,
    description: str,
    stage: str = "forming",
    confidence: float = 0.6,
) -> str:
    """Record or update a life pattern. Returns ID."""
    # Check if this pattern (by name) already exists
    row = conn.execute(
        "SELECT * FROM um_life_patterns WHERE name = ?", (name,)
    ).fetchone()

    if row:
        pattern = LifePattern(
            id=row["id"],
            name=name,
            description=description,
            stage=stage,
            evidence_count=row["evidence_count"] + 1,
            confidence=min(1.0, confidence + 0.05),
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.now(timezone.utc),
        )
    else:
        pattern = LifePattern(
            id=None,
            name=name,
            description=description,
            stage=stage,
            evidence_count=1,
            confidence=confidence,
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
    return upsert_life_pattern(conn, pattern)


# ---------------------------------------------------------------------------
# Formatting for file layer
# ---------------------------------------------------------------------------

def format_blind_spots_markdown(conn: sqlite3.Connection) -> str:
    """Format surfaced blind spots as markdown."""
    spots = get_blind_spots(conn, surfaced_only=True)
    lines = ["# Blind Spots\n"]
    lines.append("*Only surfaced blind spots are shown here. Unsurfaced ones remain in DB only.*\n")

    if not spots:
        lines.append("*No blind spots surfaced yet.*")
        return "\n".join(lines)

    for spot in spots:
        lines.append(f"## {spot.category.replace('_', ' ').title()}")
        lines.append(f"- **Confidence:** {spot.confidence:.2f}")
        lines.append(f"- **Detected:** {spot.created_at.strftime('%Y-%m-%d')}")
        lines.append(f"\n{spot.description}")
        if spot.evidence:
            lines.append(f"\n*Evidence: {spot.evidence}*")
        lines.append("")

    return "\n".join(lines)


def format_contradictions_markdown(conn: sqlite3.Connection) -> str:
    """Format active contradictions as markdown."""
    contradictions = get_active_contradictions(conn)
    lines = ["# Active Contradictions\n"]

    if not contradictions:
        lines.append("*No active contradictions detected.*")
        return "\n".join(lines)

    for c in contradictions:
        lines.append(f"- **Tension score {c.tension_score:.2f}:** {c.description}")
        lines.append(f"  - Detected: {c.detected_at.strftime('%Y-%m-%d')}")
        lines.append("")

    return "\n".join(lines)


def format_patterns_markdown(conn: sqlite3.Connection) -> str:
    """Format life patterns as markdown."""
    patterns = get_active_life_patterns(conn)
    lines = ["# Life Patterns\n"]

    if not patterns:
        lines.append("*No life patterns detected yet.*")
        return "\n".join(lines)

    for p in patterns:
        lines.append(f"## {p.name}")
        lines.append(f"- **Stage:** {p.stage}")
        lines.append(f"- **Confidence:** {p.confidence:.2f}")
        lines.append(f"- **Evidence count:** {p.evidence_count}")
        lines.append(f"- **First seen:** {p.first_seen.strftime('%Y-%m-%d')}")
        lines.append(f"- **Last seen:** {p.last_seen.strftime('%Y-%m-%d')}")
        if p.description:
            lines.append(f"\n{p.description}")
        lines.append("")

    return "\n".join(lines)
