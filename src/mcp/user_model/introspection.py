"""
Introspection: /me command family — full user-facing inspection and correction interface.

All reads from the model live here. MCP tools in tools.py delegate to these functions.
Read-only except for model_correct, which writes corrections back.

Depends on: all other user_model modules (read-only access).
"""

import sqlite3
from datetime import datetime
from typing import Any

from .db import (
    get_active_contradictions,
    get_active_life_patterns,
    get_active_narrative_arcs,
    get_all_preference_nodes,
    get_attention_stack,
    get_blind_spots,
    get_emotional_baseline,
    get_model_metadata,
    get_preference_node,
    get_recent_observations,
)
from .preference_graph import apply_correction, resolve_preferences
from .schema import NodeType


# ---------------------------------------------------------------------------
# model_query: structured query over the model
# ---------------------------------------------------------------------------

def query_model(
    conn: sqlite3.Connection,
    query_type: str,
    filters: dict[str, Any] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """
    Structured query over the user model.

    query_type options:
    - "preferences": Get preference nodes with optional filters
    - "observations": Get recent observations
    - "emotions": Get emotional baseline and recent states
    - "arcs": Get narrative arcs
    - "patterns": Get life patterns
    - "attention": Get attention stack
    - "contradictions": Get active contradictions
    - "blind_spots": Get blind spots
    - "meta": Get model metadata
    """
    filters = filters or {}

    if query_type == "preferences":
        node_type_str = filters.get("node_type")
        node_type = NodeType(node_type_str) if node_type_str else None
        min_confidence = float(filters.get("min_confidence", 0.0))
        nodes = get_all_preference_nodes(conn, node_type=node_type, min_confidence=min_confidence)
        return {
            "type": "preferences",
            "count": len(nodes),
            "nodes": [_node_to_dict(n) for n in nodes[:limit]],
        }

    elif query_type == "observations":
        hours = int(filters.get("hours", 24))
        signal_type = filters.get("signal_type")
        obs = get_recent_observations(conn, hours=hours, signal_type=signal_type, limit=limit)
        return {
            "type": "observations",
            "count": len(obs),
            "observations": [
                {
                    "id": o.id,
                    "signal_type": o.signal_type.value,
                    "content": o.content,
                    "confidence": o.confidence,
                    "context": o.context,
                    "observed_at": o.observed_at.isoformat(),
                }
                for o in obs
            ],
        }

    elif query_type == "emotions":
        days = int(filters.get("days", 30))
        baseline = get_emotional_baseline(conn, days=days)
        return {"type": "emotions", "baseline": baseline}

    elif query_type == "arcs":
        arcs = get_active_narrative_arcs(conn)
        return {
            "type": "arcs",
            "count": len(arcs),
            "arcs": [
                {
                    "id": a.id,
                    "title": a.title,
                    "status": a.status,
                    "themes": a.themes,
                    "started_at": a.started_at.isoformat(),
                }
                for a in arcs[:limit]
            ],
        }

    elif query_type == "patterns":
        patterns = get_active_life_patterns(conn)
        return {
            "type": "patterns",
            "count": len(patterns),
            "patterns": [
                {
                    "id": p.id,
                    "name": p.name,
                    "stage": p.stage,
                    "confidence": p.confidence,
                    "evidence_count": p.evidence_count,
                }
                for p in patterns[:limit]
            ],
        }

    elif query_type == "attention":
        stack = get_attention_stack(conn, limit=limit)
        return {
            "type": "attention",
            "count": len(stack),
            "items": [
                {
                    "id": i.id,
                    "title": i.title,
                    "category": i.category.value,
                    "score": round(i.score, 3),
                    "context": i.context,
                    "source": i.source,
                }
                for i in stack
            ],
        }

    elif query_type == "contradictions":
        contradictions = get_active_contradictions(conn)
        return {
            "type": "contradictions",
            "count": len(contradictions),
            "items": [
                {
                    "id": c.id,
                    "description": c.description,
                    "tension_score": c.tension_score,
                    "detected_at": c.detected_at.isoformat(),
                }
                for c in contradictions[:limit]
            ],
        }

    elif query_type == "blind_spots":
        spots = get_blind_spots(conn, surfaced_only=False)
        return {
            "type": "blind_spots",
            "count": len(spots),
            "items": [
                {
                    "id": s.id,
                    "category": s.category,
                    "description": s.description,
                    "surfaced": s.surfaced,
                    "confidence": s.confidence,
                }
                for s in spots[:limit]
            ],
        }

    elif query_type == "meta":
        meta = get_model_metadata(conn)
        return {
            "type": "meta",
            "schema_version": meta.schema_version,
            "owner_id": meta.owner_id,
            "observation_count": meta.observation_count,
            "preference_node_count": meta.preference_node_count,
            "created_at": meta.created_at.isoformat(),
            "last_observation_at": meta.last_observation_at.isoformat() if meta.last_observation_at else None,
            "last_consolidation_at": meta.last_consolidation_at.isoformat() if meta.last_consolidation_at else None,
        }

    else:
        return {"error": f"Unknown query_type: {query_type}"}


# ---------------------------------------------------------------------------
# model_preferences: context-resolved preference list
# ---------------------------------------------------------------------------

def get_resolved_preferences(
    conn: sqlite3.Connection,
    contexts: list[str] | None = None,
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    """
    Return inheritance-resolved preferences for the given contexts.
    Uses the preference graph's resolve_preferences which handles inheritance and overrides.
    """
    nodes = resolve_preferences(conn, contexts or [], min_confidence)
    return {
        "contexts": contexts or [],
        "min_confidence": min_confidence,
        "count": len(nodes),
        "preferences": [_node_to_dict(n) for n in nodes],
    }


# ---------------------------------------------------------------------------
# model_inspect: drill into a specific entity
# ---------------------------------------------------------------------------

def inspect_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    entity_type: str = "preference",
) -> dict[str, Any]:
    """
    Get full details for a specific entity (preference node, arc, pattern, etc.).
    """
    if entity_type == "preference":
        node = get_preference_node(conn, entity_id)
        if not node:
            return {"error": f"Preference node {entity_id} not found"}

        # Get edges
        edges = conn.execute(
            """SELECT pe.edge_type, pe.source_id, pe.target_id,
                      pn.name as other_name
               FROM um_preference_edges pe
               JOIN um_preference_nodes pn
                 ON (pe.source_id = pn.id AND pe.target_id = ?)
                 OR (pe.target_id = pn.id AND pe.source_id = ?)
               WHERE pe.source_id = ? OR pe.target_id = ?""",
            (entity_id, entity_id, entity_id, entity_id),
        ).fetchall()

        return {
            "entity_type": "preference",
            "node": _node_to_dict(node),
            "edges": [
                {
                    "type": e["edge_type"],
                    "source_id": e["source_id"],
                    "target_id": e["target_id"],
                    "other_name": e["other_name"],
                }
                for e in edges
            ],
        }

    return {"error": f"Unknown entity_type: {entity_type}"}


# ---------------------------------------------------------------------------
# model_correct: user correction (audit-trailed write)
# ---------------------------------------------------------------------------

def correct_preference(
    conn: sqlite3.Connection,
    node_id: str,
    corrected_description: str,
    corrected_strength: float | None = None,
) -> dict[str, Any]:
    """
    Apply a user correction to a preference node.
    Source becomes 'corrected', confidence becomes 1.0.
    Returns confirmation dict.
    """
    node = get_preference_node(conn, node_id)
    if not node:
        return {"success": False, "error": f"Node {node_id} not found"}

    old_description = node.description
    apply_correction(conn, node_id, corrected_description, corrected_strength)

    return {
        "success": True,
        "node_id": node_id,
        "name": node.name,
        "old_description": old_description,
        "new_description": corrected_description,
        "confidence": 1.0,
        "source": "corrected",
    }


# ---------------------------------------------------------------------------
# model_reflect: trigger a synthesis pass
# ---------------------------------------------------------------------------

def reflect(
    conn: sqlite3.Connection,
    focus: str | None = None,
) -> dict[str, Any]:
    """
    Trigger a lightweight synchronous reflection pass.
    Returns a summary of what was synthesized.

    Full LLM-powered synthesis is handled by inference.py (nightly pipeline).
    This provides an immediate, heuristic-based reflection.
    """
    from .self_knowledge import detect_contradictions, persist_new_contradictions
    from .prediction import refresh_attention_stack

    results: dict[str, Any] = {"focus": focus, "actions": []}

    # Detect and persist new contradictions
    new_contradictions = detect_contradictions(conn)
    if new_contradictions:
        ids = persist_new_contradictions(conn, new_contradictions)
        results["actions"].append(
            f"Detected {len(new_contradictions)} new contradictions: {ids}"
        )

    # Refresh attention stack
    att_ids = refresh_attention_stack(conn)
    results["actions"].append(f"Refreshed attention stack ({len(att_ids)} items)")

    # Summary stats
    meta = get_model_metadata(conn)
    results["model_stats"] = {
        "observation_count": meta.observation_count,
        "preference_node_count": meta.preference_node_count,
        "last_observation_at": meta.last_observation_at.isoformat() if meta.last_observation_at else None,
    }

    return results


# ---------------------------------------------------------------------------
# model_attention: get scored attention stack
# ---------------------------------------------------------------------------

def get_attention(
    conn: sqlite3.Connection,
    contexts: list[str] | None = None,
    max_items: int = 10,
) -> dict[str, Any]:
    """Get the current attention stack with scores."""
    from .prediction import build_attention_stack
    items = build_attention_stack(conn, max_items=max_items, contexts=contexts)
    return {
        "contexts": contexts or [],
        "count": len(items),
        "items": [
            {
                "title": i.title,
                "description": i.description,
                "category": i.category.value,
                "score": round(i.score, 3),
                "context": i.context,
                "source": i.source,
            }
            for i in items
        ],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_to_dict(node: Any) -> dict[str, Any]:
    """Convert a PreferenceNode to a serializable dict."""
    return {
        "id": node.id,
        "name": node.name,
        "type": node.node_type.value if hasattr(node.node_type, "value") else node.node_type,
        "strength": round(node.strength, 3),
        "flexibility": node.flexibility.value if hasattr(node.flexibility, "value") else node.flexibility,
        "contexts": node.contexts,
        "source": node.source.value if hasattr(node.source, "value") else node.source,
        "confidence": round(node.confidence, 3),
        "description": node.description,
        "evidence_count": node.evidence_count,
        "last_observed": node.last_observed.isoformat() if node.last_observed else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }
