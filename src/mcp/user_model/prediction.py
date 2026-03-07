"""
Prediction & Attention: attention scoring, proactive guidance, value-aligned nudging.

Computes what the user should focus on right now, based on:
- Active preference nodes (strength × confidence)
- Narrative arc momentum
- Life pattern stage
- Emotional state
- Time-of-day relevance

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .db import (
    get_active_narrative_arcs,
    get_active_life_patterns,
    get_attention_stack,
    get_emotional_baseline,
    get_recent_observations,
    upsert_attention_item,
)
from .schema import AttentionCategory, AttentionItem


# ---------------------------------------------------------------------------
# Attention scoring
# ---------------------------------------------------------------------------

def compute_attention_score(
    urgency: float = 0.5,
    importance: float = 0.5,
    alignment: float = 0.5,  # How well this aligns with user's values
    recency: float = 0.5,    # How recent the signal
    staleness_days: int = 0, # Days since last action
) -> float:
    """
    Compute a priority score for an attention item.

    Weights loosely follow the Eisenhower matrix, adjusted for value alignment.
    """
    # Urgency weight: 0.35, Importance: 0.35, Alignment: 0.2, Recency: 0.1
    score = (
        0.35 * urgency
        + 0.35 * importance
        + 0.20 * alignment
        + 0.10 * recency
    )

    # Staleness bonus: if something has been waiting, bump it up slightly
    if staleness_days > 3:
        score += min(0.15, staleness_days * 0.02)

    return min(1.0, score)


def build_attention_stack(
    conn: sqlite3.Connection,
    max_items: int = 10,
    contexts: list[str] | None = None,
) -> list[AttentionItem]:
    """
    Build a scored attention stack from the current model state.

    Synthesizes:
    - Active narrative arcs (ongoing projects)
    - Life patterns in "forming" or "active" stage
    - Recent high-energy observations
    - Unresolved contradictions
    """
    items = []
    now = datetime.utcnow()
    contexts = contexts or []

    # --- From narrative arcs ---
    arcs = get_active_narrative_arcs(conn)
    for arc in arcs[:3]:
        staleness = (now - arc.last_updated).days
        score = compute_attention_score(
            urgency=0.4,
            importance=0.8,
            alignment=0.7,
            recency=max(0.1, 1.0 - staleness * 0.05),
            staleness_days=staleness,
        )
        items.append(AttentionItem(
            id=None,
            title=f"Arc: {arc.title}",
            description=arc.description[:200] if arc.description else "",
            category=AttentionCategory.IMPORTANT,
            score=score,
            context=", ".join(arc.themes),
            source="narrative_arc",
            metadata={"arc_id": arc.id, "arc_status": arc.status},
            created_at=now,
        ))

    # --- From life patterns in forming stage ---
    patterns = get_active_life_patterns(conn)
    for p in patterns:
        if p.stage == "forming":
            # Forming patterns benefit from reinforcement — surface them
            score = compute_attention_score(
                urgency=0.3,
                importance=0.6,
                alignment=0.8,
                recency=0.6,
            )
            items.append(AttentionItem(
                id=None,
                title=f"Pattern forming: {p.name}",
                description=p.description[:200] if p.description else "",
                category=AttentionCategory.MONITORING,
                score=score,
                context="patterns",
                source="life_pattern",
                metadata={"pattern_id": p.id},
                created_at=now,
            ))

    # --- High-energy observations from last 24h ---
    high_energy = get_recent_observations(conn, hours=24, signal_type="energy", limit=5)
    for obs in high_energy:
        if obs.content == "high":
            score = compute_attention_score(
                urgency=0.7,
                importance=0.6,
                alignment=0.5,
                recency=0.9,
            )
            items.append(AttentionItem(
                id=None,
                title="High-urgency signal detected",
                description=f"Recent high-energy message observed (context: {obs.context})",
                category=AttentionCategory.URGENT,
                score=score,
                context=obs.context,
                source="observation",
                metadata={"obs_id": obs.id},
                created_at=now,
            ))
            break  # One per day is enough

    # Sort by score and take top N
    items.sort(key=lambda x: x.score, reverse=True)
    return items[:max_items]


def refresh_attention_stack(
    conn: sqlite3.Connection,
    max_items: int = 10,
) -> list[str]:
    """
    Recompute the attention stack and persist to DB.
    Returns list of upserted item IDs.
    """
    # Clear expired items
    conn.execute(
        "DELETE FROM um_attention_items WHERE expires_at IS NOT NULL AND expires_at < datetime('now')"
    )
    conn.commit()

    # Compute new stack
    items = build_attention_stack(conn, max_items)

    # Persist
    ids = []
    for item in items:
        item_id = upsert_attention_item(conn, item)
        ids.append(item_id)

    return ids


# ---------------------------------------------------------------------------
# Formatting for file layer
# ---------------------------------------------------------------------------

def format_attention_markdown(conn: sqlite3.Connection) -> str:
    """Format the attention stack as markdown for the file layer."""
    stack = get_attention_stack(conn, limit=10)
    lines = ["# Attention Stack\n"]
    lines.append("*Scored priority queue — use this to answer 'what should I focus on?'*\n")

    if not stack:
        lines.append("*No attention items currently scored.*")
        return "\n".join(lines)

    by_category: dict[str, list[AttentionItem]] = {}
    for item in stack:
        cat = item.category.value
        by_category.setdefault(cat, []).append(item)

    for cat in ["urgent", "important", "monitoring", "deferred"]:
        if cat not in by_category:
            continue
        lines.append(f"## {cat.title()}")
        for item in by_category[cat]:
            lines.append(f"- **{item.title}** (score: {item.score:.2f})")
            if item.description:
                lines.append(f"  {item.description}")
        lines.append("")

    return "\n".join(lines)
