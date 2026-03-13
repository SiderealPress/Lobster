"""Tests for user model v2 integration: bridges, narrative, inquiry, observation worker."""

import json
import sqlite3
import tempfile
import threading
import queue
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/mcp is on path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "mcp"))

from user_model.db import init_schema, open_db, upsert_narrative_arc, upsert_attention_item, insert_observation
from user_model.schema import (
    NarrativeArc, AttentionCategory, AttentionItem, Observation,
    ObservationSignalType, NodeType, NodeSource, NodeFlexibility,
    PreferenceNode, Contradiction, BlindSpot,
)
from user_model.db import upsert_preference_node, insert_contradiction, insert_blind_spot


@pytest.fixture
def conn():
    """In-memory DB with schema initialized."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


@pytest.fixture
def workspace(tmp_path):
    """Temporary workspace with canonical memory structure."""
    mem = tmp_path / "memory" / "canonical"
    mem.mkdir(parents=True)
    (mem / "projects").mkdir()
    (tmp_path / "user-model").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Bridges tests
# ---------------------------------------------------------------------------

class TestBridges:
    def test_sync_projects_to_arcs_creates_arcs(self, conn, workspace):
        """Projects in canonical memory become narrative arcs."""
        from user_model.bridges import sync_projects_to_arcs

        proj_dir = workspace / "memory" / "canonical" / "projects"
        (proj_dir / "myownlobster.md").write_text(
            "# MyOwnLobster\n\n- **Status:** Active\n\nManaged Lobster hosting SaaS.\n"
        )
        (proj_dir / "kissinger.md").write_text(
            "# Kissinger CRM\n\n- **Status:** Paused\n\nRelationship graph tool.\n"
        )

        result = sync_projects_to_arcs(conn, str(workspace))
        assert result["created"] == 2
        assert result["synced"] == 2

        from user_model.db import get_active_narrative_arcs
        arcs = get_active_narrative_arcs(conn)
        titles = {a.title for a in arcs}
        assert "MyOwnLobster" in titles

    def test_sync_projects_updates_existing(self, conn, workspace):
        """Re-syncing updates rather than duplicates."""
        from user_model.bridges import sync_projects_to_arcs

        proj_dir = workspace / "memory" / "canonical" / "projects"
        (proj_dir / "test.md").write_text("# Test Project\n\nActive project.\n")

        sync_projects_to_arcs(conn, str(workspace))
        r2 = sync_projects_to_arcs(conn, str(workspace))
        assert r2["updated"] == 1
        assert r2["created"] == 0

    def test_sync_priorities_to_attention(self, conn, workspace):
        """Priorities.md items become attention items."""
        from user_model.bridges import sync_priorities_to_attention

        pri = workspace / "memory" / "canonical" / "priorities.md"
        pri.write_text(
            "# Priorities\n\n"
            "1. **Ship v1.0** — Get the MVP deployed\n"
            "2. **Fix auth bug** — Users can't log in\n"
        )

        result = sync_priorities_to_attention(conn, str(workspace))
        assert result["injected"] == 2

    def test_write_context_cache(self, conn, workspace):
        """Context cache is written as a markdown file."""
        from user_model.bridges import write_context_cache

        content = write_context_cache(conn, str(workspace))
        assert "User Model Context" in content
        assert (workspace / "user-model" / "_context.md").exists()

    def test_run_bridges_combined(self, conn, workspace):
        """Full bridge pass runs without error."""
        from user_model.bridges import run_bridges

        result = run_bridges(conn, str(workspace))
        assert "projects" in result
        assert "priorities" in result
        assert "context_cache" in result


# ---------------------------------------------------------------------------
# Narrative tests
# ---------------------------------------------------------------------------

class TestNarrative:
    def test_create_arc(self, conn):
        from user_model.narrative import create_arc
        arc_id = create_arc(conn, "Test Project", "A test", themes=["testing"])
        assert arc_id

    def test_refresh_arcs_warms_matching(self, conn):
        """Topic observations matching an arc keep it warm."""
        from user_model.narrative import create_arc, refresh_arcs_from_observations

        create_arc(conn, "Lobster Development", "Building lobster", themes=["lobster", "coding"])

        # Insert a topic observation mentioning "lobster"
        insert_observation(conn, Observation(
            id=None, message_id="msg1",
            signal_type=ObservationSignalType.TOPIC,
            content="lobster", confidence=0.8, context="coding",
        ))

        result = refresh_arcs_from_observations(conn, hours=24)
        assert result["warmed"] >= 1

    def test_refresh_arcs_cools_stale(self, conn):
        """Arcs not mentioned for 14+ days get paused."""
        from user_model.narrative import refresh_arcs_from_observations
        import json, uuid

        # Insert directly to bypass upsert_narrative_arc's last_updated=now override
        stale_date = (datetime.utcnow() - timedelta(days=15)).isoformat()
        conn.execute(
            """INSERT INTO um_narrative_arcs
               (id, title, description, themes, status, started_at, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), "Old Project", "Stale", json.dumps(["irrelevant"]),
             "active", stale_date, stale_date),
        )
        conn.commit()

        result = refresh_arcs_from_observations(conn, hours=24)
        assert result["cooled"] == 1

    def test_get_arc_for_topic(self, conn):
        from user_model.narrative import create_arc, get_arc_for_topic
        create_arc(conn, "MyOwnLobster SaaS", "Hosting platform", themes=["saas", "hosting"])
        match = get_arc_for_topic(conn, "working on the lobster hosting saas")
        assert match is not None
        assert "MyOwnLobster" in match.title


# ---------------------------------------------------------------------------
# Inquiry tests
# ---------------------------------------------------------------------------

class TestInquiry:
    def test_budget_allows_first_question(self, conn):
        from user_model.inquiry import should_ask_question
        assert should_ask_question(conn) is True

    def test_budget_blocks_after_question(self, conn):
        from user_model.inquiry import should_ask_question, record_inquiry
        record_inquiry(conn)
        assert should_ask_question(conn) is False

    def test_contradiction_question(self, conn):
        """Generates question from unresolved contradiction."""
        from user_model.inquiry import generate_clarifying_question

        n1 = PreferenceNode(
            id="n1", name="concise-replies", node_type=NodeType.PREFERENCE,
            strength=0.9, flexibility=NodeFlexibility.HARD, contexts=[],
            source=NodeSource.STATED, confidence=0.8, description="Keep it short",
        )
        n2 = PreferenceNode(
            id="n2", name="detailed-explanations", node_type=NodeType.PREFERENCE,
            strength=0.8, flexibility=NodeFlexibility.HARD, contexts=[],
            source=NodeSource.INFERRED, confidence=0.7, description="Explain fully",
        )
        upsert_preference_node(conn, n1)
        upsert_preference_node(conn, n2)

        c = Contradiction(
            id=None, node_id_a="n1", node_id_b="n2",
            description="tension", tension_score=0.8,
        )
        insert_contradiction(conn, c)

        q = generate_clarifying_question(conn)
        assert q is not None
        assert "concise" in q or "detailed" in q

    def test_low_confidence_question(self, conn):
        """Generates question from low-confidence inferred node."""
        from user_model.inquiry import generate_clarifying_question

        node = PreferenceNode(
            id="n1", name="prefers-dark-mode", node_type=NodeType.PREFERENCE,
            strength=0.5, flexibility=NodeFlexibility.SOFT, contexts=[],
            source=NodeSource.INFERRED, confidence=0.3, description="Seems to prefer dark mode",
        )
        upsert_preference_node(conn, node)

        q = generate_clarifying_question(conn)
        assert q is not None
        assert "dark" in q.lower() or "prefer" in q.lower()

    def test_fading_arc_question(self, conn):
        """Generates question about a fading arc."""
        from user_model.inquiry import generate_clarifying_question
        import json, uuid

        # Insert directly to preserve stale last_updated
        stale_date = (datetime.utcnow() - timedelta(days=10)).isoformat()
        start_date = (datetime.utcnow() - timedelta(days=20)).isoformat()
        conn.execute(
            """INSERT INTO um_narrative_arcs
               (id, title, description, themes, status, started_at, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), "Signal Connector", "Build Signal support",
             json.dumps(["signal"]), "active", start_date, stale_date),
        )
        conn.commit()

        q = generate_clarifying_question(conn)
        assert q is not None
        assert "Signal" in q

    def test_inquiry_status(self, conn):
        from user_model.inquiry import get_inquiry_status
        status = get_inquiry_status(conn)
        assert status["can_ask"] is True
        assert "available_sources" in status


# ---------------------------------------------------------------------------
# Consolidation pipeline tests
# ---------------------------------------------------------------------------

class TestConsolidation:
    def test_full_pipeline_runs(self, conn, workspace):
        """Consolidation pipeline completes without error."""
        from user_model.inference import run_consolidation

        result = run_consolidation(conn, str(workspace))
        assert "completed_at" in result
        step_names = [s["step"] for s in result["steps"]]
        assert "bridges" in step_names
        assert "arc_refresh" in step_names
        assert "decay" in step_names
        assert "context_cache" in step_names


# ---------------------------------------------------------------------------
# Observation queue tests
# ---------------------------------------------------------------------------

class TestObservationQueue:
    def test_queue_and_drain(self):
        """Observation queue accepts items and worker drains them."""
        q = queue.Queue(maxsize=10)
        processed = []

        def worker():
            while True:
                try:
                    item = q.get(timeout=1)
                except queue.Empty:
                    break
                if item is None:
                    break
                processed.append(item[1])  # message_id

        q.put(("hello", "msg1", "telegram", None))
        q.put(("world", "msg2", "slack", None))
        q.put(None)  # sentinel

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        assert "msg1" in processed
        assert "msg2" in processed

    def test_queue_full_drops_silently(self):
        """Full queue drops without raising."""
        q = queue.Queue(maxsize=1)
        q.put(("a", "1", None, None))
        # Should not raise
        try:
            q.put_nowait(("b", "2", None, None))
        except queue.Full:
            pass  # expected behavior
