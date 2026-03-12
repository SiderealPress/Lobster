# Phase 1: Enhanced Data Model

*Part of User Model v2 — see [PLAN.md](PLAN.md)*

---

## Goal

Extend the SQLite schema from v1 to v2 without breaking existing functionality. All changes are backward-compatible via `ALTER TABLE` and new tables. The migration is idempotent.

## New Schema Objects

### 1. `um_temporal_snapshots` — Weekly state captures

```sql
CREATE TABLE IF NOT EXISTS um_temporal_snapshots (
    id          TEXT PRIMARY KEY,
    snapshot_at TEXT NOT NULL,
    week_number INTEGER NOT NULL,    -- ISO week (1-53)
    year        INTEGER NOT NULL,
    data        TEXT NOT NULL,       -- JSON blob: serialized preference state
    obs_count   INTEGER NOT NULL DEFAULT 0,  -- how many observations existed at snapshot time
    node_count  INTEGER NOT NULL DEFAULT 0   -- how many preference nodes at snapshot time
);
CREATE INDEX IF NOT EXISTS um_snap_week ON um_temporal_snapshots(year, week_number);
```

**Data blob structure:**
```json
{
  "preferences": [
    {"id": "...", "name": "...", "strength": 0.7, "confidence": 0.8, "type": "value"}
  ],
  "emotional_baseline": {"valence": 0.2, "arousal": 0.5, "dominance": 0.6},
  "active_arc_titles": ["...", "..."],
  "active_pattern_names": ["..."]
}
```

### 2. `um_drift_records` — Detected week-over-week changes

```sql
CREATE TABLE IF NOT EXISTS um_drift_records (
    id              TEXT PRIMARY KEY,
    detected_at     TEXT NOT NULL,
    snapshot_a_id   TEXT NOT NULL REFERENCES um_temporal_snapshots(id),
    snapshot_b_id   TEXT NOT NULL REFERENCES um_temporal_snapshots(id),
    drift_type      TEXT NOT NULL,   -- 'preference_shift'|'value_drift'|'pattern_change'|'emotional_drift'
    description     TEXT NOT NULL,
    magnitude       REAL NOT NULL,   -- 0.0-1.0
    node_id         TEXT,            -- preference node that drifted (if applicable)
    surfaced        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS um_drift_detected ON um_drift_records(detected_at);
CREATE INDEX IF NOT EXISTS um_drift_surfaced ON um_drift_records(surfaced);
```

### 3. `um_activity_rhythm` — Hourly/daily activity patterns

```sql
CREATE TABLE IF NOT EXISTS um_activity_rhythm (
    id             TEXT PRIMARY KEY,
    hour_of_day    INTEGER NOT NULL CHECK (hour_of_day >= 0 AND hour_of_day <= 23),
    day_of_week    INTEGER NOT NULL CHECK (day_of_week >= 0 AND day_of_week <= 6),
    message_count  INTEGER NOT NULL DEFAULT 0,
    total_length   INTEGER NOT NULL DEFAULT 0,   -- cumulative character count for avg computation
    total_latency  REAL NOT NULL DEFAULT 0.0,    -- cumulative ms for avg computation
    latency_count  INTEGER NOT NULL DEFAULT 0,   -- samples with latency data
    updated_at     TEXT NOT NULL,
    UNIQUE(hour_of_day, day_of_week)
);
```

### 4. `um_inference_cache` — Short-lived prediction cache

```sql
CREATE TABLE IF NOT EXISTS um_inference_cache (
    id          TEXT PRIMARY KEY,
    cache_key   TEXT NOT NULL UNIQUE,
    result      TEXT NOT NULL,       -- JSON prediction
    confidence  REAL NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS um_cache_key ON um_inference_cache(cache_key);
CREATE INDEX IF NOT EXISTS um_cache_expires ON um_inference_cache(expires_at);
```

## Modified Tables

### `um_observations` additions

```sql
ALTER TABLE um_observations ADD COLUMN latency_ms INTEGER;
ALTER TABLE um_observations ADD COLUMN reply_length INTEGER;
ALTER TABLE um_observations ADD COLUMN is_followup INTEGER NOT NULL DEFAULT 0;
ALTER TABLE um_observations ADD COLUMN source_specificity TEXT DEFAULT 'heuristic';
```

- `latency_ms`: milliseconds between previous Lobster reply and this message. NULL if first message.
- `reply_length`: character count of the user's message. Signal for engagement level.
- `is_followup`: 1 if this message continues the same topic as the previous message.
- `source_specificity`: `'explicit'` (user stated it), `'behavioral'` (inferred from behavior), `'heuristic'` (keyword/pattern match).

### `um_preference_nodes` additions

```sql
ALTER TABLE um_preference_nodes ADD COLUMN seed_source TEXT;
ALTER TABLE um_preference_nodes ADD COLUMN decay_rate_override REAL;
ALTER TABLE um_preference_nodes ADD COLUMN temporal_weight REAL NOT NULL DEFAULT 1.0;
```

- `seed_source`: `'owner_toml'`, `'setup_interview'`, `'observed'`, `'stated'`. NULL = unknown.
- `decay_rate_override`: if set, overrides the `decay_rate` field for this node. Seeded values get 0.001.
- `temporal_weight`: recency-weighted importance factor (updated during nightly synthesis).

## New Dataclasses (`schema.py` additions)

```python
@dataclass
class TemporalSnapshot:
    """A weekly snapshot of the preference graph state."""
    id: str | None
    snapshot_at: datetime
    week_number: int
    year: int
    data: dict[str, Any]       # Serialized preference state
    obs_count: int = 0
    node_count: int = 0


@dataclass
class DriftRecord:
    """A detected change between two temporal snapshots."""
    id: str | None
    detected_at: datetime
    snapshot_a_id: str
    snapshot_b_id: str
    drift_type: str            # 'preference_shift'|'value_drift'|'pattern_change'|'emotional_drift'
    description: str
    magnitude: float           # 0.0-1.0
    node_id: str | None = None
    surfaced: bool = False


@dataclass
class ActivityRhythm:
    """Hourly/daily activity pattern entry."""
    id: str | None
    hour_of_day: int           # 0-23
    day_of_week: int           # 0=Monday, 6=Sunday
    message_count: int = 0
    total_length: int = 0
    total_latency: float = 0.0
    latency_count: int = 0
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class InferenceCacheEntry:
    """A cached inference result."""
    id: str | None
    cache_key: str
    result: dict[str, Any]
    confidence: float
    created_at: datetime
    expires_at: datetime
    hit_count: int = 0
```

## Migration Strategy

The migration runs in `_apply_v2(conn)` called from `init_schema()`:

```python
def init_schema(conn):
    current = _get_schema_version(conn)
    if current < 1:
        _apply_v1(conn)
    if current < 2:
        _apply_v2(conn)       # NEW
    _set_schema_version(conn, 2)
```

`_apply_v2` uses `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern with a try/except for SQLite compatibility (SQLite doesn't support IF NOT EXISTS in ALTER TABLE — we check column existence first).

## CRUD Functions Added to `db.py`

### Temporal Snapshots
- `insert_temporal_snapshot(conn, snapshot) -> str`
- `get_latest_snapshot(conn) -> TemporalSnapshot | None`
- `get_snapshot_by_week(conn, year, week) -> TemporalSnapshot | None`
- `get_snapshots_since(conn, days) -> list[TemporalSnapshot]`

### Drift Records
- `insert_drift_record(conn, record) -> str`
- `get_recent_drifts(conn, limit=10) -> list[DriftRecord]`
- `get_unsurfaced_drifts(conn) -> list[DriftRecord]`

### Activity Rhythm
- `update_activity_rhythm(conn, hour, day, length, latency_ms=None) -> None`
- `get_activity_rhythm(conn) -> list[ActivityRhythm]`
- `get_peak_activity_hours(conn) -> list[int]` — top 3 most active hours

### Inference Cache
- `get_cached_inference(conn, cache_key) -> dict | None` — returns result if not expired
- `set_cached_inference(conn, cache_key, result, confidence, ttl_minutes=30) -> str`
- `cleanup_expired_cache(conn) -> int` — removes expired entries, returns count

## Test Plan

- [ ] Schema migration from v1 runs without error on existing DBs
- [ ] ALTER TABLE additions are idempotent (running twice doesn't fail)
- [ ] All new CRUD functions have round-trip tests (insert → retrieve)
- [ ] TemporalSnapshot data blob serializes/deserializes cleanly
- [ ] Activity rhythm correctly handles the (hour, day) unique constraint
- [ ] Inference cache correctly expires entries and reports cache misses
