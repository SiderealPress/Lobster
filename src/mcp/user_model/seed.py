"""
Seed Bootstrap: ingest stated values and preferences from owner.toml.

On first run, this module populates the preference
graph with stated values and preferences from owner.toml. Seeded nodes have:
- source = NodeSource.STATED
- seed_source = 'owner_toml'
- confidence = 1.0
- decay_rate_override = 0.001 (very slow decay)

Format supported in owner.toml:
    [values]
    autonomy = "I strongly value independence and making my own decisions."
    technical_depth = "I prefer understanding systems deeply."

    [preferences]
    response_length = "concise — short answers unless I ask for detail"

    [constraints]
    no_emojis = "Never use emojis in responses unless I explicitly ask"

Depends on: schema.py, db.py, preference_graph.py, owner.py only.
"""

import sqlite3
from pathlib import Path
from typing import Any

from .db import upsert_preference_node
from .owner import read_owner
from .preference_graph import add_preference
from .schema import NodeFlexibility, NodeSource, NodeType, PreferenceNode


# ---------------------------------------------------------------------------
# Seed ingestion
# ---------------------------------------------------------------------------

def seed_from_owner_toml(
    conn: sqlite3.Connection,
    owner_file: Path | None = None,
) -> dict[str, int]:
    """
    Ingest values, preferences, and constraints from owner.toml into the preference graph.

    Returns count dict: {'values': N, 'preferences': N, 'constraints': N, 'principles': N}.

    Idempotent: nodes with same name and seed_source='owner_toml' are upserted, not duplicated.
    Uses name matching to avoid creating duplicate nodes on repeated calls.
    """
    data = read_owner(owner_file)
    counts: dict[str, int] = {"values": 0, "preferences": 0, "constraints": 0, "principles": 0}

    section_to_type = {
        "values": NodeType.VALUE,
        "preferences": NodeType.PREFERENCE,
        "constraints": NodeType.CONSTRAINT,
        "principles": NodeType.PRINCIPLE,
    }

    for section_name, node_type in section_to_type.items():
        section = data.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for key, description in section.items():
            if not description or not isinstance(description, str):
                continue
            _upsert_seeded_node(
                conn,
                name=key.replace("_", "-").lower(),
                description=description.strip(),
                node_type=node_type,
            )
            counts[section_name] += 1

    return counts


def _upsert_seeded_node(
    conn: sqlite3.Connection,
    name: str,
    description: str,
    node_type: NodeType,
) -> str:
    """
    Insert or update a seeded preference node.
    If a node with this name already exists, update its description and mark as seeded.
    Returns the node ID.
    """
    from datetime import datetime, timezone
    import json

    # Check if a node with this name already exists
    existing = conn.execute(
        "SELECT * FROM um_preference_nodes WHERE name = ?", (name,)
    ).fetchone()

    if existing:
        # Update in place — preserve evidence_count and last_observed
        node = PreferenceNode(
            id=existing["id"],
            name=name,
            node_type=NodeType(existing["node_type"]),
            strength=max(existing["strength"], 0.85),  # seeded nodes have strong baseline
            flexibility=_node_type_to_flexibility(node_type),
            contexts=json.loads(existing["contexts"]),
            source=NodeSource.STATED,
            confidence=1.0,
            description=description,
            evidence_count=existing["evidence_count"],
            created_at=datetime.fromisoformat(existing["created_at"]),
            updated_at=datetime.now(timezone.utc),
            decay_rate=0.01,
        )
        # Set v2 columns via direct SQL (they may not exist in PreferenceNode dataclass yet)
        upsert_preference_node(conn, node)
        _set_seed_columns(conn, node.id, seed_source="owner_toml", decay_rate_override=0.001)
        return node.id
    else:
        # Create new seeded node
        node_id = add_preference(
            conn,
            name=name,
            node_type=node_type,
            description=description,
            strength=0.85,
            flexibility=_node_type_to_flexibility(node_type),
            contexts=[],  # Universal — applies everywhere
            source=NodeSource.STATED,
            confidence=1.0,
        )
        _set_seed_columns(conn, node_id, seed_source="owner_toml", decay_rate_override=0.001)
        return node_id


def _node_type_to_flexibility(node_type: NodeType) -> NodeFlexibility:
    """Map node type to default flexibility."""
    return {
        NodeType.VALUE: NodeFlexibility.SOFT,
        NodeType.PRINCIPLE: NodeFlexibility.SOFT,
        NodeType.PREFERENCE: NodeFlexibility.SOFT,
        NodeType.CONSTRAINT: NodeFlexibility.HARD,
    }.get(node_type, NodeFlexibility.SOFT)


def _set_seed_columns(
    conn: sqlite3.Connection,
    node_id: str,
    seed_source: str,
    decay_rate_override: float,
) -> None:
    """Set v2 seed columns on a preference node. Safe to call even if columns don't exist yet."""
    try:
        conn.execute(
            """UPDATE um_preference_nodes
               SET seed_source = ?, decay_rate_override = ?
               WHERE id = ?""",
            (seed_source, decay_rate_override, node_id),
        )
        conn.commit()
    except Exception:
        pass  # v2 columns may not exist on very old DBs; safe to skip


# ---------------------------------------------------------------------------
# Seed state detection
# ---------------------------------------------------------------------------

def is_seeded(conn: sqlite3.Connection) -> bool:
    """Return True if at least one node with seed_source='owner_toml' exists."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM um_preference_nodes WHERE seed_source = 'owner_toml'"
        ).fetchone()
        return row["n"] > 0 if row else False
    except Exception:
        return False


def reseed_if_needed(conn: sqlite3.Connection, owner_file: Path | None = None) -> bool:
    """
    Run seed_from_owner_toml if not yet seeded.
    Also re-seeds if owner.toml has been updated (detected by checking count vs expected).
    Returns True if seeding occurred.
    """
    data = read_owner(owner_file)

    # Count expected seeds
    expected = sum(
        len(data.get(section, {}))
        for section in ["values", "preferences", "constraints", "principles"]
        if isinstance(data.get(section), dict)
    )

    if expected == 0:
        return False  # Nothing to seed

    if not is_seeded(conn):
        seed_from_owner_toml(conn, owner_file)
        return True

    return False
