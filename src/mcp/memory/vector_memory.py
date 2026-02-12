"""
Vector Memory Backend - Layer 1: Real-Time Event Store

Uses SQLite + sqlite-vec for vector search + FTS5 for keyword search.
Embeddings are generated locally using fastembed (all-MiniLM-L6-v2).
CPU usage is logged for every embedding operation.

This is the primary, fast memory backend. If it fails to initialize,
the system falls back to StaticMemory.
"""

import json
import logging
import sqlite3
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

from .provider import MemoryEvent

log = logging.getLogger("lobster-memory")

# Embedding dimension for all-MiniLM-L6-v2
EMBEDDING_DIM = 384

# Hybrid search weights
VECTOR_WEIGHT = 0.70
KEYWORD_WEIGHT = 0.30

# Default DB location
DEFAULT_DB_PATH = Path.home() / "lobster" / "data" / "memory.db"


def _serialize_vector(vec: list[float]) -> bytes:
    """Pack a float list into a binary blob for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_vector(blob: bytes) -> list[float]:
    """Unpack a binary blob into a float list."""
    n = len(blob) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{n}f", blob))


class EmbeddingModel:
    """Lazy-loaded embedding model with CPU usage logging.

    Uses fastembed with all-MiniLM-L6-v2 for local-only embeddings.
    No external API calls are made.
    """

    def __init__(self):
        self._model = None
        self._process = psutil.Process()

    def _ensure_loaded(self):
        """Lazy-load the model on first use."""
        if self._model is None:
            log.info("Loading embedding model: all-MiniLM-L6-v2 via fastembed")
            start = time.monotonic()
            from fastembed import TextEmbedding
            self._model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
            elapsed = time.monotonic() - start
            log.info(f"Embedding model loaded in {elapsed:.2f}s")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings with CPU usage logging.

        Args:
            texts: List of strings to embed.

        Returns:
            List of embedding vectors (each 384-dim float list).
        """
        self._ensure_loaded()

        # Measure CPU before
        cpu_before = self._process.cpu_percent(interval=None)
        start = time.monotonic()

        embeddings = list(self._model.embed(texts))

        elapsed = time.monotonic() - start
        cpu_after = self._process.cpu_percent(interval=None)

        # Log CPU usage for every embedding operation
        log.info(
            f"Embedding: {len(texts)} text(s), "
            f"{elapsed:.3f}s, "
            f"CPU before={cpu_before:.1f}% after={cpu_after:.1f}%"
        )

        return [emb.tolist() if hasattr(emb, "tolist") else list(emb) for emb in embeddings]

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text string."""
        return self.embed([text])[0]


class VectorMemory:
    """SQLite + sqlite-vec + FTS5 hybrid memory backend.

    Layer 1 of the three-layer memory system. Provides fast hybrid
    search combining cosine similarity (70%) and BM25 keyword (30%).

    The vector DB is an acceleration layer - static files remain
    the source of truth. This DB can be deleted and rebuilt.
    """

    def __init__(self, db_path: Path = None):
        self._db_path = db_path or DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = EmbeddingModel()
        self._conn = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize SQLite with sqlite-vec and FTS5."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row

        # Load sqlite-vec extension
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        # Create main events table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                source TEXT NOT NULL,
                project TEXT,
                content TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                consolidated INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Create FTS5 virtual table for keyword search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                content,
                project,
                type,
                source,
                content=events,
                content_rowid=id
            )
        """)

        # Create triggers to keep FTS5 in sync
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                INSERT INTO events_fts(rowid, content, project, type, source)
                VALUES (new.id, new.content, new.project, new.type, new.source);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                INSERT INTO events_fts(events_fts, rowid, content, project, type, source)
                VALUES ('delete', old.id, old.content, old.project, old.type, old.source);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
                INSERT INTO events_fts(events_fts, rowid, content, project, type, source)
                VALUES ('delete', old.id, old.content, old.project, old.type, old.source);
                INSERT INTO events_fts(rowid, content, project, type, source)
                VALUES (new.id, new.content, new.project, new.type, new.source);
            END
        """)

        # Create sqlite-vec virtual table for vector search
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS events_vec USING vec0(
                embedding float[{EMBEDDING_DIM}]
            )
        """)

        # Index for time-range queries
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_timestamp
            ON events(timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_consolidated
            ON events(consolidated)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_project
            ON events(project)
        """)

        conn.commit()
        return conn

    def store(self, event: MemoryEvent) -> int:
        """Store an event with its embedding.

        Returns the assigned event ID.
        """
        # Generate embedding
        embedding = self._embedder.embed_one(event.content)

        cursor = self._conn.execute(
            """
            INSERT INTO events (timestamp, type, source, project, content, metadata, consolidated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.timestamp.isoformat(),
                event.type,
                event.source,
                event.project,
                event.content,
                json.dumps(event.metadata),
                1 if event.consolidated else 0,
            ),
        )
        event_id = cursor.lastrowid

        # Store embedding vector
        vec_blob = _serialize_vector(embedding)
        self._conn.execute(
            "INSERT INTO events_vec(rowid, embedding) VALUES (?, ?)",
            (event_id, vec_blob),
        )

        self._conn.commit()
        event.id = event_id
        return event_id

    def search(self, query: str, limit: int = 10, project: str = None) -> list[MemoryEvent]:
        """Hybrid search: 70% cosine similarity + 30% BM25 keyword.

        Falls back to keyword-only if vector search fails.
        """
        try:
            return self._hybrid_search(query, limit, project)
        except Exception as e:
            log.warning(f"Hybrid search failed, falling back to keyword: {e}")
            return self._keyword_search(query, limit, project)

    def _hybrid_search(self, query: str, limit: int, project: str = None) -> list[MemoryEvent]:
        """Combine vector similarity and BM25 keyword scores."""
        # Vector search
        query_embedding = self._embedder.embed_one(query)
        vec_blob = _serialize_vector(query_embedding)

        # Get top candidates from vector search (fetch more than limit for merging)
        fetch_limit = limit * 3
        vec_results = self._conn.execute(
            """
            SELECT rowid, distance
            FROM events_vec
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (vec_blob, fetch_limit),
        ).fetchall()

        # Get top candidates from FTS5 keyword search
        # Escape FTS5 special characters in query
        fts_query = query.replace('"', '""')
        try:
            fts_results = self._conn.execute(
                """
                SELECT rowid, rank
                FROM events_fts
                WHERE events_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, fetch_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS query syntax error - try with quoted phrase
            try:
                fts_results = self._conn.execute(
                    """
                    SELECT rowid, rank
                    FROM events_fts
                    WHERE events_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (f'"{fts_query}"', fetch_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                fts_results = []

        # Build score maps
        # Vector: convert distance to similarity (lower distance = higher similarity)
        vec_scores = {}
        if vec_results:
            max_dist = max(r["distance"] for r in vec_results) or 1.0
            for r in vec_results:
                # Normalize: 1.0 for distance=0, 0.0 for max distance
                vec_scores[r["rowid"]] = 1.0 - (r["distance"] / (max_dist + 1e-6))

        # Keyword: BM25 rank (already negative, more negative = better match)
        kw_scores = {}
        if fts_results:
            min_rank = min(r["rank"] for r in fts_results) or -1.0
            for r in fts_results:
                # Normalize: 1.0 for best rank, 0.0 for worst
                kw_scores[r["rowid"]] = r["rank"] / (min_rank - 1e-6) if min_rank < 0 else 0.0

        # Merge scores with weights
        all_ids = set(vec_scores.keys()) | set(kw_scores.keys())
        combined = {}
        for event_id in all_ids:
            v_score = vec_scores.get(event_id, 0.0)
            k_score = kw_scores.get(event_id, 0.0)
            combined[event_id] = VECTOR_WEIGHT * v_score + KEYWORD_WEIGHT * k_score

        # Sort by combined score descending
        ranked_ids = sorted(combined.keys(), key=lambda eid: combined[eid], reverse=True)

        # Apply project filter if specified
        if project:
            ranked_ids = [eid for eid in ranked_ids if self._event_matches_project(eid, project)]

        # Fetch full events for top results
        return self._fetch_events(ranked_ids[:limit])

    def _keyword_search(self, query: str, limit: int, project: str = None) -> list[MemoryEvent]:
        """Keyword-only search using FTS5."""
        fts_query = query.replace('"', '""')

        where_clause = ""
        params = []

        if project:
            where_clause = "AND e.project = ?"
            params.append(project)

        try:
            rows = self._conn.execute(
                f"""
                SELECT e.*
                FROM events e
                JOIN events_fts f ON e.id = f.rowid
                WHERE events_fts MATCH ?
                {where_clause}
                ORDER BY f.rank
                LIMIT ?
                """,
                [fts_query] + params + [limit],
            ).fetchall()
        except sqlite3.OperationalError:
            # Try quoted phrase
            try:
                rows = self._conn.execute(
                    f"""
                    SELECT e.*
                    FROM events e
                    JOIN events_fts f ON e.id = f.rowid
                    WHERE events_fts MATCH ?
                    {where_clause}
                    ORDER BY f.rank
                    LIMIT ?
                    """,
                    [f'"{fts_query}"'] + params + [limit],
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []

        return [self._row_to_event(r) for r in rows]

    def _event_matches_project(self, event_id: int, project: str) -> bool:
        """Check if an event belongs to a project."""
        row = self._conn.execute(
            "SELECT project FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return row is not None and row["project"] == project

    def _fetch_events(self, event_ids: list[int]) -> list[MemoryEvent]:
        """Fetch full event objects by ID, preserving order."""
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE id IN ({placeholders})",
            event_ids,
        ).fetchall()

        # Build map and return in original order
        row_map = {r["id"]: r for r in rows}
        return [self._row_to_event(row_map[eid]) for eid in event_ids if eid in row_map]

    def recent(self, hours: int = 24, project: str = None) -> list[MemoryEvent]:
        """Get events from the last N hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        if project:
            rows = self._conn.execute(
                """
                SELECT * FROM events
                WHERE timestamp >= ? AND project = ?
                ORDER BY timestamp DESC
                """,
                (cutoff, project),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM events
                WHERE timestamp >= ?
                ORDER BY timestamp DESC
                """,
                (cutoff,),
            ).fetchall()

        return [self._row_to_event(r) for r in rows]

    def unconsolidated(self) -> list[MemoryEvent]:
        """Get all events that haven't been consolidated."""
        rows = self._conn.execute(
            """
            SELECT * FROM events
            WHERE consolidated = 0
            ORDER BY timestamp ASC
            """,
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def mark_consolidated(self, event_ids: list[int]) -> None:
        """Mark events as consolidated."""
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        self._conn.execute(
            f"UPDATE events SET consolidated = 1 WHERE id IN ({placeholders})",
            event_ids,
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def event_count(self) -> int:
        """Return total number of stored events."""
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM events").fetchone()
        return row["cnt"]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> MemoryEvent:
        """Convert a SQLite row to a MemoryEvent."""
        metadata = row["metadata"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}

        ts = row["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)

        return MemoryEvent(
            id=row["id"],
            timestamp=ts,
            type=row["type"],
            source=row["source"],
            project=row["project"],
            content=row["content"],
            metadata=metadata,
            consolidated=bool(row["consolidated"]),
        )
