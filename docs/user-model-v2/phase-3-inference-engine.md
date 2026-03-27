# Phase 3: Inference Engine (model_infer)

*Part of User Model v2 — see [PLAN.md](PLAN.md)*

---

## Goal

Build `model_infer`: a context-aware prediction function that estimates the user's current state and likely needs, given the current context. Results are cached for 30 minutes to avoid redundant computation.

## What `model_infer` Predicts

Given: current contexts, optional recent message, optional task description.

Produces:
1. **Mood estimate** — VAD state inferred from recent observations + emotional baseline
2. **Response style hint** — preferred length, tone, and detail level for this interaction
3. **Likely next request type** — follow-up, new topic, action request, or information request
4. **Value alignment score** — if a task is provided, how well it aligns with the user's values

All predictions carry confidence scores. Low confidence is reported honestly rather than fabricated.

## Implementation: `infer.py`

### Core Data Structure

```python
@dataclass
class InferenceResult:
    """Full prediction result from model_infer."""
    mood_estimate: MoodEstimate
    response_style: ResponseStyleHint
    likely_next: LikelyNextRequest
    value_alignment: ValueAlignmentScore | None  # Only if task_description provided
    computed_at: datetime
    expires_at: datetime
    cached: bool = False
    context_used: list[str] = field(default_factory=list)


@dataclass
class MoodEstimate:
    valence: float      # -1.0 to +1.0
    arousal: float      # 0.0 to 1.0
    dominance: float    # 0.0 to 1.0
    confidence: float
    basis: str          # Human-readable: "Based on 3 recent observations"


@dataclass
class ResponseStyleHint:
    preferred_length: str   # 'brief' | 'medium' | 'detailed'
    tone: str               # 'direct' | 'explanatory' | 'conversational'
    include_rationale: bool
    confidence: float


@dataclass
class LikelyNextRequest:
    request_type: str   # 'action' | 'information' | 'followup' | 'new_topic'
    topic_continuity: float  # 0.0-1.0 how likely current topic continues
    confidence: float


@dataclass
class ValueAlignmentScore:
    score: float             # 0.0-1.0
    aligned_values: list[str]
    misaligned_values: list[str]
    confidence: float
```

### Mood Estimation Algorithm

```python
def estimate_mood(
    conn: sqlite3.Connection,
    recent_hours: int = 4,
) -> MoodEstimate:
    """
    Estimate current mood from:
    1. Recent emotional states (last 4 hours, high weight)
    2. Recent sentiment observations (last 4 hours)
    3. 30-day emotional baseline (low weight, provides floor)
    4. Activity pattern (is this a normally active time? boosts dominance)

    Algorithm:
    - recent_states = get_recent_emotional_states(limit=5) from last 4h
    - recent_sentiment = get_recent_observations(hours=4, signal_type='sentiment')
    - baseline = get_emotional_baseline(days=30)

    If recent_states exist:
      Weighted average: recent 70%, baseline 30%
      confidence = min(0.9, 0.4 + len(recent_states) * 0.1)

    If only sentiment observations:
      Infer VAD from sentiment (positive→valence=0.5, negative→valence=-0.4)
      confidence = 0.5

    If only baseline:
      Return baseline with confidence = 0.4

    If nothing:
      Return neutral (0.0, 0.5, 0.5) with confidence = 0.2
    """
```

### Response Style Algorithm

```python
def infer_response_style(
    conn: sqlite3.Connection,
    mood: MoodEstimate,
    contexts: list[str],
) -> ResponseStyleHint:
    """
    Infer preferred response style from:
    1. Active preference nodes for 'response_style', 'concise', 'detail'
    2. Current mood (high arousal → prefer brief; high dominance → prefer direct)
    3. Time-of-day (morning/late night → prefer brief; afternoon → more receptive to detail)
    4. Activity rhythm (is the user in a normally active period? → more engaged)

    Default: brief + direct (matches observed behavior from owner.toml defaults)

    Override rules (in priority order):
    1. HARD constraint nodes always win
    2. High-confidence (>0.8) STATED preferences override inferences
    3. Mood-based adjustments are soft (can be overridden by stated preferences)

    High arousal (>0.7): → prefer 'brief'
    High dominance (>0.7): → prefer 'direct'
    Morning (5-9am): → prefer 'brief'
    Coding context: → prefer 'direct' with 'include_rationale=False' unless debugging
    """
```

### Likely Next Request Algorithm

```python
def predict_next_request(
    conn: sqlite3.Connection,
    recent_message: str | None = None,
    recent_hours: int = 1,
) -> LikelyNextRequest:
    """
    Predict likely next request type from:
    1. Recent follow-up observations (is the user in a follow-up pattern?)
    2. Recent topic observations (what was just discussed?)
    3. Time since last message (long gap → more likely new topic)
    4. High-energy observations (urgency signals → likely action request)

    If recent_message provided: detect intent signals in text
      - Contains verbs like 'do', 'make', 'create', 'fix', 'update' → 'action'
      - Contains '?', 'what', 'how', 'why', 'explain' → 'information'
      - References prior message → 'followup'
      - Otherwise → 'new_topic'

    topic_continuity: fraction of recent observations in last 30m on same topic
    """
```

### Value Alignment Algorithm

```python
def score_value_alignment(
    conn: sqlite3.Connection,
    task_description: str,
    contexts: list[str] | None = None,
) -> ValueAlignmentScore:
    """
    Score how well a task aligns with the user's values.

    Algorithm:
    1. Get all value nodes (NodeType.VALUE) with confidence > 0.5
    2. For each value, check if task_description keywords suggest alignment or conflict:
       - Simple keyword matching first (fast, heuristic)
       - Score: +1.0 for clear alignment, -0.5 for tension, 0 for unrelated
    3. Aggregate: weighted average by value strength

    Example:
      Values: autonomy (strength=0.9), craftsmanship (0.8), speed (0.5)
      Task: "Build a quick throwaway script to export CSV"
        - autonomy: neutral (0)
        - craftsmanship: mild tension ("quick throwaway" → -0.3)
        - speed: aligned (+0.8)
      Result: (0*0.9 + -0.3*0.8 + 0.8*0.5) / (0.9+0.8+0.5) = 0.16/2.2 = 0.07
      → Low alignment score, aligned_values=['speed'], misaligned=['craftsmanship']

    If no value nodes: return score=0.5, confidence=0.2 (no information)
    """
```

## Cache Strategy

Cache key format: `infer:{contexts_sorted}:{hour_bucket}:{recent_obs_hash}`

- `contexts_sorted`: sorted, joined context list (e.g., `"coding,work"`)
- `hour_bucket`: current hour (predictions differ by hour)
- `recent_obs_hash`: CRC32 of last 5 observation IDs (invalidates cache when new signals arrive)

TTL: 30 minutes by default, 15 minutes when recent observations are changing rapidly (>3 observations in last hour).

```python
def _compute_cache_key(
    contexts: list[str],
    conn: sqlite3.Connection,
) -> str:
    """Compute a stable cache key for the given context."""
    import zlib
    sorted_ctx = ",".join(sorted(contexts))
    hour = datetime.utcnow().hour
    recent = get_recent_observations(conn, hours=1, limit=5)
    obs_hash = zlib.crc32("|".join(o.id or "" for o in recent).encode()) & 0xFFFFFFFF
    return f"infer:{sorted_ctx}:{hour}:{obs_hash}"
```

## Context Injection into Main Loop

`UserModel.get_context()` in `__init__.py` currently returns only the top-5 preference nodes. v2 extends it to optionally include an inference hint:

```python
def get_context(
    self,
    contexts: list[str] | None = None,
    include_inference: bool = False,
) -> str:
    """
    Return context snippet for injection into system prompt.

    If include_inference=True (enabled after first week of observations):
      Appends a brief inference hint:
      "Current state hint: moderate engagement, direct tone preferred, likely continuing coding topic."
    """
```

The `include_inference` flag is auto-enabled when the model has >20 observations. Before that threshold, inference results are too unreliable to inject into the main loop.

## MCP Tool Definition

```python
{
    "name": "model_infer",
    "description": (
        "Given current context, predict the user's likely state and needs. "
        "Returns mood estimate, response style hint, likely next request type, "
        "and optional value alignment score for a task. Results cached 30 minutes."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "context": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Active contexts (e.g. ['work', 'coding', 'morning']).",
                "default": [],
            },
            "recent_message": {
                "type": "string",
                "description": "Optional: the most recent user message text for context.",
            },
            "task_description": {
                "type": "string",
                "description": "Optional: task to score for value alignment.",
            },
            "force_refresh": {
                "type": "boolean",
                "description": "If true, bypass cache and recompute. Default: false.",
                "default": False,
            },
        },
    },
}
```

## Test Plan

- [ ] `estimate_mood` returns neutral (0.0, 0.5, 0.5, confidence=0.2) with no data
- [ ] `estimate_mood` correctly weights recent states over baseline
- [ ] `infer_response_style` returns 'brief' + 'direct' when no preference nodes exist (defaults)
- [ ] `infer_response_style` respects HARD constraint nodes over mood adjustments
- [ ] `predict_next_request` returns 'action' for "please fix this bug" message
- [ ] `predict_next_request` returns 'information' for "how does X work?" message
- [ ] `score_value_alignment` returns 0.5 confidence when no value nodes exist
- [ ] Cache correctly invalidates when new observations arrive
- [ ] Cache correctly returns cached result and increments hit_count
- [ ] `force_refresh=True` bypasses cache
- [ ] Full `model_infer` call round-trip: returns all fields populated
- [ ] `get_context(include_inference=True)` includes hint when >20 observations exist
