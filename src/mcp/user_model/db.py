"""
SQLite schema initialization and CRUD operations for the User Model subsystem.

Depends only on: sqlite3 (stdlib), schema.py (zero deps)
All tables are namespaced with "um_" prefix to avoid collision with other subsystems.

Schema migration strategy: versioned, idempotent, forward-only.
"""

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import (
    AttentionCategory,
    AttentionItem,
    BlindSpot,
    Contradiction,
    EmotionalState,
    LifePattern,
    ModelMetadata,
    NarrativeArc,
    NodeFlexibility,
    NodeSource,
    NodeType,
    Observation,
    ObservationSignalType,
    PreferenceNode,
)

CURRENT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema initialization with migration support."""
    conn.row_factory = sqlite3.Row
    current = _get_schema_version(conn)
    if current < 1:
        _apply_v1(conn)
    _set_schema_version(conn, CURRENT_SCHEMA_VERSION)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM um_metadata WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO um_metadata (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )
    conn.commit()


def _apply_v1(conn: sqlite3.Connection) -> None:
    """Apply version 1 schema — all CREATE TABLE IF NOT EXISTS."""
    conn.executescript("""
        -- Metadata table (key-value store for version tracking and config)
        CREATE TABLE IF NOT EXISTS um_metadata (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Observations: raw signals extracted from user messages
        CREATE TABLE IF NOT EXISTS um_observations (
            id           TEXT PRIMARY KEY,
            message_id   TEXT NOT NULL,
            signal_type  TEXT NOT NULL,
            content      TEXT NOT NULL,
            confidence   REAL NOT NULL DEFAULT 0.7,
            context      TEXT NOT NULL DEFAULT '',
            metadata     TEXT NOT NULL DEFAULT '{}',
            observed_at  TEXT NOT NULL,
            processed    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS um_obs_signal_type ON um_observations(signal_type);
        CREATE INDEX IF NOT EXISTS um_obs_observed_at ON um_observations(observed_at);
        CREATE INDEX IF NOT EXISTS um_obs_processed ON um_observations(processed);

        -- Preference graph nodes
        CREATE TABLE IF NOT EXISTS um_preference_nodes (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            node_type      TEXT NOT NULL,
            strength       REAL NOT NULL DEFAULT 0.7,
            flexibility    TEXT NOT NULL DEFAULT 'soft',
            contexts       TEXT NOT NULL DEFAULT '[]',
            source         TEXT NOT NULL DEFAULT 'inferred',
            confidence     REAL NOT NULL DEFAULT 0.7,
            description    TEXT NOT NULL DEFAULT '',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_observed  TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            decay_rate     REAL NOT NULL DEFAULT 0.01
        );
        CREATE INDEX IF NOT EXISTS um_pref_type ON um_preference_nodes(node_type);
        CREATE INDEX IF NOT EXISTS um_pref_confidence ON um_preference_nodes(confidence);

        -- Preference graph edges (parent → child, overrides)
        CREATE TABLE IF NOT EXISTS um_preference_edges (
            id          TEXT PRIMARY KEY,
            source_id   TEXT NOT NULL REFERENCES um_preference_nodes(id),
            target_id   TEXT NOT NULL REFERENCES um_preference_nodes(id),
            edge_type   TEXT NOT NULL,  -- 'derives_from' | 'overrides'
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS um_edge_source ON um_preference_edges(source_id);
        CREATE INDEX IF NOT EXISTS um_edge_target ON um_preference_edges(target_id);

        -- Emotional state snapshots (VAD model)
        CREATE TABLE IF NOT EXISTS um_emotional_states (
            id          TEXT PRIMARY KEY,
            valence     REAL NOT NULL,
            arousal     REAL NOT NULL,
            dominance   REAL NOT NULL,
            trigger     TEXT,
            context     TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL,
            confidence  REAL NOT NULL DEFAULT 0.7
        );
        CREATE INDEX IF NOT EXISTS um_emotion_recorded ON um_emotional_states(recorded_at);

        -- Blind spots
        CREATE TABLE IF NOT EXISTS um_blind_spots (
            id          TEXT PRIMARY KEY,
            category    TEXT NOT NULL,
            description TEXT NOT NULL,
            evidence    TEXT NOT NULL DEFAULT '',
            surfaced    INTEGER NOT NULL DEFAULT 0,
            confidence  REAL NOT NULL DEFAULT 0.6,
            created_at  TEXT NOT NULL
        );

        -- Contradictions
        CREATE TABLE IF NOT EXISTS um_contradictions (
            id            TEXT PRIMARY KEY,
            node_id_a     TEXT NOT NULL,
            node_id_b     TEXT NOT NULL,
            description   TEXT NOT NULL,
            tension_score REAL NOT NULL DEFAULT 0.5,
            resolved      INTEGER NOT NULL DEFAULT 0,
            resolution    TEXT,
            detected_at   TEXT NOT NULL
        );

        -- Narrative arcs
        CREATE TABLE IF NOT EXISTS um_narrative_arcs (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            themes       TEXT NOT NULL DEFAULT '[]',
            status       TEXT NOT NULL DEFAULT 'active',
            started_at   TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            resolution   TEXT
        );
        CREATE INDEX IF NOT EXISTS um_arc_status ON um_narrative_arcs(status);

        -- Life patterns
        CREATE TABLE IF NOT EXISTS um_life_patterns (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            description    TEXT NOT NULL DEFAULT '',
            stage          TEXT NOT NULL DEFAULT 'forming',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            confidence     REAL NOT NULL DEFAULT 0.6,
            first_seen     TEXT NOT NULL,
            last_seen      TEXT NOT NULL
        );

        -- Attention stack
        CREATE TABLE IF NOT EXISTS um_attention_items (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category    TEXT NOT NULL,
            score       REAL NOT NULL DEFAULT 0.5,
            context     TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT '',
            metadata    TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL,
            expires_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS um_att_score ON um_attention_items(score DESC);
        CREATE INDEX IF NOT EXISTS um_att_category ON um_attention_items(category);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Observation CRUD
# ---------------------------------------------------------------------------

def insert_observation(conn: sqlite3.Connection, obs: Observation) -> str:
    """Insert an observation and return its ID."""
    obs_id = _new_id()
    conn.execute(
        """INSERT INTO um_observations
           (id, message_id, signal_type, content, confidence, context,
            metadata, observed_at, processed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            obs_id,
            obs.message_id,
            obs.signal_type.value if isinstance(obs.signal_type, ObservationSignalType) else obs.signal_type,
            obs.content,
            obs.confidence,
            obs.context,
            json.dumps(obs.metadata),
            obs.observed_at.isoformat(),
            1 if obs.processed else 0,
        ),
    )
    conn.commit()
    return obs_id


def get_unprocessed_observations(conn: sqlite3.Connection, limit: int = 100) -> list[Observation]:
    """Get observations not yet consumed by the inference pipeline."""
    rows = conn.execute(
        "SELECT * FROM um_observations WHERE processed = 0 ORDER BY observed_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_observation(r) for r in rows]


def mark_observations_processed(conn: sqlite3.Connection, obs_ids: list[str]) -> None:
    """Mark observations as consumed."""
    placeholders = ",".join("?" * len(obs_ids))
    conn.execute(
        f"UPDATE um_observations SET processed = 1 WHERE id IN ({placeholders})",
        obs_ids,
    )
    conn.commit()


def get_recent_observations(
    conn: sqlite3.Connection,
    hours: int = 24,
    signal_type: str | None = None,
    limit: int = 100,
) -> list[Observation]:
    """Get recent observations, optionally filtered by signal type."""
    cutoff = datetime.utcnow()
    cutoff_iso = cutoff.replace(
        hour=cutoff.hour - min(hours, cutoff.hour),
    ).isoformat()
    # Simpler approach: use strftime subtraction
    if signal_type:
        rows = conn.execute(
            """SELECT * FROM um_observations
               WHERE observed_at > datetime('now', ?)
               AND signal_type = ?
               ORDER BY observed_at DESC LIMIT ?""",
            (f"-{hours} hours", signal_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM um_observations
               WHERE observed_at > datetime('now', ?)
               ORDER BY observed_at DESC LIMIT ?""",
            (f"-{hours} hours", limit),
        ).fetchall()
    return [_row_to_observation(r) for r in rows]


def _row_to_observation(row: sqlite3.Row) -> Observation:
    return Observation(
        id=row["id"],
        message_id=row["message_id"],
        signal_type=ObservationSignalType(row["signal_type"]),
        content=row["content"],
        confidence=row["confidence"],
        context=row["context"],
        metadata=json.loads(row["metadata"]),
        observed_at=datetime.fromisoformat(row["observed_at"]),
        processed=bool(row["processed"]),
    )


# ---------------------------------------------------------------------------
# Preference Node CRUD
# ---------------------------------------------------------------------------

def upsert_preference_node(conn: sqlite3.Connection, node: PreferenceNode) -> str:
    """Insert or update a preference node. Returns node ID."""
    if not node.id:
        node.id = _new_id()
    node.updated_at = datetime.utcnow()
    conn.execute(
        """INSERT INTO um_preference_nodes
           (id, name, node_type, strength, flexibility, contexts, source,
            confidence, description, evidence_count, last_observed,
            created_at, updated_at, decay_rate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, strength=excluded.strength,
             flexibility=excluded.flexibility, contexts=excluded.contexts,
             source=excluded.source, confidence=excluded.confidence,
             description=excluded.description,
             evidence_count=excluded.evidence_count,
             last_observed=excluded.last_observed,
             updated_at=excluded.updated_at,
             decay_rate=excluded.decay_rate""",
        (
            node.id,
            node.name,
            node.node_type.value if isinstance(node.node_type, NodeType) else node.node_type,
            node.strength,
            node.flexibility.value if isinstance(node.flexibility, NodeFlexibility) else node.flexibility,
            json.dumps(node.contexts),
            node.source.value if isinstance(node.source, NodeSource) else node.source,
            node.confidence,
            node.description,
            node.evidence_count,
            node.last_observed.isoformat() if node.last_observed else None,
            node.created_at.isoformat(),
            node.updated_at.isoformat(),
            node.decay_rate,
        ),
    )
    conn.commit()
    return node.id


def get_preference_node(conn: sqlite3.Connection, node_id: str) -> PreferenceNode | None:
    """Get a preference node by ID."""
    row = conn.execute(
        "SELECT * FROM um_preference_nodes WHERE id = ?", (node_id,)
    ).fetchone()
    return _row_to_preference_node(row) if row else None


def get_all_preference_nodes(
    conn: sqlite3.Connection,
    node_type: NodeType | None = None,
    min_confidence: float = 0.0,
) -> list[PreferenceNode]:
    """Get all preference nodes, optionally filtered."""
    if node_type:
        rows = conn.execute(
            """SELECT * FROM um_preference_nodes
               WHERE node_type = ? AND confidence >= ?
               ORDER BY strength DESC""",
            (node_type.value, min_confidence),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM um_preference_nodes
               WHERE confidence >= ?
               ORDER BY node_type, strength DESC""",
            (min_confidence,),
        ).fetchall()
    return [_row_to_preference_node(r) for r in rows]


def get_preferences_for_context(
    conn: sqlite3.Connection,
    contexts: list[str],
    min_confidence: float = 0.5,
) -> list[PreferenceNode]:
    """Get preference nodes relevant to the given contexts (including universal ones)."""
    rows = conn.execute(
        """SELECT * FROM um_preference_nodes
           WHERE confidence >= ?
           ORDER BY strength DESC""",
        (min_confidence,),
    ).fetchall()
    nodes = [_row_to_preference_node(r) for r in rows]
    result = []
    for node in nodes:
        # Universal (empty contexts) or any context matches
        if not node.contexts or any(c in node.contexts for c in contexts):
            result.append(node)
    return result


def add_preference_edge(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    edge_type: str,  # 'derives_from' or 'overrides'
) -> None:
    """Add an edge to the preference graph."""
    # Avoid duplicates
    existing = conn.execute(
        """SELECT id FROM um_preference_edges
           WHERE source_id = ? AND target_id = ? AND edge_type = ?""",
        (source_id, target_id, edge_type),
    ).fetchone()
    if not existing:
        conn.execute(
            """INSERT INTO um_preference_edges (id, source_id, target_id, edge_type, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (_new_id(), source_id, target_id, edge_type, _now_iso()),
        )
        conn.commit()


def _row_to_preference_node(row: sqlite3.Row) -> PreferenceNode:
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
        last_observed=_parse_dt(row["last_observed"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        decay_rate=row["decay_rate"],
    )


# ---------------------------------------------------------------------------
# Emotional State CRUD
# ---------------------------------------------------------------------------

def insert_emotional_state(conn: sqlite3.Connection, state: EmotionalState) -> str:
    """Insert an emotional state snapshot. Returns ID."""
    state_id = _new_id()
    conn.execute(
        """INSERT INTO um_emotional_states
           (id, valence, arousal, dominance, trigger, context, recorded_at, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            state_id,
            state.valence,
            state.arousal,
            state.dominance,
            state.trigger,
            state.context,
            state.recorded_at.isoformat(),
            state.confidence,
        ),
    )
    conn.commit()
    return state_id


def get_emotional_baseline(conn: sqlite3.Connection, days: int = 30) -> dict[str, float] | None:
    """Compute the emotional baseline from recent states."""
    rows = conn.execute(
        """SELECT AVG(valence) as v, AVG(arousal) as a, AVG(dominance) as d,
                  COUNT(*) as n
           FROM um_emotional_states
           WHERE recorded_at > datetime('now', ?)""",
        (f"-{days} days",),
    ).fetchone()
    if not rows or rows["n"] == 0:
        return None
    return {
        "valence": round(rows["v"], 3),
        "arousal": round(rows["a"], 3),
        "dominance": round(rows["d"], 3),
        "sample_count": rows["n"],
    }


def get_recent_emotional_states(
    conn: sqlite3.Connection, limit: int = 10
) -> list[EmotionalState]:
    """Get the most recent emotional state snapshots."""
    rows = conn.execute(
        "SELECT * FROM um_emotional_states ORDER BY recorded_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        EmotionalState(
            id=r["id"],
            valence=r["valence"],
            arousal=r["arousal"],
            dominance=r["dominance"],
            trigger=r["trigger"],
            context=r["context"],
            recorded_at=datetime.fromisoformat(r["recorded_at"]),
            confidence=r["confidence"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Blind Spot CRUD
# ---------------------------------------------------------------------------

def insert_blind_spot(conn: sqlite3.Connection, spot: BlindSpot) -> str:
    """Insert a blind spot. Returns ID."""
    spot_id = _new_id()
    conn.execute(
        """INSERT INTO um_blind_spots
           (id, category, description, evidence, surfaced, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            spot_id,
            spot.category,
            spot.description,
            spot.evidence,
            1 if spot.surfaced else 0,
            spot.confidence,
            spot.created_at.isoformat(),
        ),
    )
    conn.commit()
    return spot_id


def get_blind_spots(
    conn: sqlite3.Connection, surfaced_only: bool = False
) -> list[BlindSpot]:
    """Get blind spots, optionally filtering to surfaced ones only."""
    if surfaced_only:
        rows = conn.execute(
            "SELECT * FROM um_blind_spots WHERE surfaced = 1 ORDER BY confidence DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM um_blind_spots ORDER BY confidence DESC"
        ).fetchall()
    return [
        BlindSpot(
            id=r["id"],
            category=r["category"],
            description=r["description"],
            evidence=r["evidence"],
            surfaced=bool(r["surfaced"]),
            confidence=r["confidence"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Contradiction CRUD
# ---------------------------------------------------------------------------

def insert_contradiction(conn: sqlite3.Connection, c: Contradiction) -> str:
    """Insert a contradiction. Returns ID."""
    c_id = _new_id()
    conn.execute(
        """INSERT INTO um_contradictions
           (id, node_id_a, node_id_b, description, tension_score, resolved,
            resolution, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            c_id,
            c.node_id_a,
            c.node_id_b,
            c.description,
            c.tension_score,
            1 if c.resolved else 0,
            c.resolution,
            c.detected_at.isoformat(),
        ),
    )
    conn.commit()
    return c_id


def get_active_contradictions(conn: sqlite3.Connection) -> list[Contradiction]:
    """Get unresolved contradictions."""
    rows = conn.execute(
        "SELECT * FROM um_contradictions WHERE resolved = 0 ORDER BY tension_score DESC"
    ).fetchall()
    return [
        Contradiction(
            id=r["id"],
            node_id_a=r["node_id_a"],
            node_id_b=r["node_id_b"],
            description=r["description"],
            tension_score=r["tension_score"],
            resolved=bool(r["resolved"]),
            resolution=r["resolution"],
            detected_at=datetime.fromisoformat(r["detected_at"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Narrative Arc CRUD
# ---------------------------------------------------------------------------

def upsert_narrative_arc(conn: sqlite3.Connection, arc: NarrativeArc) -> str:
    """Insert or update a narrative arc. Returns ID."""
    if not arc.id:
        arc.id = _new_id()
    arc.last_updated = datetime.utcnow()
    conn.execute(
        """INSERT INTO um_narrative_arcs
           (id, title, description, themes, status, started_at, last_updated, resolution)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, description=excluded.description,
             themes=excluded.themes, status=excluded.status,
             last_updated=excluded.last_updated,
             resolution=excluded.resolution""",
        (
            arc.id,
            arc.title,
            arc.description,
            json.dumps(arc.themes),
            arc.status,
            arc.started_at.isoformat(),
            arc.last_updated.isoformat(),
            arc.resolution,
        ),
    )
    conn.commit()
    return arc.id


def get_active_narrative_arcs(conn: sqlite3.Connection) -> list[NarrativeArc]:
    """Get active narrative arcs."""
    rows = conn.execute(
        "SELECT * FROM um_narrative_arcs WHERE status = 'active' ORDER BY last_updated DESC"
    ).fetchall()
    return [
        NarrativeArc(
            id=r["id"],
            title=r["title"],
            description=r["description"],
            themes=json.loads(r["themes"]),
            status=r["status"],
            started_at=datetime.fromisoformat(r["started_at"]),
            last_updated=datetime.fromisoformat(r["last_updated"]),
            resolution=r["resolution"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Life Pattern CRUD
# ---------------------------------------------------------------------------

def upsert_life_pattern(conn: sqlite3.Connection, pattern: LifePattern) -> str:
    """Insert or update a life pattern. Returns ID."""
    if not pattern.id:
        pattern.id = _new_id()
    pattern.last_seen = datetime.utcnow()
    conn.execute(
        """INSERT INTO um_life_patterns
           (id, name, description, stage, evidence_count, confidence, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, description=excluded.description,
             stage=excluded.stage, evidence_count=excluded.evidence_count,
             confidence=excluded.confidence, last_seen=excluded.last_seen""",
        (
            pattern.id,
            pattern.name,
            pattern.description,
            pattern.stage,
            pattern.evidence_count,
            pattern.confidence,
            pattern.first_seen.isoformat(),
            pattern.last_seen.isoformat(),
        ),
    )
    conn.commit()
    return pattern.id


def get_active_life_patterns(conn: sqlite3.Connection) -> list[LifePattern]:
    """Get active (non-broken) life patterns."""
    rows = conn.execute(
        """SELECT * FROM um_life_patterns
           WHERE stage != 'broken'
           ORDER BY confidence DESC""",
    ).fetchall()
    return [
        LifePattern(
            id=r["id"],
            name=r["name"],
            description=r["description"],
            stage=r["stage"],
            evidence_count=r["evidence_count"],
            confidence=r["confidence"],
            first_seen=datetime.fromisoformat(r["first_seen"]),
            last_seen=datetime.fromisoformat(r["last_seen"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Attention Stack CRUD
# ---------------------------------------------------------------------------

def upsert_attention_item(conn: sqlite3.Connection, item: AttentionItem) -> str:
    """Insert or update an attention item. Returns ID."""
    if not item.id:
        item.id = _new_id()
    conn.execute(
        """INSERT INTO um_attention_items
           (id, title, description, category, score, context, source, metadata,
            created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, description=excluded.description,
             category=excluded.category, score=excluded.score,
             context=excluded.context, source=excluded.source,
             metadata=excluded.metadata, expires_at=excluded.expires_at""",
        (
            item.id,
            item.title,
            item.description,
            item.category.value if isinstance(item.category, AttentionCategory) else item.category,
            item.score,
            item.context,
            item.source,
            json.dumps(item.metadata),
            item.created_at.isoformat(),
            item.expires_at.isoformat() if item.expires_at else None,
        ),
    )
    conn.commit()
    return item.id


def get_attention_stack(
    conn: sqlite3.Connection, limit: int = 10
) -> list[AttentionItem]:
    """Get the current attention stack, sorted by score descending."""
    rows = conn.execute(
        """SELECT * FROM um_attention_items
           WHERE (expires_at IS NULL OR expires_at > datetime('now'))
           ORDER BY score DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        AttentionItem(
            id=r["id"],
            title=r["title"],
            description=r["description"],
            category=AttentionCategory(r["category"]),
            score=r["score"],
            context=r["context"],
            source=r["source"],
            metadata=json.loads(r["metadata"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            expires_at=_parse_dt(r["expires_at"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Model Metadata
# ---------------------------------------------------------------------------

def get_model_metadata(conn: sqlite3.Connection) -> ModelMetadata:
    """Read model metadata from DB."""
    def _get(key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM um_metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # Count aggregates
    obs_count = conn.execute(
        "SELECT COUNT(*) as n FROM um_observations"
    ).fetchone()["n"]
    pref_count = conn.execute(
        "SELECT COUNT(*) as n FROM um_preference_nodes"
    ).fetchone()["n"]

    created_str = _get("created_at")
    last_obs_str = _get("last_observation_at")
    last_consol_str = _get("last_consolidation_at")

    return ModelMetadata(
        schema_version=int(_get("schema_version") or "0"),
        owner_id=_get("owner_id"),
        created_at=datetime.fromisoformat(created_str) if created_str else datetime.utcnow(),
        last_observation_at=_parse_dt(last_obs_str),
        last_consolidation_at=_parse_dt(last_consol_str),
        observation_count=obs_count,
        preference_node_count=pref_count,
    )


def set_metadata_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a metadata key-value pair."""
    conn.execute(
        "INSERT OR REPLACE INTO um_metadata (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (and initialize) the SQLite database at the given path."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn
