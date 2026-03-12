"""
Temporal Modeling: weekly snapshots and drift detection.

Captures weekly state of the preference graph and detects meaningful changes
over time. This is the foundation for understanding how Drew's values and
preferences evolve — and for surfacing those changes back to him.

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime
from typing import Any

from .db import (
    get_active_narrative_arcs,
    get_active_life_patterns,
    get_all_preference_nodes,
    get_emotional_baseline,
    get_latest_snapshot,
    get_observation_count,
    get_recent_drifts,
    get_snapshots_since,
    insert_drift_record,
    insert_temporal_snapshot,
)
from .schema import (
    DriftRecord,
    NodeType,
    TemporalSnapshot,
)


# ---------------------------------------------------------------------------
# Snapshot management
# ---------------------------------------------------------------------------

def take_weekly_snapshot_if_due(conn: sqlite3.Connection) -> str | None:
    """
    Take a temporal snapshot if one is due (first run, or > 6 days since last).
    Returns snapshot ID if taken, None otherwise.
    """
    latest = get_latest_snapshot(conn)
    if latest:
        days_since = (datetime.utcnow() - latest.snapshot_at).days
        if days_since < 6:
            return None

    return _capture_snapshot(conn)


def force_snapshot(conn: sqlite3.Connection) -> str:
    """Force an immediate snapshot regardless of timing. Returns snapshot ID."""
    return _capture_snapshot(conn)


def _capture_snapshot(conn: sqlite3.Connection) -> str:
    """Capture and persist the current preference graph state as a snapshot."""
    nodes = get_all_preference_nodes(conn)
    baseline = get_emotional_baseline(conn, days=30)
    arcs = get_active_narrative_arcs(conn)
    patterns = get_active_life_patterns(conn)
    obs_count = get_observation_count(conn)

    data: dict[str, Any] = {
        "preferences": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.node_type.value if hasattr(n.node_type, "value") else n.node_type,
                "strength": round(n.strength, 3),
                "confidence": round(n.confidence, 3),
                "contexts": n.contexts,
            }
            for n in nodes
        ],
        "emotional_baseline": baseline,
        "active_arc_titles": [a.title for a in arcs],
        "active_pattern_names": [p.name for p in patterns],
    }

    now = datetime.utcnow()
    iso_cal = now.isocalendar()
    snapshot = TemporalSnapshot(
        id=None,
        snapshot_at=now,
        week_number=int(iso_cal[1]),
        year=int(iso_cal[0]),
        data=data,
        obs_count=obs_count,
        node_count=len(nodes),
    )
    return insert_temporal_snapshot(conn, snapshot)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def detect_drift_since_last_snapshot(conn: sqlite3.Connection) -> list[DriftRecord]:
    """
    Compare the current model state to the most recent snapshot and detect changes.

    Drift types:
    - 'preference_shift': strength changed > 0.15 for any preference node
    - 'value_drift': strength changed > 0.25 for value nodes (higher threshold)
    - 'pattern_change': new node emerged or existing node faded (confidence < 0.3)
    - 'emotional_drift': emotional baseline valence shifted > 0.3

    Returns a list of DriftRecords (not yet persisted — caller should persist).
    """
    latest = get_latest_snapshot(conn)
    if not latest:
        return []

    # Get the snapshot before the latest (to compare)
    snapshots = get_snapshots_since(conn, days=60)
    if len(snapshots) < 2:
        return []

    # snapshots is sorted newest-first; compare latest (index 0) to previous (index 1)
    current_snap = snapshots[0]
    previous_snap = snapshots[1]

    records: list[DriftRecord] = []
    now = datetime.utcnow()

    # Build index of previous state
    prev_prefs = {p["id"]: p for p in previous_snap.data.get("preferences", [])}
    curr_prefs = {p["id"]: p for p in current_snap.data.get("preferences", [])}

    # Detect strength changes
    for node_id, curr in curr_prefs.items():
        if node_id not in prev_prefs:
            # New node — emerged this week
            records.append(DriftRecord(
                id=None,
                detected_at=now,
                snapshot_a_id=previous_snap.id,
                snapshot_b_id=current_snap.id,
                drift_type="pattern_change",
                description=f"New preference emerged: '{curr['name']}' (strength {curr['strength']:.2f})",
                magnitude=curr["strength"],
                node_id=node_id,
            ))
            continue

        prev = prev_prefs[node_id]
        delta = abs(curr["strength"] - prev["strength"])

        is_value = curr.get("type") == "value"
        threshold = 0.25 if is_value else 0.15
        drift_type = "value_drift" if is_value else "preference_shift"

        if delta > threshold:
            direction = "strengthened" if curr["strength"] > prev["strength"] else "weakened"
            records.append(DriftRecord(
                id=None,
                detected_at=now,
                snapshot_a_id=previous_snap.id,
                snapshot_b_id=current_snap.id,
                drift_type=drift_type,
                description=(
                    f"'{curr['name']}' {direction}: "
                    f"{prev['strength']:.2f} → {curr['strength']:.2f}"
                ),
                magnitude=round(delta, 3),
                node_id=node_id,
            ))

    # Detect faded nodes (in previous but confidence now very low)
    for node_id, prev in prev_prefs.items():
        if node_id not in curr_prefs:
            # Check if it still exists with low confidence
            row = conn.execute(
                "SELECT name, confidence FROM um_preference_nodes WHERE id = ?",
                (node_id,),
            ).fetchone()
            if row and row["confidence"] < 0.3:
                records.append(DriftRecord(
                    id=None,
                    detected_at=now,
                    snapshot_a_id=previous_snap.id,
                    snapshot_b_id=current_snap.id,
                    drift_type="pattern_change",
                    description=f"Preference faded: '{prev['name']}' (confidence below threshold)",
                    magnitude=prev.get("strength", 0.5),
                    node_id=node_id,
                ))

    # Detect emotional drift
    prev_baseline = previous_snap.data.get("emotional_baseline") or {}
    curr_baseline = current_snap.data.get("emotional_baseline") or {}
    if prev_baseline and curr_baseline:
        valence_delta = abs(
            curr_baseline.get("valence", 0.0) - prev_baseline.get("valence", 0.0)
        )
        if valence_delta > 0.3:
            direction = "more positive" if curr_baseline.get("valence", 0) > prev_baseline.get("valence", 0) else "more negative"
            records.append(DriftRecord(
                id=None,
                detected_at=now,
                snapshot_a_id=previous_snap.id,
                snapshot_b_id=current_snap.id,
                drift_type="emotional_drift",
                description=f"Emotional baseline shifted {direction} (delta {valence_delta:.2f})",
                magnitude=round(valence_delta, 3),
            ))

    return records


def persist_drift_records(
    conn: sqlite3.Connection,
    records: list[DriftRecord],
) -> list[str]:
    """Persist drift records to DB. Returns inserted IDs."""
    return [insert_drift_record(conn, r) for r in records]


# ---------------------------------------------------------------------------
# Drift summary formatting
# ---------------------------------------------------------------------------

def format_drift_summary(drifts: list[DriftRecord]) -> str:
    """Format a list of drift records as a human-readable summary."""
    if not drifts:
        return "No significant changes detected since last week."

    lines = []
    for d in drifts[:5]:  # Cap at 5 for readability
        lines.append(f"- {d.description} (magnitude: {d.magnitude:.2f})")

    return "\n".join(lines)


def get_drift_summary_for_reflect(conn: sqlite3.Connection) -> list[dict]:
    """Return recent unsurfaced drifts formatted for model_reflect output."""
    from .db import get_unsurfaced_drifts
    drifts = get_unsurfaced_drifts(conn)
    return [
        {
            "type": d.drift_type,
            "description": d.description,
            "magnitude": round(d.magnitude, 3),
            "node_id": d.node_id,
        }
        for d in drifts[:5]
    ]
