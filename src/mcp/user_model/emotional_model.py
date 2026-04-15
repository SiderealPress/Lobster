"""
Emotional Model: VAD (Valence-Arousal-Dominance) state tracking and pattern detection.

Tracks the user's emotional state over time using the VAD model:
- Valence: -1.0 (very negative) to +1.0 (very positive)
- Arousal: 0.0 (calm/low energy) to 1.0 (excited/high energy)
- Dominance: 0.0 (submissive/uncertain) to 1.0 (confident/in-control)

Depends on: schema.py, db.py only.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import (
    get_emotional_baseline,
    get_recent_emotional_states,
    insert_emotional_state,
)
from .schema import EmotionalState, ObservationSignalType


# ---------------------------------------------------------------------------
# Heuristic VAD extraction from text signals
# ---------------------------------------------------------------------------

# Sentiment → Valence mapping
_SENTIMENT_VALENCE = {
    "positive": 0.6,
    "negative": -0.5,
    "neutral": 0.0,
}

# Energy → Arousal mapping
_ENERGY_AROUSAL = {
    "high": 0.8,
    "medium": 0.5,
    "low": 0.2,
}

# Topic → rough Dominance adjustment
_TOPIC_DOMINANCE_ADJUST = {
    "coding": 0.1,    # Users often feel in-control when coding
    "planning": 0.15, # Planning = feeling in control
    "health": -0.05,  # Health discussions can feel uncertain
    "work": 0.0,
}


def infer_vad_from_signals(
    sentiment: str | None = None,
    energy: str | None = None,
    correction: bool = False,
    topic: str | None = None,
) -> tuple[float, float, float, float]:
    """
    Infer VAD values from observation signals.
    Returns (valence, arousal, dominance, confidence).
    """
    valence = _SENTIMENT_VALENCE.get(sentiment, 0.0) if sentiment else 0.0
    arousal = _ENERGY_AROUSAL.get(energy, 0.4) if energy else 0.4
    dominance = 0.5  # Baseline

    if correction:
        # Corrections often indicate frustration or uncertainty
        valence = min(valence - 0.2, -0.1)
        dominance = max(0.3, dominance - 0.1)

    if topic:
        dominance += _TOPIC_DOMINANCE_ADJUST.get(topic, 0.0)

    # Clamp to valid ranges
    valence = max(-1.0, min(1.0, valence))
    arousal = max(0.0, min(1.0, arousal))
    dominance = max(0.0, min(1.0, dominance))

    # Confidence is lower when we have few signals
    signal_count = sum(1 for x in [sentiment, energy] if x)
    confidence = 0.5 + signal_count * 0.15
    if correction:
        confidence += 0.1

    return valence, arousal, dominance, min(0.9, confidence)


def record_emotional_state(
    conn: sqlite3.Connection,
    sentiment: str | None = None,
    energy: str | None = None,
    correction: bool = False,
    topic: str | None = None,
    trigger: str | None = None,
    context: str = "",
) -> str:
    """
    Infer and record an emotional state from observation signals.
    Returns the inserted state ID.
    """
    valence, arousal, dominance, confidence = infer_vad_from_signals(
        sentiment, energy, correction, topic
    )
    state = EmotionalState(
        id=None,
        valence=valence,
        arousal=arousal,
        dominance=dominance,
        trigger=trigger,
        context=context,
        recorded_at=datetime.now(timezone.utc),
        confidence=confidence,
    )
    return insert_emotional_state(conn, state)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

def detect_emotional_patterns(
    conn: sqlite3.Connection, lookback_days: int = 30
) -> dict[str, Any]:
    """
    Detect patterns in emotional data over the lookback period.
    Returns a dict with pattern descriptions.
    """
    baseline = get_emotional_baseline(conn, lookback_days)
    recent = get_recent_emotional_states(conn, limit=20)

    patterns: dict[str, Any] = {}

    if not baseline:
        return {"message": "Insufficient data for pattern detection."}

    # Detect valence trend
    if len(recent) >= 5:
        recent_valence = [s.valence for s in recent[:5]]
        older_valence = [s.valence for s in recent[5:10]] if len(recent) > 5 else []
        if older_valence:
            recent_avg = sum(recent_valence) / len(recent_valence)
            older_avg = sum(older_valence) / len(older_valence)
            delta = recent_avg - older_avg
            if delta > 0.2:
                patterns["valence_trend"] = "improving"
            elif delta < -0.2:
                patterns["valence_trend"] = "declining"
            else:
                patterns["valence_trend"] = "stable"

    # Characterize baseline
    v = baseline["valence"]
    a = baseline["arousal"]
    d = baseline["dominance"]

    if v > 0.3:
        patterns["valence_state"] = "generally positive"
    elif v < -0.2:
        patterns["valence_state"] = "generally stressed"
    else:
        patterns["valence_state"] = "neutral/mixed"

    if a > 0.6:
        patterns["arousal_state"] = "high energy / engaged"
    elif a < 0.3:
        patterns["arousal_state"] = "low energy / calm"
    else:
        patterns["arousal_state"] = "moderate energy"

    if d > 0.6:
        patterns["dominance_state"] = "confident / in-control"
    elif d < 0.4:
        patterns["dominance_state"] = "uncertain / seeking guidance"
    else:
        patterns["dominance_state"] = "balanced"

    patterns["baseline"] = baseline
    return patterns


def format_emotional_baseline_markdown(conn: sqlite3.Connection) -> str:
    """
    Format the emotional baseline as markdown for the file layer.
    """
    baseline = get_emotional_baseline(conn, days=30)
    patterns = detect_emotional_patterns(conn)
    recent = get_recent_emotional_states(conn, limit=5)

    lines = ["# Emotional Baseline\n"]

    if not baseline:
        lines.append("*Insufficient data — no emotional states recorded yet.*")
        return "\n".join(lines)

    lines.append(f"- **Valence (30-day avg):** {baseline['valence']:+.2f} — {patterns.get('valence_state', 'unknown')}")
    lines.append(f"- **Arousal (30-day avg):** {baseline['arousal']:.2f} — {patterns.get('arousal_state', 'unknown')}")
    lines.append(f"- **Dominance (30-day avg):** {baseline['dominance']:.2f} — {patterns.get('dominance_state', 'unknown')}")
    lines.append(f"- **Sample count:** {baseline['sample_count']}")

    if "valence_trend" in patterns:
        lines.append(f"- **Recent trend:** {patterns['valence_trend']}")

    if recent:
        lines.append("\n## Recent States")
        for s in recent[:3]:
            trigger_str = f" (trigger: {s.trigger})" if s.trigger else ""
            lines.append(
                f"- {s.recorded_at.strftime('%Y-%m-%d %H:%M')} — "
                f"V:{s.valence:+.2f} A:{s.arousal:.2f} D:{s.dominance:.2f}{trigger_str}"
            )

    return "\n".join(lines)
