# Phase 4: Enhanced Reflection, Drift Detection & Prediction API

*Part of User Model v2 — see [PLAN.md](PLAN.md)*

---

## Goal

Make `model_reflect` and the nightly consolidation pipeline produce:
1. Weekly temporal snapshots for drift tracking
2. Structured week-over-week user state summaries
3. Better contradiction detection (beyond keyword matching)
4. A `user_state.md` file in the markdown layer

## Enhanced Nightly Consolidation Pipeline

The updated pipeline in `inference.py`:

```
run_consolidation(conn, workspace_path, days_since_last_run):
  1. Apply preference decay           [unchanged from v1]
  2. Detect contradictions            [enhanced: semantic expansion]
  3. Process pending observations     [enhanced: observation → preference feedback loop]
  4. Refresh attention stack          [enhanced: time + emotional weighting]
  5. Sync markdown files              [enhanced: adds user_state.md]
  6. Update last_consolidation_at     [unchanged]
  NEW 7. Take weekly snapshot if due
  NEW 8. Detect drift since last snapshot
  NEW 9. Update activity rhythm (hourly rollup)
```

## Weekly Snapshot Logic

A snapshot is "due" when:
- No snapshot exists yet (first run), OR
- The latest snapshot was taken > 6 days ago

```python
def take_weekly_snapshot_if_due(conn: sqlite3.Connection) -> str | None:
    """
    Take a temporal snapshot if one is due.
    Returns snapshot ID if taken, None otherwise.
    """
    latest = get_latest_snapshot(conn)
    if latest:
        days_since = (datetime.utcnow() - latest.snapshot_at).days
        if days_since < 6:
            return None

    # Serialize current preference graph state
    nodes = get_all_preference_nodes(conn)
    baseline = get_emotional_baseline(conn, days=30)
    arcs = get_active_narrative_arcs(conn)
    patterns = get_active_life_patterns(conn)

    data = {
        "preferences": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.node_type.value,
                "strength": round(n.strength, 3),
                "confidence": round(n.confidence, 3),
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
        week_number=iso_cal[1],
        year=iso_cal[0],
        data=data,
        obs_count=get_observation_count(conn),
        node_count=len(nodes),
    )
    return insert_temporal_snapshot(conn, snapshot)
```

## Drift Detection Algorithm

```python
def detect_drift_since_last_snapshot(
    conn: sqlite3.Connection,
) -> list[DriftRecord]:
    """
    Compare the current state to the most recent snapshot and detect significant changes.
    Called after a new snapshot is taken.

    Drift detection rules:

    1. Preference strength drift:
       For each node that exists in both current state and snapshot:
       If abs(current_strength - snapshot_strength) > 0.15:
         Create DriftRecord(type='preference_shift', magnitude=abs_delta)

    2. Value drift (higher threshold — values are more stable):
       For VALUE nodes only:
       If abs(current_strength - snapshot_strength) > 0.25:
         Create DriftRecord(type='value_drift', magnitude=abs_delta)

    3. New node emergence:
       If a node exists in current state but not in snapshot (was created this week):
         Create DriftRecord(type='preference_shift', description='New preference emerged: ...')

    4. Node disappearance:
       If a node in snapshot has confidence < 0.3 in current state (effectively gone):
         Create DriftRecord(type='preference_shift', description='Preference faded: ...')

    5. Emotional drift:
       If emotional baseline valence shifted > 0.3 since last snapshot:
         Create DriftRecord(type='emotional_drift')

    Returns list of DriftRecords (not yet persisted — caller persists them).
    """
```

## Enhanced Contradiction Detection

v1's contradiction detection is purely lexical — it looks for "concise" vs "detail" in node names. v2 expands this with a small semantic expansion dictionary:

```python
_CONTRADICTION_KEYWORDS = {
    "brevity": {"concise", "brief", "short", "terse", "minimal", "quick"},
    "thoroughness": {"detail", "thorough", "comprehensive", "deep", "exhaustive", "complete"},
    "speed": {"fast", "quick", "rapid", "immediate", "asap"},
    "quality": {"craft", "quality", "elegant", "correct", "precise", "robust"},
    "autonomy": {"independent", "autonomous", "self-directed", "own decisions"},
    "collaboration": {"team", "discuss", "consult", "check", "ask", "confirm"},
}

_CONTRADICTION_PAIRS = [
    ("brevity", "thoroughness"),
    ("speed", "quality"),
    ("autonomy", "collaboration"),
]
```

Updated `_compute_tension()` checks whether each node's name or description contains terms from opposing groups, using the expanded keyword sets rather than individual words.

```python
def _compute_tension_v2(node_a: PreferenceNode, node_b: PreferenceNode) -> float:
    """Enhanced tension computation with semantic expansion."""
    tension = 0.0

    # V1 structural checks (unchanged)
    if node_a.flexibility == NodeFlexibility.HARD and node_b.flexibility == NodeFlexibility.HARD:
        tension += 0.3
    shared_contexts = set(node_a.contexts) & set(node_b.contexts)
    if shared_contexts and node_a.strength > 0.7 and node_b.strength > 0.7:
        tension += 0.2

    # V2 semantic check
    name_a = (node_a.name + " " + node_a.description).lower()
    name_b = (node_b.name + " " + node_b.description).lower()

    for group_a, group_b in _CONTRADICTION_PAIRS:
        terms_a = _CONTRADICTION_KEYWORDS[group_a]
        terms_b = _CONTRADICTION_KEYWORDS[group_b]
        a_in_a = any(t in name_a for t in terms_a)
        b_in_b = any(t in name_b for t in terms_b)
        b_in_a = any(t in name_a for t in terms_b)
        a_in_b = any(t in name_b for t in terms_a)
        if (a_in_a and b_in_b) or (b_in_a and a_in_b):
            tension += 0.35
            break

    return min(1.0, tension)
```

## Structured User State Summary

`model_reflect` now produces a `user_state_summary` — a structured natural language summary of Drew's current state. This is written to `user_state.md` in the file layer.

```python
def produce_user_state_summary(conn: sqlite3.Connection) -> str:
    """
    Produce a structured 1-2 paragraph summary of the user's current state.
    Pure computation — no LLM required.

    Format:
    "As of [date], [name]'s top active values are [X, Y, Z]. Recent behavioral patterns
    suggest [emotional state description]. The current attention stack is dominated by
    [top arc/pattern]. [Drift description if recent drifts exist]."
    """
    meta = get_model_metadata(conn)
    nodes = get_all_preference_nodes(conn, node_type=NodeType.VALUE, min_confidence=0.5)
    baseline = get_emotional_baseline(conn, days=7)  # 7-day window for "current"
    arcs = get_active_narrative_arcs(conn)
    drifts = get_recent_drifts(conn, limit=3)
    patterns = detect_emotional_patterns(conn, lookback_days=7)

    # Compose the summary
    owner_name = get_owner_name() or "the user"
    date_str = datetime.utcnow().strftime("%Y-%m-%d")

    lines = [f"# User State Summary — {date_str}", ""]

    # Values paragraph
    if nodes:
        top_values = [n.name for n in sorted(nodes, key=lambda n: -n.strength)[:3]]
        lines.append(f"**Core values (current top 3):** {', '.join(top_values)}")

    # Emotional state
    if baseline and patterns.get("valence_state"):
        lines.append(f"**Emotional baseline (7-day):** {patterns['valence_state']}, {patterns.get('arousal_state', 'moderate energy')}")
        if "valence_trend" in patterns:
            lines.append(f"**Recent trend:** {patterns['valence_trend']}")

    # Attention / arcs
    if arcs:
        top_arc = arcs[0].title
        lines.append(f"**Primary narrative arc:** {top_arc}")

    # Drift
    if drifts:
        lines.append("")
        lines.append("**Recent shifts detected:**")
        for d in drifts[:2]:
            lines.append(f"- {d.description} (magnitude: {d.magnitude:.2f})")

    # Model health
    lines.append("")
    lines.append(f"*Model stats: {meta.observation_count} observations, {meta.preference_node_count} preference nodes*")

    return "\n".join(lines)
```

## Updated `model_reflect` Return Value

```json
{
  "focus": null,
  "actions": [
    "Detected 0 new contradictions",
    "Refreshed attention stack (4 items)",
    "Took weekly snapshot (id: abc-123, week 10/2026)",
    "Detected 2 drift records since last snapshot"
  ],
  "model_stats": {
    "observation_count": 147,
    "preference_node_count": 23,
    "last_observation_at": "2026-03-08T09:14:00"
  },
  "drift_detected": [
    {
      "type": "preference_shift",
      "description": "Preference for 'concise-responses' strengthened from 0.65 to 0.82",
      "magnitude": 0.17
    }
  ],
  "user_state_summary": "Core values: autonomy, technical-depth, craftsmanship. Emotional baseline: generally positive, high energy. Primary arc: Lobster v2 development.",
  "weekly_snapshot_taken": true
}
```

## `user_state.md` in Markdown Layer

Added to `sync_all()` in `markdown_sync.py`:

```python
# User state summary
try:
    from .inference import produce_user_state_summary
    content = produce_user_state_summary(conn)
    if _write_file(base / "user_state.md", content):
        files_written += 1
except Exception as e:
    errors.append(f"user_state: {e}")
```

## Weekly Digest Notification

When drift records are detected, add them to the nightly digest message (if Lobster sends one). The `run_consolidation` summary includes a `drift_summary` field that the calling code can surface to the user.

## Test Plan

- [ ] `take_weekly_snapshot_if_due` creates snapshot on first run
- [ ] `take_weekly_snapshot_if_due` returns None when snapshot is < 6 days old
- [ ] `detect_drift_since_last_snapshot` detects >0.15 strength changes correctly
- [ ] `detect_drift_since_last_snapshot` creates VALUE_DRIFT type for value nodes
- [ ] Emotional drift detected when baseline valence shifts > 0.3
- [ ] `_compute_tension_v2` catches "concise" vs "thorough" via semantic expansion
- [ ] `_compute_tension_v2` catches "speed" vs "quality" via semantic expansion
- [ ] `produce_user_state_summary` returns non-empty string with no data (graceful)
- [ ] `user_state.md` written correctly to file layer
- [ ] Full consolidation run includes drift detection and snapshot steps
- [ ] `model_reflect` return includes `drift_detected` and `weekly_snapshot_taken` fields
- [ ] Drift records are persisted to `um_drift_records` table
