"""
Inference Engine: context-aware prediction of user state and needs.

Implements the `model_infer` MCP tool — given current context, predicts:
1. Mood estimate (VAD)
2. Response style hint (brief/detailed, direct/explanatory)
3. Likely next request type (action/information/followup/new_topic)
4. Value alignment score for a task (optional)

Results are cached for 30 minutes to avoid redundant computation.

Depends on: schema.py, db.py, emotional_model.py, rhythm.py only.
"""

import re
import sqlite3
import zlib
from datetime import datetime, timedelta
from typing import Any

from .db import (
    cleanup_expired_cache,
    get_all_preference_nodes,
    get_cached_inference,
    get_emotional_baseline,
    get_peak_activity_hours,
    get_recent_observations,
    get_recent_emotional_states,
    set_cached_inference,
)
from .rhythm import get_current_activity_level
from .schema import NodeFlexibility, NodeSource, NodeType, ObservationSignalType


# ---------------------------------------------------------------------------
# Mood estimation
# ---------------------------------------------------------------------------

def estimate_mood(
    conn: sqlite3.Connection,
    recent_hours: int = 4,
) -> dict[str, Any]:
    """
    Estimate current mood from recent emotional states and sentiment observations.

    Priority:
    1. Recent emotional states (last 4h) — high weight (70%)
    2. Recent sentiment observations (last 4h) — supplementary
    3. 30-day baseline — fallback (30%)

    Returns: {"valence": float, "arousal": float, "dominance": float, "confidence": float, "basis": str}
    """
    recent_states = get_recent_emotional_states(conn, limit=5)
    # Filter to recent_hours
    cutoff = datetime.utcnow() - timedelta(hours=recent_hours)
    recent_states = [s for s in recent_states if s.recorded_at > cutoff]

    baseline = get_emotional_baseline(conn, days=30)

    if recent_states:
        # Weighted average of recent states
        n = len(recent_states)
        avg_v = sum(s.valence for s in recent_states) / n
        avg_a = sum(s.arousal for s in recent_states) / n
        avg_d = sum(s.dominance for s in recent_states) / n
        confidence = min(0.9, 0.4 + n * 0.1)

        if baseline:
            # Blend: 70% recent, 30% baseline
            avg_v = 0.7 * avg_v + 0.3 * baseline["valence"]
            avg_a = 0.7 * avg_a + 0.3 * baseline["arousal"]
            avg_d = 0.7 * avg_d + 0.3 * baseline["dominance"]

        return {
            "valence": round(avg_v, 3),
            "arousal": round(avg_a, 3),
            "dominance": round(avg_d, 3),
            "confidence": round(confidence, 3),
            "basis": f"Based on {n} recent observation(s)",
        }

    # Fall back to sentiment observations
    sentiment_obs = get_recent_observations(conn, hours=recent_hours, signal_type="sentiment", limit=5)
    if sentiment_obs:
        valence_map = {"positive": 0.5, "negative": -0.4, "neutral": 0.0}
        valences = [valence_map.get(o.content, 0.0) * o.confidence for o in sentiment_obs]
        avg_v = sum(valences) / len(valences)

        if baseline:
            avg_v = 0.6 * avg_v + 0.4 * baseline["valence"]
            return {
                "valence": round(avg_v, 3),
                "arousal": round(baseline["arousal"], 3),
                "dominance": round(baseline["dominance"], 3),
                "confidence": 0.5,
                "basis": f"From {len(sentiment_obs)} sentiment signals",
            }
        return {
            "valence": round(avg_v, 3),
            "arousal": 0.5,
            "dominance": 0.5,
            "confidence": 0.4,
            "basis": "From sentiment signals only",
        }

    if baseline:
        return {
            "valence": round(baseline["valence"], 3),
            "arousal": round(baseline["arousal"], 3),
            "dominance": round(baseline["dominance"], 3),
            "confidence": 0.35,
            "basis": "30-day baseline only",
        }

    return {
        "valence": 0.0,
        "arousal": 0.5,
        "dominance": 0.5,
        "confidence": 0.2,
        "basis": "No observation data — using neutral defaults",
    }


# ---------------------------------------------------------------------------
# Response style inference
# ---------------------------------------------------------------------------

def infer_response_style(
    conn: sqlite3.Connection,
    mood: dict[str, Any],
    contexts: list[str],
) -> dict[str, Any]:
    """
    Infer preferred response style from preference nodes, mood, and time-of-day.

    Returns: {"preferred_length": str, "tone": str, "include_rationale": bool, "confidence": float}
    """
    now = datetime.utcnow()
    hour = now.hour

    # Defaults
    length = "brief"
    tone = "direct"
    include_rationale = False
    confidence = 0.5

    # Check preference nodes for response style signals
    from .preference_graph import resolve_preferences
    prefs = resolve_preferences(conn, contexts, min_confidence=0.5)

    # HARD constraints override everything
    for pref in prefs:
        if pref.flexibility == NodeFlexibility.HARD:
            name_lower = pref.name.lower()
            desc_lower = pref.description.lower()
            combined = name_lower + " " + desc_lower
            if any(w in combined for w in ["concise", "brief", "short", "terse"]):
                length = "brief"
                confidence = 0.9
            elif any(w in combined for w in ["detail", "thorough", "comprehensive"]):
                length = "detailed"
                confidence = 0.9
            if any(w in combined for w in ["direct", "blunt"]):
                tone = "direct"
                confidence = max(confidence, 0.85)

    # STATED high-confidence preferences (if no HARD constraints already resolved this)
    if confidence < 0.85:
        for pref in prefs:
            if pref.source == NodeSource.STATED and pref.confidence >= 0.8:
                name_lower = pref.name.lower()
                desc_lower = pref.description.lower()
                combined = name_lower + " " + desc_lower
                if any(w in combined for w in ["concise", "brief", "short"]):
                    length = "brief"
                    confidence = max(confidence, 0.8)
                elif any(w in combined for w in ["detail", "thorough", "explain"]):
                    length = "detailed"
                    confidence = max(confidence, 0.8)
                if any(w in combined for w in ["direct", "rationale", "explain why"]):
                    include_rationale = "rationale" in combined or "explain why" in combined
                    confidence = max(confidence, 0.75)

    # Mood-based soft adjustments (lower priority than stated prefs)
    if confidence < 0.75:
        arousal = mood.get("arousal", 0.5)
        dominance = mood.get("dominance", 0.5)
        if arousal > 0.7:
            length = "brief"
            confidence = max(confidence, 0.6)
        if dominance > 0.7:
            tone = "direct"
            confidence = max(confidence, 0.6)

    # Time-of-day soft adjustment
    if confidence < 0.65:
        if hour < 9 or hour >= 22:
            length = "brief"
            tone = "direct"
            confidence = max(confidence, 0.55)

    # Context-based adjustment
    if "coding" in contexts or "debugging" in contexts:
        tone = "direct"
        include_rationale = "debugging" in contexts  # debugging = want rationale
        confidence = max(confidence, 0.6)

    return {
        "preferred_length": length,
        "tone": tone,
        "include_rationale": include_rationale,
        "confidence": round(confidence, 3),
    }


# ---------------------------------------------------------------------------
# Next request prediction
# ---------------------------------------------------------------------------

_ACTION_PATTERNS = re.compile(
    r"\b(do|make|create|fix|update|write|build|run|add|remove|delete|deploy|check|send|get|fetch)\b",
    re.I,
)
_INFORMATION_PATTERNS = re.compile(
    r"\b(what|how|why|explain|describe|tell me|show me|what is|what are|help me understand)\b",
    re.I,
)
_FOLLOWUP_PATTERNS = re.compile(
    r"\b(also|and also|another thing|what about|can you also|one more|actually|wait|but|however)\b",
    re.I,
)


def predict_next_request(
    conn: sqlite3.Connection,
    recent_message: str | None = None,
    recent_hours: int = 1,
) -> dict[str, Any]:
    """
    Predict the likely next request type based on behavioral signals and message intent.

    Returns: {"request_type": str, "topic_continuity": float, "confidence": float}
    """
    request_type = "new_topic"
    confidence = 0.4
    topic_continuity = 0.5

    # Message-level intent detection (highest priority)
    if recent_message:
        text = recent_message.strip()
        if _FOLLOWUP_PATTERNS.search(text):
            request_type = "followup"
            confidence = 0.75
            topic_continuity = 0.85
        elif _ACTION_PATTERNS.search(text) and not _INFORMATION_PATTERNS.search(text):
            request_type = "action"
            confidence = 0.7
            topic_continuity = 0.6
        elif _INFORMATION_PATTERNS.search(text):
            request_type = "information"
            confidence = 0.7
            topic_continuity = 0.5

    # Behavioral signal check (supplement message-level)
    if confidence < 0.7:
        followup_obs = get_recent_observations(conn, hours=recent_hours, signal_type="topic", limit=10)
        followup_count = sum(1 for o in followup_obs if o.is_followup if hasattr(o, "is_followup") and o.is_followup)
        if followup_count >= 2:
            request_type = "followup"
            topic_continuity = min(0.9, 0.5 + followup_count * 0.1)
            confidence = max(confidence, 0.65)

        energy_obs = get_recent_observations(conn, hours=recent_hours, signal_type="energy", limit=5)
        high_energy = [o for o in energy_obs if o.content == "high"]
        if high_energy:
            request_type = "action"
            confidence = max(confidence, 0.6)

    return {
        "request_type": request_type,
        "topic_continuity": round(topic_continuity, 3),
        "confidence": round(confidence, 3),
    }


# ---------------------------------------------------------------------------
# Value alignment scoring
# ---------------------------------------------------------------------------

# Simple keyword-to-value-group mapping for alignment detection
_VALUE_ALIGNMENT_KEYWORDS: dict[str, list[str]] = {
    "autonomy": ["independent", "own choice", "self-direct", "autonomous", "freedom", "alone"],
    "technical-depth": ["deep dive", "understand", "internals", "how it works", "fundamentals", "architecture"],
    "craftsmanship": ["quality", "elegant", "clean", "refactor", "correct", "robust", "maintainable"],
    "directness": ["direct", "clear", "concise", "straight", "blunt", "no hedging"],
    "speed": ["quick", "fast", "rapid", "asap", "throwaway", "prototype", "hack"],
    "collaboration": ["discuss", "review", "together", "team", "consult", "feedback"],
}

_VALUE_TENSION_KEYWORDS: dict[str, list[str]] = {
    "craftsmanship": ["hack", "quick", "throwaway", "rough", "messy"],
    "technical-depth": ["skip", "just work", "don't explain", "black box"],
    "autonomy": ["ask permission", "check first", "wait for approval"],
    "directness": ["lengthy explanation", "comprehensive overview", "in detail"],
}


def score_value_alignment(
    conn: sqlite3.Connection,
    task_description: str,
    contexts: list[str] | None = None,
) -> dict[str, Any]:
    """
    Score how well a task aligns with the user's current values.

    Returns:
    {
      "score": float (0.0-1.0, 0.5 = neutral),
      "aligned_values": list[str],
      "misaligned_values": list[str],
      "confidence": float
    }
    """
    value_nodes = get_all_preference_nodes(conn, node_type=NodeType.VALUE, min_confidence=0.5)

    if not value_nodes:
        return {
            "score": 0.5,
            "aligned_values": [],
            "misaligned_values": [],
            "confidence": 0.2,
        }

    task_lower = task_description.lower()
    aligned: list[str] = []
    misaligned: list[str] = []
    weighted_scores: list[tuple[float, float]] = []  # (score, weight)

    for node in value_nodes:
        node_name = node.name.lower().replace("-", "_").replace(" ", "_")
        node_key = node.name.lower().replace(" ", "-")

        # Check alignment keywords
        alignment_score = 0.0
        align_keywords = _VALUE_ALIGNMENT_KEYWORDS.get(node_key, [])
        tension_keywords = _VALUE_TENSION_KEYWORDS.get(node_key, [])

        align_hit = any(kw in task_lower for kw in align_keywords)
        tension_hit = any(kw in task_lower for kw in tension_keywords)

        # Also check node name/description keywords in task
        if any(w in task_lower for w in node.name.lower().split("-")):
            align_hit = True

        if align_hit and not tension_hit:
            alignment_score = 0.8
            aligned.append(node.name)
        elif tension_hit and not align_hit:
            alignment_score = 0.2
            misaligned.append(node.name)
        else:
            alignment_score = 0.5  # Neutral

        weighted_scores.append((alignment_score, node.strength))

    if not weighted_scores:
        return {"score": 0.5, "aligned_values": [], "misaligned_values": [], "confidence": 0.2}

    total_weight = sum(w for _, w in weighted_scores)
    if total_weight == 0:
        final_score = 0.5
    else:
        final_score = sum(s * w for s, w in weighted_scores) / total_weight

    # Confidence scales with number of value nodes and whether any matched
    confidence = min(0.85, 0.3 + len(value_nodes) * 0.05 + (0.2 if aligned or misaligned else 0))

    return {
        "score": round(final_score, 3),
        "aligned_values": aligned[:5],
        "misaligned_values": misaligned[:5],
        "confidence": round(confidence, 3),
    }


# ---------------------------------------------------------------------------
# Cache key computation
# ---------------------------------------------------------------------------

def _compute_cache_key(
    conn: sqlite3.Connection,
    contexts: list[str],
) -> str:
    """Compute a stable cache key for the given context."""
    sorted_ctx = ",".join(sorted(contexts))
    hour = datetime.utcnow().hour
    recent = get_recent_observations(conn, hours=1, limit=5)
    obs_ids = "|".join(o.id or "" for o in recent)
    obs_hash = zlib.crc32(obs_ids.encode()) & 0xFFFFFFFF
    return f"infer:{sorted_ctx}:{hour}:{obs_hash}"


# ---------------------------------------------------------------------------
# Main entry point: model_infer
# ---------------------------------------------------------------------------

def run_inference(
    conn: sqlite3.Connection,
    contexts: list[str] | None = None,
    recent_message: str | None = None,
    task_description: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Main entry point for model_infer.

    Returns a full inference result dict with:
    - mood_estimate
    - response_style
    - likely_next
    - value_alignment (only if task_description provided)
    - cached: bool
    - expires_at: ISO string
    """
    contexts = contexts or []

    # Check cache first
    cache_key = _compute_cache_key(conn, contexts)
    if not force_refresh:
        cached = get_cached_inference(conn, cache_key)
        if cached:
            cached["cached"] = True
            return cached

    # Cleanup stale cache entries periodically
    cleanup_expired_cache(conn)

    # Compute all predictions
    mood = estimate_mood(conn)
    response_style = infer_response_style(conn, mood, contexts)
    likely_next = predict_next_request(conn, recent_message)

    result: dict[str, Any] = {
        "mood_estimate": mood,
        "response_style": response_style,
        "likely_next": likely_next,
        "context_used": contexts,
        "computed_at": datetime.utcnow().isoformat(),
        "cached": False,
    }

    if task_description:
        result["value_alignment"] = score_value_alignment(conn, task_description, contexts)

    # Cache the result (without task_description — task alignment is not cacheable)
    cacheable = {k: v for k, v in result.items() if k != "value_alignment"}
    ttl = 30
    # Shorten TTL if data is thin (low confidence)
    if mood.get("confidence", 0) < 0.4:
        ttl = 15
    expires = datetime.utcnow() + timedelta(minutes=ttl)
    result["expires_at"] = expires.isoformat()
    cacheable["expires_at"] = expires.isoformat()

    avg_confidence = (
        mood.get("confidence", 0.3)
        + response_style.get("confidence", 0.3)
        + likely_next.get("confidence", 0.3)
    ) / 3.0
    set_cached_inference(conn, cache_key, cacheable, avg_confidence, ttl_minutes=ttl)

    return result


# ---------------------------------------------------------------------------
# Context summary for main loop injection
# ---------------------------------------------------------------------------

def get_inference_context_hint(
    conn: sqlite3.Connection,
    contexts: list[str] | None = None,
    min_observation_count: int = 20,
) -> str:
    """
    Return a brief inference hint for injection into the system prompt.
    Only returns non-empty string when there are enough observations to be meaningful.

    Format: "State hint: [mood], [style], [likely next]."
    """
    from .db import get_model_metadata
    meta = get_model_metadata(conn)
    if meta.observation_count < min_observation_count:
        return ""

    try:
        result = run_inference(conn, contexts or [])
        mood = result.get("mood_estimate", {})
        style = result.get("response_style", {})
        likely = result.get("likely_next", {})

        valence = mood.get("valence", 0.0)
        mood_desc = "positive" if valence > 0.2 else "negative" if valence < -0.2 else "neutral"
        length = style.get("preferred_length", "brief")
        tone = style.get("tone", "direct")
        next_type = likely.get("request_type", "unknown")

        return (
            f"[User model: mood={mood_desc}, prefer={length}/{tone}, "
            f"likely_next={next_type}]"
        )
    except Exception:
        return ""
