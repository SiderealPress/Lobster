"""
Active Inquiry: budget-constrained question generation woven into conversation.

Generates clarifying questions when the model has gaps or uncertainty.
Questions are budget-constrained (max 1–2 per conversation) to avoid
making the user feel interrogated.

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .db import get_all_preference_nodes, get_emotional_baseline
from .schema import NodeSource, NodeType


# Default: at most 1 question every 24 hours
_DEFAULT_BUDGET_HOURS = 24
_DEFAULT_MAX_QUESTIONS = 1


def should_ask_question(
    conn: sqlite3.Connection,
    budget_hours: int = _DEFAULT_BUDGET_HOURS,
) -> bool:
    """
    Check if the inquiry budget allows asking a question right now.
    Reads last_inquiry_at from um_metadata.
    """
    row = conn.execute(
        "SELECT value FROM um_metadata WHERE key = 'last_inquiry_at'"
    ).fetchone()
    if not row:
        return True
    last_inquiry = datetime.fromisoformat(row["value"])
    return datetime.utcnow() - last_inquiry > timedelta(hours=budget_hours)


def record_inquiry(conn: sqlite3.Connection) -> None:
    """Record that a question was asked (update budget tracker)."""
    conn.execute(
        "INSERT OR REPLACE INTO um_metadata (key, value) VALUES ('last_inquiry_at', ?)",
        (datetime.utcnow().isoformat(),),
    )
    conn.commit()


def generate_clarifying_question(
    conn: sqlite3.Connection,
    context: str = "",
) -> str | None:
    """
    Generate a clarifying question based on model gaps.
    Returns None if no question is warranted or budget is exhausted.
    """
    if not should_ask_question(conn):
        return None

    # Find low-confidence inferred nodes — these are gaps worth asking about
    nodes = get_all_preference_nodes(conn, min_confidence=0.0)
    low_confidence = [
        n for n in nodes
        if n.confidence < 0.5 and n.source == NodeSource.INFERRED
    ]

    if not low_confidence:
        return None

    # Sort by lowest confidence first
    low_confidence.sort(key=lambda n: n.confidence)
    target = low_confidence[0]

    # Generate a question based on node type
    if target.node_type == NodeType.VALUE:
        q = f"I've noticed I might be assuming '{target.name}' is important to you — is that right?"
    elif target.node_type == NodeType.PREFERENCE:
        q = f"To better understand your preferences: {target.description[:100]}... Is this accurate?"
    elif target.node_type == NodeType.CONSTRAINT:
        q = f"I have a soft constraint noted: '{target.name}'. Is this still valid?"
    else:
        q = f"Quick check: how important is '{target.name}' to you right now?"

    return q


def get_inquiry_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return current inquiry budget status."""
    row = conn.execute(
        "SELECT value FROM um_metadata WHERE key = 'last_inquiry_at'"
    ).fetchone()
    if not row:
        return {"can_ask": True, "last_inquiry": None, "hours_until_next": 0}

    last = datetime.fromisoformat(row["value"])
    elapsed = datetime.utcnow() - last
    can_ask = elapsed > timedelta(hours=_DEFAULT_BUDGET_HOURS)
    hours_remaining = max(0, _DEFAULT_BUDGET_HOURS - elapsed.total_seconds() / 3600)

    return {
        "can_ask": can_ask,
        "last_inquiry": last.isoformat(),
        "hours_until_next": round(hours_remaining, 1),
    }
