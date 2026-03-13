"""
Observation Engine: extract signals from user messages.

Tier 1 (Heuristic, <50ms): Pattern-based signal extraction, no ML needed.
Tier 2 (Embedding, <200ms): Semantic similarity — available when embedder is provided.
Tier 3 (LLM background): Deep inference — scheduled separately via inference.py.

Depends on: schema.py, db.py only.
"""

import re
import sqlite3
from datetime import datetime
from typing import Any

from .db import insert_observation, set_metadata_value
from .schema import Observation, ObservationSignalType


# ---------------------------------------------------------------------------
# Tier 1: Heuristic signal extraction
# ---------------------------------------------------------------------------

# Sentiment keyword sets (simple heuristic)
_POSITIVE_WORDS = frozenset([
    "great", "excellent", "love", "amazing", "perfect", "thanks", "appreciate",
    "helpful", "good", "nice", "happy", "excited", "yes", "exactly", "brilliant",
    "fantastic", "awesome", "wonderful", "glad",
])
_NEGATIVE_WORDS = frozenset([
    "bad", "wrong", "no", "don't", "not", "stop", "hate", "terrible", "awful",
    "frustrated", "annoying", "slow", "broken", "fail", "error", "confusing",
    "useless", "waste", "never", "please don't",
])
_HIGH_ENERGY_WORDS = frozenset([
    "urgent", "asap", "immediately", "now", "critical", "emergency", "quickly",
    "fast", "rush", "important", "deadline", "need", "must",
])
_CORRECTION_PATTERNS = [
    re.compile(r"\b(no,?\s+)?(actually|that'?s?\s+wrong|incorrect|not\s+right|you'?re?\s+wrong)\b", re.I),
    re.compile(r"\b(don'?t|please\s+don'?t|stop|never)\s+\w+\s+(that|this|it)\b", re.I),
    re.compile(r"\bi\s+(meant|mean|said|want)\b", re.I),
    re.compile(r"\bcorrect(ion)?\b", re.I),
]
_PREFERENCE_PATTERNS = [
    re.compile(r"\bi\s+(prefer|like|love|hate|dislike|want|need|always|never)\b", re.I),
    re.compile(r"\b(please|always|never|don'?t)\s+(use|give|send|write|include|skip)\b", re.I),
    re.compile(r"\bi'?m\s+(a\s+)?(morning|night|early|late)\s+(person|riser|owl)\b", re.I),
]


def extract_signals(
    message_text: str,
    message_id: str,
    context: str = "",
    metadata: dict[str, Any] | None = None,
) -> list[Observation]:
    """
    Tier 1 heuristic extraction. Returns a list of Observation objects.
    Fast, synchronous, <50ms for typical messages.
    """
    signals = []
    text = message_text.strip()
    words = set(re.findall(r"\b\w+\b", text.lower()))
    now = datetime.utcnow()
    meta = metadata or {}

    # --- Sentiment ---
    pos_count = len(words & _POSITIVE_WORDS)
    neg_count = len(words & _NEGATIVE_WORDS)
    if pos_count > neg_count and pos_count >= 2:
        signals.append(Observation(
            id=None,
            message_id=message_id,
            signal_type=ObservationSignalType.SENTIMENT,
            content="positive",
            confidence=min(0.5 + pos_count * 0.1, 0.9),
            context=context,
            metadata={**meta, "pos_count": pos_count, "neg_count": neg_count},
            observed_at=now,
        ))
    elif neg_count > pos_count and neg_count >= 2:
        signals.append(Observation(
            id=None,
            message_id=message_id,
            signal_type=ObservationSignalType.SENTIMENT,
            content="negative",
            confidence=min(0.5 + neg_count * 0.1, 0.9),
            context=context,
            metadata={**meta, "pos_count": pos_count, "neg_count": neg_count},
            observed_at=now,
        ))

    # --- Energy ---
    high_energy_words = words & _HIGH_ENERGY_WORDS
    if high_energy_words or text.endswith("!") or text.count("!") >= 2:
        signals.append(Observation(
            id=None,
            message_id=message_id,
            signal_type=ObservationSignalType.ENERGY,
            content="high",
            confidence=0.7,
            context=context,
            metadata={**meta, "energy_words": list(high_energy_words)},
            observed_at=now,
        ))

    # --- Correction ---
    for pattern in _CORRECTION_PATTERNS:
        if pattern.search(text):
            signals.append(Observation(
                id=None,
                message_id=message_id,
                signal_type=ObservationSignalType.CORRECTION,
                content=text[:200],  # Truncate for storage
                confidence=0.75,
                context=context,
                metadata=meta,
                observed_at=now,
            ))
            break  # One correction signal per message

    # --- Preference statement ---
    for pattern in _PREFERENCE_PATTERNS:
        if pattern.search(text):
            signals.append(Observation(
                id=None,
                message_id=message_id,
                signal_type=ObservationSignalType.PREFERENCE,
                content=text[:300],
                confidence=0.8,
                context=context,
                metadata=meta,
                observed_at=now,
            ))
            break

    # --- Topic extraction (simple keyword-based) ---
    topic = _detect_topic(text)
    if topic:
        signals.append(Observation(
            id=None,
            message_id=message_id,
            signal_type=ObservationSignalType.TOPIC,
            content=topic,
            confidence=0.6,
            context=context,
            metadata=meta,
            observed_at=now,
        ))

    return signals


def _detect_topic(text: str) -> str | None:
    """Very simple topic detection from keyword clusters."""
    text_lower = text.lower()
    topic_keywords = {
        "coding": ["code", "bug", "function", "python", "javascript", "api", "git", "deploy"],
        "health": ["sleep", "exercise", "gym", "run", "walk", "diet", "health", "tired", "energy"],
        "work": ["meeting", "deadline", "project", "task", "team", "manager", "client", "office"],
        "finance": ["money", "budget", "spend", "cost", "invest", "salary", "bill", "expense"],
        "learning": ["learn", "read", "book", "course", "study", "understand", "research"],
        "planning": ["plan", "schedule", "calendar", "agenda", "goal", "roadmap", "tomorrow"],
    }
    best_topic = None
    best_count = 0
    for topic, keywords in topic_keywords.items():
        count = sum(1 for kw in keywords if kw in text_lower)
        if count > best_count:
            best_count = count
            best_topic = topic
    return best_topic if best_count >= 2 else None


# ---------------------------------------------------------------------------
# Timing observation (message metadata)
# ---------------------------------------------------------------------------

def extract_timing_signal(
    message_id: str,
    message_ts: datetime,
    context: str = "",
) -> Observation:
    """Extract timing signal (time-of-day pattern)."""
    hour = message_ts.hour
    if 5 <= hour < 10:
        period = "early_morning"
    elif 10 <= hour < 12:
        period = "late_morning"
    elif 12 <= hour < 14:
        period = "midday"
    elif 14 <= hour < 18:
        period = "afternoon"
    elif 18 <= hour < 22:
        period = "evening"
    else:
        period = "night"

    return Observation(
        id=None,
        message_id=message_id,
        signal_type=ObservationSignalType.TIMING,
        content=period,
        confidence=1.0,  # Timing is deterministic
        context=context,
        metadata={"hour": hour, "weekday": message_ts.weekday()},
        observed_at=message_ts,
    )


# ---------------------------------------------------------------------------
# Observe entry point: extract and persist signals
# ---------------------------------------------------------------------------

def observe_message(
    conn: sqlite3.Connection,
    message_text: str,
    message_id: str,
    context: str = "",
    message_ts: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """
    Extract signals from a message and persist them to the DB.
    Returns list of inserted observation IDs.
    """
    ts = message_ts or datetime.utcnow()
    signals = extract_signals(message_text, message_id, context, metadata)

    # Add timing signal
    signals.append(extract_timing_signal(message_id, ts, context))

    # Persist
    obs_ids = []
    for obs in signals:
        obs_id = insert_observation(conn, obs)
        obs_ids.append(obs_id)

    # Update last observation timestamp
    set_metadata_value(conn, "last_observation_at", ts.isoformat())

    # Record activity rhythm (non-critical)
    try:
        from .rhythm import record_message_rhythm
        record_message_rhythm(conn, ts, len(message_text))
    except Exception:
        pass

    return obs_ids
