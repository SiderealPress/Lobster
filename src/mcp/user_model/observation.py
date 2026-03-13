"""
Observation Engine: extract signals from user messages.

Tier 1 (Heuristic, <50ms): Pattern-based signal extraction, no ML needed.
Tier 2 (Embedding, <200ms): Semantic similarity — available when embedder is provided.
Tier 3 (LLM background): Deep inference — scheduled separately via inference.py.

v2 additions: response latency, message length, follow-up detection, activity rhythm.

Depends on: schema.py, db.py only.
"""

import re
import sqlite3
from datetime import datetime, timedelta
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
    # v2 parameters
    latency_ms: int | None = None,
    previous_topic: str | None = None,
    previous_message_ts: datetime | None = None,
) -> list[str]:
    """
    Extract signals from a message and persist them to the DB.
    Returns list of inserted observation IDs.

    v2 additions:
    - latency_ms: ms since last Lobster reply (response latency signal)
    - previous_topic: topic of previous message (for topic shift detection)
    - previous_message_ts: timestamp of previous message (for follow-up detection)
    """
    ts = message_ts or datetime.utcnow()
    reply_length = len(message_text)
    signals = extract_signals(message_text, message_id, context, metadata)

    # Add timing signal
    signals.append(extract_timing_signal(message_id, ts, context))

    # v2: Add latency signal
    if latency_ms is not None:
        lat_signal = extract_latency_signal(message_id, latency_ms, context)
        if lat_signal:
            signals.append(lat_signal)

    # v2: Add message length signal
    len_signal = extract_length_signal(message_id, message_text, context)
    if len_signal:
        signals.append(len_signal)

    # v2: Detect follow-up
    current_topic = _detect_topic(message_text)
    if previous_topic and previous_message_ts:
        followup_signal = extract_followup_signal(
            message_id, message_text, previous_topic, previous_message_ts, ts, context
        )
        if followup_signal:
            signals.append(followup_signal)

    # v2: Detect topic shift
    if previous_topic and current_topic and previous_message_ts:
        shift_signal = extract_topic_shift_signal(
            message_id, current_topic, previous_topic, context,
            previous_message_ts, ts
        )
        if shift_signal:
            signals.append(shift_signal)

    # Persist
    obs_ids = []
    for obs in signals:
        obs_id = _insert_observation_v2(
            conn, obs,
            latency_ms=latency_ms,
            reply_length=reply_length,
            is_followup=bool(
                any(s.content.startswith("followup:") for s in signals
                    if hasattr(s, "content") and s.content)
            ),
        )
        obs_ids.append(obs_id)

    # v2: Update activity rhythm
    try:
        from .rhythm import record_message_rhythm
        record_message_rhythm(conn, ts, reply_length, latency_ms)
    except Exception:
        pass

    # Update last observation timestamp
    set_metadata_value(conn, "last_observation_at", ts.isoformat())

    # Record activity rhythm (non-critical)
    try:
        from .rhythm import record_message_rhythm
        record_message_rhythm(conn, ts, len(message_text))
    except Exception:
        pass

    return obs_ids


def _insert_observation_v2(
    conn: sqlite3.Connection,
    obs: Observation,
    latency_ms: int | None = None,
    reply_length: int | None = None,
    is_followup: bool = False,
) -> str:
    """Insert an observation with v2 columns if available."""
    import json as _json
    obs_id = insert_observation(conn, obs)
    # Try to update v2 columns (may not exist on old DBs)
    try:
        conn.execute(
            """UPDATE um_observations
               SET latency_ms=?, reply_length=?, is_followup=?, source_specificity=?
               WHERE id=?""",
            (
                latency_ms,
                reply_length,
                1 if is_followup else 0,
                _get_source_specificity(obs),
                obs_id,
            ),
        )
        conn.commit()
    except Exception:
        pass  # v2 columns don't exist yet — safe to skip
    return obs_id


def _get_source_specificity(obs: Observation) -> str:
    """Determine source specificity for an observation."""
    if obs.signal_type == ObservationSignalType.PREFERENCE:
        return "explicit"
    if obs.signal_type in (ObservationSignalType.LATENCY, ObservationSignalType.LENGTH,
                            ObservationSignalType.ENGAGEMENT):
        return "behavioral"
    return "heuristic"


# ---------------------------------------------------------------------------
# v2: New signal extractors
# ---------------------------------------------------------------------------

def extract_latency_signal(
    message_id: str,
    latency_ms: int,
    context: str = "",
) -> Observation | None:
    """
    Classify response latency as an engagement signal.

    Categories:
    - < 30,000ms (30s): 'immediate' — very high interest
    - 30s–2min: 'quick' — engaged
    - 2min–10min: None — normal, not informative
    - 10min–60min: 'slow' — lower engagement
    - > 60min: None — separate session, not a follow-up signal
    """
    if latency_ms < 30_000:
        content = "immediate"
        confidence = 0.85
    elif latency_ms < 120_000:
        content = "quick"
        confidence = 0.75
    elif latency_ms < 600_000:
        return None  # Normal latency — not informative
    elif latency_ms < 3_600_000:
        content = "slow"
        confidence = 0.7
    else:
        return None  # New session — not a latency signal

    return Observation(
        id=None,
        message_id=message_id,
        signal_type=ObservationSignalType.LATENCY,
        content=content,
        confidence=confidence,
        context=context,
        metadata={"latency_ms": latency_ms},
        observed_at=datetime.utcnow(),
    )


def extract_length_signal(
    message_id: str,
    message_text: str,
    context: str = "",
) -> Observation | None:
    """
    Message length as engagement proxy.
    Only records signals at extremes (< 30 chars or > 300 chars).
    """
    length = len(message_text.strip())

    if length < 30:
        content = "very_short"
        confidence = 0.7
    elif length > 500:
        content = "very_long"
        confidence = 0.8
    elif length > 300:
        content = "long"
        confidence = 0.7
    else:
        return None  # Normal length — not informative

    return Observation(
        id=None,
        message_id=message_id,
        signal_type=ObservationSignalType.LENGTH,
        content=content,
        confidence=confidence,
        context=context,
        metadata={"char_count": length},
        observed_at=datetime.utcnow(),
    )


def extract_followup_signal(
    message_id: str,
    message_text: str,
    previous_topic: str | None,
    previous_message_ts: datetime,
    current_ts: datetime,
    context: str = "",
) -> Observation | None:
    """
    Detect follow-up: same topic continuation within 5 minutes.
    A follow-up is strong evidence that the topic matters to the user.
    """
    if not previous_topic:
        return None

    # Check time delta
    delta_seconds = (current_ts - previous_message_ts).total_seconds()
    if delta_seconds > 300:  # 5 minutes
        return None

    # Check topic continuity
    current_topic = _detect_topic(message_text)
    if current_topic != previous_topic:
        return None

    return Observation(
        id=None,
        message_id=message_id,
        signal_type=ObservationSignalType.TOPIC,
        content=f"followup:{current_topic}",
        confidence=0.8,
        context=context,
        metadata={
            "topic": current_topic,
            "delta_seconds": int(delta_seconds),
        },
        observed_at=current_ts,
    )


def extract_topic_shift_signal(
    message_id: str,
    current_topic: str | None,
    previous_topic: str | None,
    context: str = "",
    previous_ts: datetime | None = None,
    current_ts: datetime | None = None,
) -> Observation | None:
    """
    Detect abrupt topic shifts within 10 minutes.
    May indicate frustration, cognitive load, or context switch.
    """
    if not current_topic or not previous_topic:
        return None
    if current_topic == previous_topic:
        return None

    # Only flag rapid shifts (within 10 minutes)
    if previous_ts and current_ts:
        delta = (current_ts - previous_ts).total_seconds()
        if delta > 600:
            return None  # Slow shift — normal, not notable

    return Observation(
        id=None,
        message_id=message_id,
        signal_type=ObservationSignalType.TOPIC,
        content=f"shift:{previous_topic}\u2192{current_topic}",
        confidence=0.65,
        context=context,
        metadata={"from_topic": previous_topic, "to_topic": current_topic},
        observed_at=current_ts or datetime.utcnow(),
    )
