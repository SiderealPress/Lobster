"""
Preference Graph: manage the directed graph of values → principles → preferences → constraints.

Handles:
- Graph construction and querying
- Context-scoped preference resolution with inheritance
- Conflict resolution between competing preferences
- Decay of unobserved preferences over time

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .db import (
    add_preference_edge,
    get_all_preference_nodes,
    get_preference_node,
    get_preferences_for_context,
    upsert_preference_node,
)
from .schema import (
    NodeFlexibility,
    NodeSource,
    NodeType,
    PreferenceNode,
)


# ---------------------------------------------------------------------------
# Graph construction helpers
# ---------------------------------------------------------------------------

def build_graph_index(
    conn: sqlite3.Connection,
) -> dict[str, PreferenceNode]:
    """Load all nodes into a dict keyed by ID."""
    nodes = get_all_preference_nodes(conn)
    return {n.id: n for n in nodes}


def get_node_ancestry(
    conn: sqlite3.Connection, node_id: str
) -> list[PreferenceNode]:
    """
    Walk the 'derives_from' edges upward and return the ancestor chain.
    Returns [immediate parent, grandparent, ...] in order.
    """
    ancestors = []
    visited = set()
    current_id = node_id
    while True:
        if current_id in visited:
            break
        visited.add(current_id)
        row = conn.execute(
            """SELECT pn.* FROM um_preference_nodes pn
               JOIN um_preference_edges pe ON pe.source_id = pn.id
               WHERE pe.target_id = ? AND pe.edge_type = 'derives_from'
               LIMIT 1""",
            (current_id,),
        ).fetchone()
        if not row:
            break
        parent = _row_to_node_fast(row)
        ancestors.append(parent)
        current_id = parent.id
    return ancestors


def resolve_preferences(
    conn: sqlite3.Connection,
    contexts: list[str],
    min_confidence: float = 0.5,
) -> list[PreferenceNode]:
    """
    Return context-resolved preferences with inheritance.

    Resolution rules:
    1. Preferences in matching contexts override universal ones for the same concept.
    2. HARD constraints always win over SOFT preferences.
    3. Higher confidence wins ties.
    4. Overrides specified via 'overrides' edges suppress the overridden node.
    """
    candidates = get_preferences_for_context(conn, contexts, min_confidence)
    suppressed_ids = _compute_overrides(conn, candidates)
    result = [n for n in candidates if n.id not in suppressed_ids]
    # Sort: hard first, then by strength desc
    result.sort(
        key=lambda n: (0 if n.flexibility == NodeFlexibility.HARD else 1, -n.strength)
    )
    return result


def _compute_overrides(
    conn: sqlite3.Connection, nodes: list[PreferenceNode]
) -> set[str]:
    """Return IDs of nodes that are suppressed by override edges."""
    suppressed = set()
    node_ids = [n.id for n in nodes]
    if not node_ids:
        return suppressed
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"""SELECT source_id, target_id FROM um_preference_edges
            WHERE edge_type = 'overrides'
            AND source_id IN ({placeholders})""",
        node_ids,
    ).fetchall()
    for row in rows:
        suppressed.add(row["target_id"])
    return suppressed


# ---------------------------------------------------------------------------
# Preference upsert with graph wiring
# ---------------------------------------------------------------------------

def add_preference(
    conn: sqlite3.Connection,
    name: str,
    node_type: NodeType,
    description: str,
    strength: float = 0.7,
    flexibility: NodeFlexibility = NodeFlexibility.SOFT,
    contexts: list[str] | None = None,
    source: NodeSource = NodeSource.INFERRED,
    confidence: float = 0.7,
    parent_id: str | None = None,
    overrides_ids: list[str] | None = None,
) -> str:
    """
    Add or update a preference node and wire graph edges.
    Returns the node ID.
    """
    node = PreferenceNode(
        id=None,
        name=name,
        node_type=node_type,
        strength=strength,
        flexibility=flexibility,
        contexts=contexts or [],
        source=source,
        confidence=confidence,
        description=description,
        evidence_count=1,
        last_observed=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    node_id = upsert_preference_node(conn, node)

    # Wire 'derives_from' edge
    if parent_id:
        add_preference_edge(conn, node_id, parent_id, "derives_from")

    # Wire 'overrides' edges
    for oid in (overrides_ids or []):
        add_preference_edge(conn, node_id, oid, "overrides")

    return node_id


def reinforce_preference(
    conn: sqlite3.Connection,
    node_id: str,
    confidence_delta: float = 0.05,
) -> None:
    """
    Reinforce an existing preference node: increase evidence count,
    bump confidence, update last_observed.
    """
    node = get_preference_node(conn, node_id)
    if not node:
        return
    node.evidence_count += 1
    node.confidence = min(1.0, node.confidence + confidence_delta)
    node.last_observed = datetime.utcnow()
    node.updated_at = datetime.utcnow()
    upsert_preference_node(conn, node)


def apply_correction(
    conn: sqlite3.Connection,
    node_id: str,
    corrected_description: str,
    corrected_strength: float | None = None,
) -> None:
    """
    Apply a user correction to a preference node.
    Source becomes 'corrected', confidence becomes 1.0.
    """
    node = get_preference_node(conn, node_id)
    if not node:
        return
    node.source = NodeSource.CORRECTED
    node.confidence = 1.0
    node.description = corrected_description
    if corrected_strength is not None:
        node.strength = corrected_strength
    node.updated_at = datetime.utcnow()
    upsert_preference_node(conn, node)


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------

def apply_decay(
    conn: sqlite3.Connection,
    days_since_last_run: int = 1,
) -> int:
    """
    Apply decay to all non-corrected preference nodes not recently observed.
    Nodes observed within 7 days are not decayed.
    Returns the number of nodes affected.
    """
    all_nodes = get_all_preference_nodes(conn)
    cutoff = datetime.utcnow() - timedelta(days=7)
    affected = 0
    for node in all_nodes:
        if node.source == NodeSource.CORRECTED:
            continue  # Corrected nodes never decay
        last_obs = node.last_observed or node.created_at
        if last_obs < cutoff:
            decay = node.decay_rate * days_since_last_run
            node.confidence = max(0.1, node.confidence - decay)
            node.strength = max(0.1, node.strength - decay * 0.5)
            node.updated_at = datetime.utcnow()
            upsert_preference_node(conn, node)
            affected += 1
    return affected


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_node_fast(row: Any) -> PreferenceNode:
    """Convert a sqlite3.Row to a PreferenceNode (import-free helper)."""
    import json
    from datetime import datetime
    return PreferenceNode(
        id=row["id"],
        name=row["name"],
        node_type=NodeType(row["node_type"]),
        strength=row["strength"],
        flexibility=NodeFlexibility(row["flexibility"]),
        contexts=json.loads(row["contexts"]),
        source=NodeSource(row["source"]),
        confidence=row["confidence"],
        description=row["description"],
        evidence_count=row["evidence_count"],
        last_observed=datetime.fromisoformat(row["last_observed"]) if row["last_observed"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        decay_rate=row["decay_rate"],
    )
