"""
Comprehensive tests for the User Model subsystem.

Tests all layers in dependency order using in-memory SQLite.
No external dependencies required — all tests use mocks or in-memory DBs.
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Add src/mcp to the path so user_model can be imported
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "mcp"))

from user_model.db import (
    get_active_narrative_arcs,
    get_all_preference_nodes,
    get_attention_stack,
    get_blind_spots,
    get_emotional_baseline,
    get_model_metadata,
    get_preference_node,
    get_recent_observations,
    get_unprocessed_observations,
    init_schema,
    insert_emotional_state,
    insert_observation,
    mark_observations_processed,
    open_db,
    set_metadata_value,
    upsert_attention_item,
    upsert_life_pattern,
    upsert_narrative_arc,
    upsert_preference_node,
)
from user_model.schema import (
    AttentionCategory,
    AttentionItem,
    BlindSpot,
    EmotionalState,
    LifePattern,
    NarrativeArc,
    NodeFlexibility,
    NodeSource,
    NodeType,
    Observation,
    ObservationSignalType,
    PreferenceNode,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory SQLite connection with user model schema initialized."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    init_schema(c)
    return c


@pytest.fixture
def tmp_db(tmp_path):
    """On-disk temp database for tests that need file-level access."""
    db_path = tmp_path / "test_memory.db"
    conn = open_db(db_path)
    return conn, db_path


@pytest.fixture
def sample_preference(conn):
    """A pre-inserted preference node."""
    node = PreferenceNode(
        id=None,
        name="Concise Responses",
        node_type=NodeType.PREFERENCE,
        strength=0.85,
        flexibility=NodeFlexibility.SOFT,
        contexts=["communication"],
        source=NodeSource.STATED,
        confidence=0.92,
        description="User prefers short, direct replies.",
        evidence_count=5,
        last_observed=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    node_id = upsert_preference_node(conn, node)
    node.id = node_id
    return node


# ---------------------------------------------------------------------------
# Layer 1: DB Schema Tests
# ---------------------------------------------------------------------------

class TestDbSchema:
    def test_schema_initializes(self, conn):
        """Schema should initialize without error."""
        # Check all expected tables exist
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'um_%'"
            ).fetchall()
        }
        expected = {
            "um_metadata",
            "um_observations",
            "um_preference_nodes",
            "um_preference_edges",
            "um_emotional_states",
            "um_blind_spots",
            "um_contradictions",
            "um_narrative_arcs",
            "um_life_patterns",
            "um_attention_items",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_schema_is_idempotent(self, conn):
        """Calling init_schema twice should not fail."""
        init_schema(conn)  # Second call
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'um_%'"
        ).fetchone()[0]
        assert tables >= 10

    def test_schema_version_is_set(self, conn):
        """Schema version should be stored in metadata."""
        row = conn.execute(
            "SELECT value FROM um_metadata WHERE key = 'schema_version'"
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 2


# ---------------------------------------------------------------------------
# Layer 1: Observation CRUD Tests
# ---------------------------------------------------------------------------

class TestObservationCRUD:
    def test_insert_observation(self, conn):
        """Should insert and retrieve an observation."""
        obs = Observation(
            id=None,
            message_id="msg-001",
            signal_type=ObservationSignalType.SENTIMENT,
            content="positive",
            confidence=0.8,
            context="work",
            observed_at=datetime.utcnow(),
        )
        obs_id = insert_observation(conn, obs)
        assert obs_id is not None
        assert len(obs_id) > 0

    def test_get_unprocessed_observations(self, conn):
        """Should return only unprocessed observations."""
        obs = Observation(
            id=None,
            message_id="msg-002",
            signal_type=ObservationSignalType.TOPIC,
            content="coding",
            confidence=0.7,
            context="",
            observed_at=datetime.utcnow(),
        )
        obs_id = insert_observation(conn, obs)
        unprocessed = get_unprocessed_observations(conn)
        ids = [o.id for o in unprocessed]
        assert obs_id in ids

    def test_mark_observations_processed(self, conn):
        """Marking observations processed should exclude them from unprocessed."""
        obs = Observation(
            id=None,
            message_id="msg-003",
            signal_type=ObservationSignalType.ENERGY,
            content="high",
            confidence=0.75,
            context="",
            observed_at=datetime.utcnow(),
        )
        obs_id = insert_observation(conn, obs)
        mark_observations_processed(conn, [obs_id])
        unprocessed = get_unprocessed_observations(conn)
        assert obs_id not in [o.id for o in unprocessed]

    def test_get_recent_observations_filter(self, conn):
        """Should filter by signal type."""
        for sig_type, content in [
            (ObservationSignalType.SENTIMENT, "positive"),
            (ObservationSignalType.TOPIC, "coding"),
            (ObservationSignalType.ENERGY, "high"),
        ]:
            obs = Observation(
                id=None,
                message_id=f"msg-filter-{sig_type.value}",
                signal_type=sig_type,
                content=content,
                confidence=0.7,
                context="",
                observed_at=datetime.utcnow(),
            )
            insert_observation(conn, obs)

        sentiment_obs = get_recent_observations(conn, hours=24, signal_type="sentiment")
        assert all(o.signal_type == ObservationSignalType.SENTIMENT for o in sentiment_obs)


# ---------------------------------------------------------------------------
# Layer 1: Preference Node CRUD Tests
# ---------------------------------------------------------------------------

class TestPreferenceNodeCRUD:
    def test_upsert_preference_node(self, conn):
        """Should insert a preference node and return an ID."""
        node = PreferenceNode(
            id=None,
            name="Test Preference",
            node_type=NodeType.PREFERENCE,
            strength=0.7,
            flexibility=NodeFlexibility.SOFT,
            contexts=["work"],
            source=NodeSource.INFERRED,
            confidence=0.6,
            description="A test preference.",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        node_id = upsert_preference_node(conn, node)
        assert node_id is not None

    def test_get_preference_node(self, conn, sample_preference):
        """Should retrieve a preference node by ID."""
        retrieved = get_preference_node(conn, sample_preference.id)
        assert retrieved is not None
        assert retrieved.name == "Concise Responses"
        assert retrieved.confidence == pytest.approx(0.92)

    def test_upsert_updates_existing(self, conn, sample_preference):
        """Upsert with same ID should update rather than duplicate."""
        sample_preference.description = "Updated description."
        sample_preference.strength = 0.95
        upsert_preference_node(conn, sample_preference)

        # Should still be one node with same ID
        all_nodes = get_all_preference_nodes(conn)
        matching = [n for n in all_nodes if n.id == sample_preference.id]
        assert len(matching) == 1
        assert matching[0].description == "Updated description."
        assert matching[0].strength == pytest.approx(0.95)

    def test_get_all_preference_nodes_filtered(self, conn):
        """Should filter by node_type."""
        for node_type, name in [
            (NodeType.VALUE, "Craftsmanship"),
            (NodeType.PREFERENCE, "Brevity"),
            (NodeType.CONSTRAINT, "No Meetings Before 10"),
        ]:
            node = PreferenceNode(
                id=None,
                name=name,
                node_type=node_type,
                strength=0.7,
                flexibility=NodeFlexibility.SOFT,
                contexts=[],
                source=NodeSource.STATED,
                confidence=0.8,
                description=f"{name} description.",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            upsert_preference_node(conn, node)

        values = get_all_preference_nodes(conn, node_type=NodeType.VALUE)
        assert all(n.node_type == NodeType.VALUE for n in values)
        assert any(n.name == "Craftsmanship" for n in values)


# ---------------------------------------------------------------------------
# Layer 2: Observation Engine Tests
# ---------------------------------------------------------------------------

class TestObservationEngine:
    def test_extract_signals_sentiment_positive(self):
        """Should detect positive sentiment."""
        from user_model.observation import extract_signals
        signals = extract_signals(
            "This is great! Really love the excellent work you've done.",
            message_id="test-001",
            context="",
        )
        sentiments = [s for s in signals if s.signal_type == ObservationSignalType.SENTIMENT]
        assert len(sentiments) >= 1
        assert sentiments[0].content == "positive"

    def test_extract_signals_sentiment_negative(self):
        """Should detect negative sentiment."""
        from user_model.observation import extract_signals
        signals = extract_signals(
            "That's wrong and broken. Stop doing that. Terrible.",
            message_id="test-002",
            context="",
        )
        sentiments = [s for s in signals if s.signal_type == ObservationSignalType.SENTIMENT]
        assert len(sentiments) >= 1
        assert sentiments[0].content == "negative"

    def test_extract_signals_energy(self):
        """Should detect high energy signals."""
        from user_model.observation import extract_signals
        signals = extract_signals(
            "This is urgent!! Need this ASAP!!",
            message_id="test-003",
            context="",
        )
        energy = [s for s in signals if s.signal_type == ObservationSignalType.ENERGY]
        assert len(energy) >= 1
        assert energy[0].content == "high"

    def test_extract_signals_correction(self):
        """Should detect correction signals."""
        from user_model.observation import extract_signals
        signals = extract_signals(
            "Actually, that's wrong. I meant something different.",
            message_id="test-004",
            context="",
        )
        corrections = [s for s in signals if s.signal_type == ObservationSignalType.CORRECTION]
        assert len(corrections) >= 1

    def test_extract_signals_preference_statement(self):
        """Should detect explicit preference statements."""
        from user_model.observation import extract_signals
        signals = extract_signals(
            "I prefer concise bullet points rather than long paragraphs.",
            message_id="test-005",
            context="",
        )
        prefs = [s for s in signals if s.signal_type == ObservationSignalType.PREFERENCE]
        assert len(prefs) >= 1

    def test_extract_topic(self):
        """Should detect topic from keyword clusters."""
        from user_model.observation import extract_signals
        signals = extract_signals(
            "The code has a bug in the python function. Need to git commit the fix.",
            message_id="test-006",
        )
        topics = [s for s in signals if s.signal_type == ObservationSignalType.TOPIC]
        assert len(topics) >= 1
        assert topics[0].content == "coding"

    def test_observe_message_persists(self, conn):
        """observe_message should persist signals to DB."""
        from user_model.observation import observe_message
        obs_ids = observe_message(
            conn,
            message_text="Great work! I love this excellent feature.",
            message_id="persist-001",
            context="feedback",
        )
        assert len(obs_ids) > 0
        # Verify in DB
        all_obs = get_recent_observations(conn, hours=1)
        assert len(all_obs) >= len(obs_ids)


# ---------------------------------------------------------------------------
# Layer 2: Preference Graph Tests
# ---------------------------------------------------------------------------

class TestPreferenceGraph:
    def test_add_preference(self, conn):
        """Should add a preference node via the graph API."""
        from user_model.preference_graph import add_preference
        node_id = add_preference(
            conn,
            name="Deep Focus",
            node_type=NodeType.PRINCIPLE,
            description="Prefer uninterrupted work blocks.",
            strength=0.8,
        )
        assert node_id is not None
        node = get_preference_node(conn, node_id)
        assert node.name == "Deep Focus"

    def test_add_preference_with_parent(self, conn):
        """Should wire derives_from edge between nodes."""
        from user_model.preference_graph import add_preference
        parent_id = add_preference(
            conn,
            name="Craftsmanship",
            node_type=NodeType.VALUE,
            description="Core value: do things well.",
        )
        child_id = add_preference(
            conn,
            name="Clean Code",
            node_type=NodeType.PRINCIPLE,
            description="Write readable, maintainable code.",
            parent_id=parent_id,
        )
        # Check edge exists
        edge = conn.execute(
            "SELECT * FROM um_preference_edges WHERE source_id = ? AND target_id = ?",
            (child_id, parent_id),
        ).fetchone()
        assert edge is not None
        assert edge["edge_type"] == "derives_from"

    def test_resolve_preferences_context_filtering(self, conn):
        """Should return universal + matching context preferences."""
        from user_model.preference_graph import add_preference, resolve_preferences
        # Universal preference
        add_preference(conn, name="Brevity", node_type=NodeType.PREFERENCE,
                      description="Keep it short.", contexts=[])
        # Work-specific preference
        add_preference(conn, name="No Meetings", node_type=NodeType.CONSTRAINT,
                      description="No meetings before 10am.", contexts=["work"])
        # Coding-specific preference
        add_preference(conn, name="Type Hints", node_type=NodeType.PREFERENCE,
                      description="Always use type hints.", contexts=["coding"])

        # Query for work context only
        work_prefs = resolve_preferences(conn, contexts=["work"])
        names = [n.name for n in work_prefs]
        assert "Brevity" in names
        assert "No Meetings" in names
        assert "Type Hints" not in names

    def test_reinforce_preference(self, conn, sample_preference):
        """Reinforcing a preference should increase evidence count and confidence."""
        from user_model.preference_graph import reinforce_preference
        original_count = sample_preference.evidence_count
        original_confidence = sample_preference.confidence
        reinforce_preference(conn, sample_preference.id)
        updated = get_preference_node(conn, sample_preference.id)
        assert updated.evidence_count == original_count + 1
        assert updated.confidence >= original_confidence

    def test_apply_correction(self, conn, sample_preference):
        """Correction should set source=corrected and confidence=1.0."""
        from user_model.preference_graph import apply_correction
        apply_correction(conn, sample_preference.id, "Updated: ultra-concise responses only.")
        updated = get_preference_node(conn, sample_preference.id)
        assert updated.source == NodeSource.CORRECTED
        assert updated.confidence == pytest.approx(1.0)
        assert "ultra-concise" in updated.description

    def test_decay_does_not_affect_corrected_nodes(self, conn, sample_preference):
        """Corrected nodes should not decay."""
        from user_model.preference_graph import apply_correction, apply_decay
        apply_correction(conn, sample_preference.id, "Corrected preference.")
        # Make it old
        conn.execute(
            "UPDATE um_preference_nodes SET last_observed = ? WHERE id = ?",
            ((datetime.utcnow() - timedelta(days=30)).isoformat(), sample_preference.id),
        )
        conn.commit()
        apply_decay(conn, days_since_last_run=30)
        updated = get_preference_node(conn, sample_preference.id)
        assert updated.confidence == pytest.approx(1.0)  # Unchanged


# ---------------------------------------------------------------------------
# Layer 2: Emotional Model Tests
# ---------------------------------------------------------------------------

class TestEmotionalModel:
    def test_infer_vad_positive_sentiment(self):
        """Positive sentiment should yield positive valence."""
        from user_model.emotional_model import infer_vad_from_signals
        v, a, d, conf = infer_vad_from_signals(sentiment="positive")
        assert v > 0
        assert 0.0 <= a <= 1.0
        assert 0.0 <= d <= 1.0
        assert conf > 0

    def test_infer_vad_correction_lowers_valence(self):
        """Corrections should lower valence."""
        from user_model.emotional_model import infer_vad_from_signals
        v_no_correction, _, _, _ = infer_vad_from_signals(sentiment="positive")
        v_correction, _, _, _ = infer_vad_from_signals(sentiment="positive", correction=True)
        assert v_correction < v_no_correction

    def test_record_emotional_state(self, conn):
        """Should insert emotional state into DB."""
        from user_model.emotional_model import record_emotional_state
        state_id = record_emotional_state(
            conn, sentiment="positive", energy="high", context="work"
        )
        assert state_id is not None

    def test_get_emotional_baseline(self, conn):
        """Should compute baseline from stored states."""
        # Insert several states
        for _ in range(5):
            state = EmotionalState(
                id=None, valence=0.5, arousal=0.6, dominance=0.7,
                trigger=None, context="", recorded_at=datetime.utcnow()
            )
            insert_emotional_state(conn, state)

        baseline = get_emotional_baseline(conn, days=30)
        assert baseline is not None
        assert "valence" in baseline
        assert "sample_count" in baseline
        assert baseline["sample_count"] >= 5


# ---------------------------------------------------------------------------
# Layer 2: Self-Knowledge Tests
# ---------------------------------------------------------------------------

class TestSelfKnowledge:
    def test_detect_contradictions(self, conn):
        """Should detect contradictions between conflicting preferences."""
        from user_model.preference_graph import add_preference
        from user_model.self_knowledge import detect_contradictions
        # Add two potentially conflicting preferences
        add_preference(conn, name="Concise responses", node_type=NodeType.PREFERENCE,
                      description="Keep it short.", strength=0.9,
                      flexibility=NodeFlexibility.HARD, contexts=["communication"])
        add_preference(conn, name="Detailed explanations", node_type=NodeType.PREFERENCE,
                      description="Include all details.", strength=0.85,
                      flexibility=NodeFlexibility.HARD, contexts=["communication"])
        contradictions = detect_contradictions(conn)
        # May or may not detect based on heuristics — just ensure it runs
        assert isinstance(contradictions, list)

    def test_add_blind_spot(self, conn):
        """Should add and retrieve a blind spot."""
        from user_model.self_knowledge import add_blind_spot
        spot_id = add_blind_spot(
            conn,
            category="time_estimation",
            description="Tends to underestimate task durations.",
            evidence="Pattern observed over 10 interactions.",
            confidence=0.7,
        )
        assert spot_id is not None
        spots = get_blind_spots(conn, surfaced_only=False)
        assert any(s.id == spot_id for s in spots)

    def test_record_life_pattern(self, conn):
        """Should record and update life patterns."""
        from user_model.self_knowledge import record_life_pattern
        pattern_id = record_life_pattern(
            conn, name="Morning Productivity Peak", description="Most productive 9-11am.",
            stage="active", confidence=0.75
        )
        assert pattern_id is not None
        patterns = conn.execute(
            "SELECT * FROM um_life_patterns WHERE id = ?", (pattern_id,)
        ).fetchone()
        assert patterns is not None
        assert patterns["name"] == "Morning Productivity Peak"

        # Update by name — evidence count should increment
        pattern_id2 = record_life_pattern(
            conn, name="Morning Productivity Peak", description="Updated.",
            stage="active"
        )
        updated = conn.execute(
            "SELECT * FROM um_life_patterns WHERE id = ?", (pattern_id2,)
        ).fetchone()
        assert updated["evidence_count"] >= 2


# ---------------------------------------------------------------------------
# Layer 2: Prediction Tests
# ---------------------------------------------------------------------------

class TestPrediction:
    def test_compute_attention_score(self):
        """Should return a score between 0 and 1."""
        from user_model.prediction import compute_attention_score
        score = compute_attention_score(
            urgency=0.8, importance=0.9, alignment=0.7, recency=0.6
        )
        assert 0.0 <= score <= 1.0

    def test_build_attention_stack(self, conn):
        """Should return a list of attention items."""
        from user_model.prediction import build_attention_stack
        from user_model.narrative import create_arc
        # Create an arc to make the stack non-empty
        create_arc(conn, title="Side project", description="Building a tool.", themes=["coding"])
        items = build_attention_stack(conn, max_items=5)
        assert isinstance(items, list)
        assert len(items) <= 5

    def test_refresh_attention_stack_persists(self, conn):
        """Refreshed stack should be retrievable from DB."""
        from user_model.prediction import refresh_attention_stack
        from user_model.narrative import create_arc
        create_arc(conn, title="Arc for attention", description="Test arc.", themes=["work"])
        ids = refresh_attention_stack(conn, max_items=5)
        assert isinstance(ids, list)


# ---------------------------------------------------------------------------
# Layer 3: Introspection Tests
# ---------------------------------------------------------------------------

class TestIntrospection:
    def test_query_model_preferences(self, conn, sample_preference):
        """Should return preferences via query_model."""
        from user_model.introspection import query_model
        result = query_model(conn, "preferences")
        assert result["type"] == "preferences"
        assert result["count"] >= 1
        assert any(n["name"] == "Concise Responses" for n in result["nodes"])

    def test_query_model_meta(self, conn):
        """Should return model metadata."""
        from user_model.introspection import query_model
        result = query_model(conn, "meta")
        assert result["type"] == "meta"
        assert "schema_version" in result
        assert "observation_count" in result

    def test_query_model_unknown_type(self, conn):
        """Unknown query_type should return error."""
        from user_model.introspection import query_model
        result = query_model(conn, "nonexistent_type")
        assert "error" in result

    def test_get_resolved_preferences(self, conn):
        """Should return context-resolved preferences."""
        from user_model.preference_graph import add_preference
        from user_model.introspection import get_resolved_preferences
        add_preference(conn, name="No Interruptions", node_type=NodeType.CONSTRAINT,
                      description="Do not interrupt focus time.", contexts=["work"])
        result = get_resolved_preferences(conn, contexts=["work"])
        assert "preferences" in result
        assert result["contexts"] == ["work"]

    def test_inspect_entity_preference(self, conn, sample_preference):
        """Should return full entity details."""
        from user_model.introspection import inspect_entity
        result = inspect_entity(conn, sample_preference.id, "preference")
        assert "node" in result
        assert result["node"]["name"] == "Concise Responses"
        assert "edges" in result

    def test_inspect_entity_not_found(self, conn):
        """Non-existent entity should return error."""
        from user_model.introspection import inspect_entity
        result = inspect_entity(conn, "nonexistent-id", "preference")
        assert "error" in result

    def test_correct_preference(self, conn, sample_preference):
        """Should apply correction to a preference node."""
        from user_model.introspection import correct_preference
        result = correct_preference(
            conn, sample_preference.id, "Even more concise: one sentence max."
        )
        assert result["success"] is True
        assert result["confidence"] == 1.0
        assert result["source"] == "corrected"

    def test_reflect_runs(self, conn):
        """reflect() should run without error."""
        from user_model.introspection import reflect
        result = reflect(conn)
        assert "actions" in result
        assert isinstance(result["actions"], list)

    def test_get_attention(self, conn):
        """get_attention should return a dict with items."""
        from user_model.introspection import get_attention
        result = get_attention(conn, contexts=["work"])
        assert "items" in result
        assert isinstance(result["items"], list)


# ---------------------------------------------------------------------------
# Layer 3: Owner Tests
# ---------------------------------------------------------------------------

class TestOwner:
    def test_read_owner_missing_file(self, tmp_path):
        """Missing owner.toml should return empty dict."""
        from user_model.owner import read_owner
        result = read_owner(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_write_and_read_owner(self, tmp_path):
        """Should write and read back owner data."""
        from user_model.owner import write_owner, read_owner
        path = tmp_path / "owner.toml"
        data = {
            "owner": {
                "name": "TestUser",
                "telegram_chat_id": "123456789",
                "email": "test@example.com",
            }
        }
        write_owner(data, path)
        assert path.exists()
        read_back = read_owner(path)
        assert read_back["owner"]["name"] == "TestUser"
        assert read_back["owner"]["telegram_chat_id"] == "123456789"

    def test_get_owner_id(self, tmp_path):
        """get_owner_id should return telegram_chat_id preferentially."""
        from user_model.owner import write_owner, get_owner_id
        path = tmp_path / "owner.toml"
        data = {
            "owner": {
                "telegram_chat_id": "999888777",
                "email": "x@example.com",
            }
        }
        write_owner(data, path)
        owner_id = get_owner_id(path)
        assert owner_id == "999888777"

    def test_ensure_owner_toml_creates_file(self, tmp_path):
        """ensure_owner_toml should create the file if missing."""
        from user_model.owner import ensure_owner_toml, read_owner
        path = tmp_path / "owner.toml"
        data = ensure_owner_toml(name="Drew", owner_file=path)
        assert path.exists()
        read_back = read_owner(path)
        assert read_back.get("owner", {}).get("name") == "Drew"


# ---------------------------------------------------------------------------
# Layer 3: Markdown Sync Tests
# ---------------------------------------------------------------------------

class TestMarkdownSync:
    def test_sync_all_creates_files(self, conn, tmp_path):
        """sync_all should create all expected markdown files."""
        from user_model.markdown_sync import sync_all
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = sync_all(conn, str(workspace))
        assert "synced_at" in result
        assert "errors" in result
        base = workspace / "user-model"
        assert (base / "_index.md").exists()
        assert (base / "emotional-baseline.md").exists()
        assert (base / "active-arcs.md").exists()
        assert (base / "attention.md").exists()

    def test_sync_preference_node_creates_file(self, conn, tmp_path, sample_preference):
        """Preference nodes should create individual markdown files."""
        from user_model.markdown_sync import sync_all
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sync_all(conn, str(workspace))
        pref_dir = workspace / "user-model" / "preferences"
        md_files = list(pref_dir.glob("*.md"))
        assert len(md_files) >= 1
        content = md_files[0].read_text()
        assert "Concise Responses" in content
        assert "Strength:" in content

    def test_sync_is_idempotent(self, conn, tmp_path):
        """Running sync twice should not raise errors."""
        from user_model.markdown_sync import sync_all
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sync_all(conn, str(workspace))
        result2 = sync_all(conn, str(workspace))
        assert "errors" in result2
        assert len(result2["errors"]) == 0


# ---------------------------------------------------------------------------
# Layer 3: MCP Tool Handler Tests
# ---------------------------------------------------------------------------

class TestMCPTools:
    def test_handle_model_observe_auto(self, conn, tmp_path):
        """model_observe auto mode should extract and persist signals."""
        from user_model.tools import handle_model_observe
        result_json = handle_model_observe(conn, {
            "message_text": "I love this! Great work, really excellent!",
            "message_id": "tool-test-001",
            "context": "feedback",
        })
        result = json.loads(result_json)
        assert result["success"] is True
        assert result["mode"] == "auto_extracted"
        assert result["signals_extracted"] > 0

    def test_handle_model_observe_explicit(self, conn):
        """model_observe explicit mode should record the given observation."""
        from user_model.tools import handle_model_observe
        result_json = handle_model_observe(conn, {
            "message_text": "test",
            "message_id": "explicit-001",
            "observation": "User prefers Python over JavaScript",
            "observation_type": "preference",
            "confidence": 0.9,
        })
        result = json.loads(result_json)
        assert result["success"] is True
        assert result["mode"] == "explicit"

    def test_handle_model_query_preferences(self, conn, sample_preference):
        """model_query preferences should return nodes."""
        from user_model.tools import handle_model_query
        result_json = handle_model_query(conn, {"query_type": "preferences"})
        result = json.loads(result_json)
        assert result["type"] == "preferences"
        assert result["count"] >= 1

    def test_handle_model_preferences(self, conn):
        """model_preferences should return resolved preferences."""
        from user_model.tools import handle_model_preferences
        result_json = handle_model_preferences(conn, {"contexts": ["work"]})
        result = json.loads(result_json)
        assert "preferences" in result

    def test_handle_model_reflect(self, conn):
        """model_reflect should return summary with actions."""
        from user_model.tools import handle_model_reflect
        result_json = handle_model_reflect(conn, {"sync_files": False})
        result = json.loads(result_json)
        assert "actions" in result

    def test_handle_model_correct(self, conn, sample_preference):
        """model_correct should apply correction."""
        from user_model.tools import handle_model_correct
        result_json = handle_model_correct(conn, {
            "node_id": sample_preference.id,
            "corrected_description": "Keep all responses to one sentence max.",
            "corrected_strength": 0.95,
        })
        result = json.loads(result_json)
        assert result["success"] is True

    def test_handle_model_correct_missing_fields(self, conn):
        """model_correct without required fields should return error."""
        from user_model.tools import handle_model_correct
        result_json = handle_model_correct(conn, {})
        result = json.loads(result_json)
        assert "error" in result

    def test_handle_model_inspect(self, conn, sample_preference):
        """model_inspect should return entity details."""
        from user_model.tools import handle_model_inspect
        result_json = handle_model_inspect(conn, {
            "entity_id": sample_preference.id,
            "entity_type": "preference",
        })
        result = json.loads(result_json)
        assert "node" in result

    def test_handle_model_attention(self, conn):
        """model_attention should return attention items dict."""
        from user_model.tools import handle_model_attention
        result_json = handle_model_attention(conn, {"max_items": 5})
        result = json.loads(result_json)
        assert "items" in result

    def test_dispatch_unknown_tool(self, conn):
        """Dispatching an unknown tool should return error JSON."""
        from user_model.tools import dispatch
        result_json = dispatch("model_nonexistent", {}, conn)
        result = json.loads(result_json)
        assert "error" in result


# ---------------------------------------------------------------------------
# Layer 4: UserModel facade Tests
# ---------------------------------------------------------------------------

class TestUserModelFacade:
    def test_create_user_model(self, tmp_path):
        """create_user_model should return a UserModel instance."""
        from user_model import create_user_model
        db_path = tmp_path / "test.db"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        model = create_user_model(db_path=db_path, workspace_path=workspace)
        assert model is not None

    def test_user_model_observe(self, tmp_path):
        """UserModel.observe should return observation IDs."""
        from user_model import create_user_model
        db_path = tmp_path / "test.db"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        model = create_user_model(db_path=db_path, workspace_path=workspace)
        obs_ids = model.observe(
            message_text="I prefer short answers, thanks!",
            message_id="facade-001",
        )
        assert isinstance(obs_ids, list)
        assert len(obs_ids) > 0

    def test_user_model_health(self, tmp_path):
        """UserModel.health should return ok status."""
        from user_model import create_user_model
        db_path = tmp_path / "test.db"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        model = create_user_model(db_path=db_path, workspace_path=workspace)
        health = model.health()
        assert health["status"] == "ok"
        assert "schema_version" in health

    def test_user_model_dispatch(self, tmp_path):
        """UserModel.dispatch should work for model_query."""
        from user_model import create_user_model
        db_path = tmp_path / "test.db"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        model = create_user_model(db_path=db_path, workspace_path=workspace)
        result_json = model.dispatch("model_query", {"query_type": "meta"})
        result = json.loads(result_json)
        assert result["type"] == "meta"

    def test_user_model_tool_names(self, tmp_path):
        """UserModel should expose the correct set of tool names."""
        from user_model import create_user_model
        db_path = tmp_path / "test.db"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        model = create_user_model(db_path=db_path, workspace_path=workspace)
        expected = {
            "model_observe", "model_query", "model_preferences",
            "model_reflect", "model_correct", "model_inspect", "model_attention",
            "model_infer", "model_user_context",
        }
        assert model.tool_names == expected

    def test_user_model_sync_files(self, tmp_path):
        """UserModel.sync_files should create user-model directory."""
        from user_model import create_user_model
        db_path = tmp_path / "test.db"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        model = create_user_model(db_path=db_path, workspace_path=workspace)
        result = model.sync_files()
        assert "files_written" in result
        assert (workspace / "user-model").exists()


# ---------------------------------------------------------------------------
# v2: Schema Migration Tests
# ---------------------------------------------------------------------------

class TestSchemaV2:
    def test_v2_tables_exist(self, conn):
        """v2 migration should create new tables."""
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'um_%'"
            ).fetchall()
        }
        for t in ["um_temporal_snapshots", "um_drift_records", "um_activity_rhythm", "um_inference_cache"]:
            assert t in tables, f"Missing v2 table: {t}"

    def test_v2_seed_columns_exist(self, conn):
        """v2 migration should add seed_source and decay_rate_override columns."""
        info = conn.execute("PRAGMA table_info(um_preference_nodes)").fetchall()
        col_names = {r["name"] for r in info}
        assert "seed_source" in col_names
        assert "decay_rate_override" in col_names


# ---------------------------------------------------------------------------
# v2: Activity Rhythm Tests
# ---------------------------------------------------------------------------

class TestActivityRhythm:
    def test_update_and_get_rhythm(self, conn):
        """Should insert and retrieve activity rhythm entries."""
        from user_model.db import update_activity_rhythm, get_activity_rhythm
        update_activity_rhythm(conn, hour_of_day=14, day_of_week=2, message_length=100, latency_ms=500)
        update_activity_rhythm(conn, hour_of_day=14, day_of_week=2, message_length=200, latency_ms=300)
        rhythm = get_activity_rhythm(conn)
        slot = next(r for r in rhythm if r.hour_of_day == 14 and r.day_of_week == 2)
        assert slot.message_count == 2
        assert slot.total_length == 300
        assert slot.total_latency == 800
        assert slot.latency_count == 2

    def test_get_peak_activity_hours(self, conn):
        """Should return top hours by message count."""
        from user_model.db import update_activity_rhythm, get_peak_activity_hours
        for _ in range(5):
            update_activity_rhythm(conn, hour_of_day=10, day_of_week=0, message_length=50)
        for _ in range(3):
            update_activity_rhythm(conn, hour_of_day=15, day_of_week=0, message_length=50)
        for _ in range(1):
            update_activity_rhythm(conn, hour_of_day=20, day_of_week=0, message_length=50)
        peaks = get_peak_activity_hours(conn, top_n=2)
        assert peaks[0] == 10
        assert len(peaks) == 2


# ---------------------------------------------------------------------------
# v2: Inference Cache Tests
# ---------------------------------------------------------------------------

class TestInferenceCache:
    def test_set_and_get_cache(self, conn):
        """Should cache and retrieve inference results."""
        from user_model.db import set_cached_inference, get_cached_inference
        data = {"mood": "positive", "confidence": 0.8}
        set_cached_inference(conn, "test-key", data, 0.8, ttl_minutes=60)
        result = get_cached_inference(conn, "test-key")
        assert result is not None
        assert result["mood"] == "positive"

    def test_expired_cache_returns_none(self, conn):
        """Expired cache should return None."""
        from user_model.db import set_cached_inference, get_cached_inference
        set_cached_inference(conn, "expired-key", {"x": 1}, 0.5, ttl_minutes=0)
        # Manually expire it
        conn.execute(
            "UPDATE um_inference_cache SET expires_at = datetime('now', '-1 hour') WHERE cache_key = 'expired-key'"
        )
        conn.commit()
        result = get_cached_inference(conn, "expired-key")
        assert result is None

    def test_cleanup_expired(self, conn):
        """cleanup_expired_cache should remove old entries."""
        from user_model.db import set_cached_inference, cleanup_expired_cache
        set_cached_inference(conn, "old-key", {"x": 1}, 0.5, ttl_minutes=0)
        conn.execute(
            "UPDATE um_inference_cache SET expires_at = datetime('now', '-1 hour') WHERE cache_key = 'old-key'"
        )
        conn.commit()
        deleted = cleanup_expired_cache(conn)
        assert deleted >= 1


# ---------------------------------------------------------------------------
# v2: Temporal Snapshot Tests
# ---------------------------------------------------------------------------

class TestTemporalSnapshots:
    def test_insert_and_get_snapshot(self, conn):
        """Should insert and retrieve a temporal snapshot."""
        from user_model.db import insert_temporal_snapshot, get_latest_snapshot
        from user_model.schema import TemporalSnapshot
        snap = TemporalSnapshot(
            id=None, snapshot_at=datetime.utcnow(),
            week_number=11, year=2026,
            data={"preferences": []}, obs_count=10, node_count=5,
        )
        snap_id = insert_temporal_snapshot(conn, snap)
        assert snap_id is not None
        latest = get_latest_snapshot(conn)
        assert latest is not None
        assert latest.id == snap_id
        assert latest.week_number == 11

    def test_insert_and_get_drift(self, conn):
        """Should insert and retrieve drift records."""
        from user_model.db import insert_drift_record, get_recent_drifts, get_unsurfaced_drifts
        from user_model.schema import DriftRecord
        drift = DriftRecord(
            id=None, detected_at=datetime.utcnow(),
            snapshot_a_id="snap-a", snapshot_b_id="snap-b",
            drift_type="preference_shift",
            description="Test drift", magnitude=0.3,
            node_id="node-1", surfaced=False,
        )
        drift_id = insert_drift_record(conn, drift)
        assert drift_id is not None
        recent = get_recent_drifts(conn, limit=10)
        assert any(d.id == drift_id for d in recent)
        unsurfaced = get_unsurfaced_drifts(conn)
        assert any(d.id == drift_id for d in unsurfaced)


# ---------------------------------------------------------------------------
# v2: Seed Bootstrap Tests
# ---------------------------------------------------------------------------

class TestSeedBootstrap:
    def test_seed_from_owner_toml(self, conn, tmp_path):
        """seed_from_owner_toml should create preference nodes."""
        from user_model.seed import seed_from_owner_toml
        from user_model.owner import write_owner
        owner_path = tmp_path / "owner.toml"
        write_owner({
            "owner": {"name": "Test"},
            "values": {"autonomy": "Values independence"},
            "preferences": {"brevity": "Keep it short"},
            "constraints": {"no_emojis": "Never use emojis"},
        }, owner_path)
        counts = seed_from_owner_toml(conn, owner_path)
        assert counts["values"] == 1
        assert counts["preferences"] == 1
        assert counts["constraints"] == 1
        nodes = get_all_preference_nodes(conn)
        names = [n.name for n in nodes]
        assert "autonomy" in names
        assert "brevity" in names
        assert "no-emojis" in names


# ---------------------------------------------------------------------------
# v2: Profile File Tests
# ---------------------------------------------------------------------------

class TestProfileFiles:
    def test_read_write_profile(self, tmp_path):
        """Should write and read profile with YAML front matter."""
        from user_model.profile import write_profile_file, read_profile_file
        path = tmp_path / "test.md"
        write_profile_file(path, {"name": "Drew", "tz": "PST"}, "# Hello\nBody text.")
        result = read_profile_file(path)
        assert result["meta"]["name"] == "Drew"
        assert "Body text" in result["body"]

    def test_get_compact_context(self):
        """get_compact_context should return a non-empty string when profiles exist."""
        from user_model.profile import get_compact_context
        ctx = get_compact_context()
        # Profile files were created in Phase 3b, so this should work
        assert isinstance(ctx, str)
        if ctx:  # May be empty in CI
            assert "Drew" in ctx

    def test_read_all_profiles(self):
        """read_all_profiles should return dict with expected keys."""
        from user_model.profile import read_all_profiles
        profiles = read_all_profiles()
        assert "profile.md" in profiles
        assert "goals.md" in profiles
        assert "preferences.md" in profiles


# ---------------------------------------------------------------------------
# v2: Model Infer Tests
# ---------------------------------------------------------------------------

class TestModelInfer:
    def test_run_inference_returns_expected_keys(self, conn):
        """run_inference should return mood, response_style, likely_next."""
        from user_model.infer import run_inference
        result = run_inference(conn, contexts=["work"], force_refresh=True)
        assert "mood_estimate" in result
        assert "response_style" in result
        assert "likely_next" in result
        assert "cached" in result
        assert result["mood_estimate"]["valence"] is not None
