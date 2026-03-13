"""
Dataclass definitions for the User Model subsystem.

Zero external dependencies — only Python stdlib.
These are the shared data types used across all user_model modules.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class NodeType(str, Enum):
    """Types of nodes in the preference graph."""
    VALUE = "value"           # Core values (e.g., "craftsmanship", "autonomy")
    PRINCIPLE = "principle"   # Derived principles (e.g., "deep-focus-over-multitasking")
    PREFERENCE = "preference" # Specific preferences (e.g., "concise-responses")
    CONSTRAINT = "constraint" # Hard constraints (e.g., "no-meetings-before-10")


class NodeFlexibility(str, Enum):
    """How rigid a preference node is."""
    HARD = "hard"   # Non-negotiable constraint
    SOFT = "soft"   # Preferred but can be overridden with context
    FLEXIBLE = "flexible"  # Default behavior, easily changed


class NodeSource(str, Enum):
    """How a preference node was established."""
    STATED = "stated"       # User explicitly said this
    INFERRED = "inferred"   # Derived from behavioral patterns
    CORRECTED = "corrected" # User corrected a prior inference


class ObservationSignalType(str, Enum):
    """Types of signals extractable from observations."""
    TIMING = "timing"       # When messages arrive, response latency
    SENTIMENT = "sentiment" # Positive/negative/neutral tone
    TOPIC = "topic"         # Subject matter
    ENERGY = "energy"       # Urgency, engagement level
    PREFERENCE = "preference"  # Explicit or implicit preference statement
    CORRECTION = "correction"  # User correcting Lobster
    EMOTION = "emotion"     # Emotional state indicator


class AttentionCategory(str, Enum):
    """Categories for attention scoring."""
    URGENT = "urgent"       # Time-sensitive items
    IMPORTANT = "important" # High-value but not urgent
    MONITORING = "monitoring"  # Things to keep an eye on
    DEFERRED = "deferred"   # Acknowledged but not now


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class PreferenceNode:
    """A node in the preference graph."""
    id: str
    name: str
    node_type: NodeType
    strength: float          # 0.0–1.0, how strong this preference is
    flexibility: NodeFlexibility
    contexts: list[str]      # Contexts where this applies (empty = universal)
    source: NodeSource
    confidence: float        # 0.0–1.0, model confidence
    description: str         # Human-readable description
    evidence_count: int = 0
    last_observed: datetime | None = None
    parent_ids: list[str] = field(default_factory=list)   # Parent nodes (derives from)
    override_ids: list[str] = field(default_factory=list) # Nodes this overrides
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    decay_rate: float = 0.01  # Per-day decay when not reinforced


@dataclass
class Observation:
    """A raw observation extracted from a user message."""
    id: str | None          # Set after DB insert
    message_id: str         # Source message ID
    signal_type: ObservationSignalType
    content: str            # The observed signal content
    confidence: float       # 0.0–1.0
    context: str            # Active context(s) at observation time
    metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=datetime.utcnow)
    processed: bool = False  # Has inference pipeline consumed this?


@dataclass
class EmotionalState:
    """VAD (Valence-Arousal-Dominance) emotional state snapshot."""
    id: str | None
    valence: float           # -1.0 (negative) to +1.0 (positive)
    arousal: float           # 0.0 (calm) to 1.0 (excited/agitated)
    dominance: float         # 0.0 (submissive/uncertain) to 1.0 (confident/in-control)
    trigger: str | None      # What triggered this state (optional)
    context: str             # Active context
    recorded_at: datetime = field(default_factory=datetime.utcnow)
    confidence: float = 0.7


@dataclass
class BlindSpot:
    """A identified blind spot in the user's self-knowledge."""
    id: str | None
    category: str           # Type of blind spot
    description: str        # What the blind spot is
    evidence: str           # Supporting evidence
    surfaced: bool = False  # Has this been shown to the user?
    confidence: float = 0.6
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Contradiction:
    """A detected contradiction between two preference nodes or behaviors."""
    id: str | None
    node_id_a: str
    node_id_b: str
    description: str        # Human-readable explanation of the tension
    tension_score: float    # 0.0–1.0, how strong the contradiction is
    resolved: bool = False
    resolution: str | None = None
    detected_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class NarrativeArc:
    """A tracked narrative arc in the user's life."""
    id: str | None
    title: str
    description: str        # Summary of the arc
    themes: list[str]       # Associated themes/topics
    status: str             # "active", "resolved", "paused"
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_updated: datetime = field(default_factory=datetime.utcnow)
    resolution: str | None = None


@dataclass
class LifePattern:
    """A detected recurring pattern in the user's behavior."""
    id: str | None
    name: str
    description: str
    stage: str              # "forming", "active", "declining", "broken"
    evidence_count: int = 0
    confidence: float = 0.6
    first_seen: datetime = field(default_factory=datetime.utcnow)
    last_seen: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AttentionItem:
    """An item in the attention stack with scoring."""
    id: str | None
    title: str
    description: str
    category: AttentionCategory
    score: float             # Computed attention priority score
    context: str             # What context this is relevant in
    source: str              # Where this item came from
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None


@dataclass
class ModelMetadata:
    """Metadata about the user model itself."""
    schema_version: int
    owner_id: str | None    # Telegram chat_id or other identifier
    created_at: datetime
    last_observation_at: datetime | None
    last_consolidation_at: datetime | None
    observation_count: int
    preference_node_count: int


# ---------------------------------------------------------------------------
# v2 data structures (temporal modeling, drift detection, activity rhythm)
# ---------------------------------------------------------------------------

@dataclass
class TemporalSnapshot:
    """A weekly snapshot of the preference graph state for drift detection."""
    id: str | None
    snapshot_at: datetime
    week_number: int
    year: int
    data: dict[str, Any]       # Serialized graph state
    obs_count: int             # Total observations at snapshot time
    node_count: int            # Total preference nodes at snapshot time


@dataclass
class DriftRecord:
    """A detected change between two temporal snapshots."""
    id: str | None
    detected_at: datetime
    snapshot_a_id: str | None  # Earlier snapshot
    snapshot_b_id: str | None  # Later snapshot
    drift_type: str            # preference_shift, value_drift, pattern_change, emotional_drift
    description: str
    magnitude: float           # 0.0–1.0
    node_id: str | None = None # Related preference node (if applicable)
    surfaced: bool = False     # Has this been shown to the user?


@dataclass
class ActivityRhythmSlot:
    """Activity statistics for a specific hour/day-of-week slot."""
    hour_of_day: int           # 0–23
    day_of_week: int           # 0=Monday, 6=Sunday
    message_count: int
    total_length: int          # Cumulative message length
    total_latency: int         # Cumulative latency in ms
    latency_count: int         # Number of latency samples
