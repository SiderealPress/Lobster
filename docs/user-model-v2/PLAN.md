# User Model v2: Comprehensive Preference, Values & Priority Prediction System

*Branch: `user-model-v2` | Date: 2026-03-08 | Status: Active planning + implementation*

---

## Current State (What Exists)

The User Model v1 — implemented in `src/mcp/user_model/` — is a complete, well-architected system covering:

### Schema & Storage
- SQLite-backed, namespaced tables (`um_*`), schema v1
- `PreferenceNode` — directed graph of values → principles → preferences → constraints
- `Observation` — raw signals extracted from messages
- `EmotionalState` — VAD (Valence-Arousal-Dominance) snapshots
- `BlindSpot`, `Contradiction`, `NarrativeArc`, `LifePattern`, `AttentionItem`

### Observation Pipeline (Tier 1, Heuristic)
- Sentiment detection (positive/negative keyword matching)
- Energy level detection (urgency keywords, exclamation marks)
- Correction detection (regex patterns)
- Preference statement detection (regex patterns)
- Topic detection (keyword clusters: coding, health, work, finance, learning, planning)
- Timing signal (time-of-day bucketing)

### Inference & Prediction
- Preference graph with inheritance, context scoping, conflict resolution
- Decay: unobserved preferences lose confidence/strength over time (corrected nodes exempt)
- Nightly consolidation pipeline: decay → contradiction detection → attention refresh → markdown sync
- Attention scoring: Eisenhower-matrix-weighted (urgency 35%, importance 35%, alignment 20%, recency 10%)
- Attention items derived from: narrative arcs, life patterns, high-energy observations

### MCP Tools (7)
- `model_observe` — auto-extract signals or record explicit observations
- `model_query` — structured queries (preferences, observations, emotions, arcs, patterns, etc.)
- `model_preferences` — context-resolved preference list with inheritance
- `model_reflect` — trigger heuristic synthesis pass
- `model_correct` — apply user correction (sets confidence to 1.0, immune to decay)
- `model_inspect` — deep-read a specific entity with graph edges
- `model_attention` — get scored attention stack

### File Layer
- Markdown sync: `~/lobster-workspace/user-model/` mirrors the DB
- Subdirs: `values/`, `principles/`, `preferences/`, `constraints/`
- Root files: `emotional-baseline.md`, `active-arcs.md`, `patterns.md`, `blind-spots.md`, `contradictions.md`, `attention.md`, `_index.md`

### Infrastructure
- Feature-flagged: `LOBSTER_USER_MODEL=true` to enable
- Owner identity: `~/lobster-config/owner.toml` (telegram_chat_id, name, email)
- DB: `~/lobster-workspace/data/memory.db` (shared with memory subsystem)
- Active inquiry: budget-constrained clarifying questions (max 1 per 24h)

---

## Gap Analysis (What's Missing vs. The Vision)

The v1 system has excellent bones. What it lacks is **depth of signal capture**, **temporal modeling**, **predictive inference**, and **behavioral pattern learning**. Specifically:

### 1. Observation Gaps
- **No response latency tracking.** How quickly the user replies is a strong signal of interest/urgency. Currently ignored.
- **No message length as signal.** Short replies = disengagement; long replies = deep engagement. Not captured.
- **No follow-up detection.** When the user asks a follow-up within minutes, that topic scored high interest. Not tracked.
- **No topic shift tracking.** Moving from one topic to another mid-conversation tells us about cognitive load or frustration.
- **No explicit vs. implicit preference separation in storage.** Stated preferences vs. behavioral patterns both go into `um_observations` without distinguishing their inferential weight.
- **Tier 2 (embedding) and Tier 3 (LLM background) extraction stubs are not implemented.** The PRD specified a 3-tier extraction pipeline; only Tier 1 exists.

### 2. Temporal Modeling Gaps
- **No temporal snapshots.** There is no way to query "what were the user's values 3 months ago?" The model overwrites in-place.
- **No drift detection.** Week-over-week changes in preference strengths are not computed or surfaced.
- **No recency weighting in queries.** All observations are treated equally regardless of age in most queries.
- **No activity rhythm tracking.** When is the user most active? Most responsive? Most likely to make decisions? Not modeled.
- **Decay is uniform.** Every node decays at the same rate. High-confidence stated values should decay much slower than low-confidence inferences.

### 3. Prediction & Inference Gaps
- **No `model_infer` tool.** The PRD specified scenario modeling — given current context, predict the user's likely reaction, desired response length, probable next request. This does not exist.
- **Attention scoring is static.** It doesn't incorporate time-of-day, recent emotional state, or active project momentum.
- **No response style prediction.** "The user is in a high-urgency state right now — give shorter, more direct responses" is not surfaced to the main loop.
- **No value alignment scoring for tasks.** When the user is deciding between tasks, the model can't score which ones align better with their values.

### 4. Synthesis Gaps
- **`model_reflect` is heuristic-only.** Contradiction detection is keyword-based (e.g., "concise" vs "detail"). It can't detect semantic contradictions.
- **No weekly synthesis.** The nightly consolidation marks observations as processed but doesn't produce a structured "user state" summary or detect week-over-week shifts.
- **Nightly consolidation doesn't update preference nodes from observations.** It processes observations (marks them done) but doesn't actually extract preference updates from them. The inference gap is real: observations accumulate but don't feed back into the preference graph without explicit `model_correct` calls.
- **No conversation-level summarization.** Individual messages are observed, but conversation threads are not summarized into high-level preference signals.

### 5. Missing Data
- **Preference graph is empty.** Without seed data, the model has no foundation. v2 must bootstrap with seed values from `owner.toml` or a setup interview.
- **No stated values yet.** The `values/` directory is empty. The graph starts blank and depends entirely on inference.

---

## Architecture

### V2 Layered Model

```
┌─────────────────────────────────────────────────────────────────────┐
│                           MCP TOOL LAYER                             │
│  model_observe  model_infer  model_query  model_attention            │
│  model_reflect  model_correct  model_inspect  model_preferences      │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                      INFERENCE ENGINE (v2)                           │
│  Context prediction  |  Drift detection  |  Weekly synthesis         │
│  Value alignment     |  Scenario modeling |  Response style hints    │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                    OBSERVATION PIPELINE (v2)                         │
│  Tier 1: Heuristic (<50ms)  |  Response latency  |  Message length  │
│  Follow-up detection        |  Topic shift        |  Explicit/implicit│
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                      DATA MODEL (v2 — schema v2)                     │
│  PreferenceNode (+ decay_rate_override, seed_source)                 │
│  Observation (+ latency_ms, reply_length, is_followup)               │
│  TemporalSnapshot (weekly preference state captures)                 │
│  DriftRecord (detected changes between snapshots)                    │
│  InferenceResult (cached predictions with TTL)                       │
│  ActivityRhythm (hourly/daily activity pattern)                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Design Principles (Carried Forward)
- Observe greedily, infer conservatively, act sparingly
- Corrected nodes (source=corrected) are immune to all decay and never overridden
- Stated preferences outweigh 10 inferred ones
- All predictions carry confidence scores and TTLs
- Model must fail gracefully (no observations = Lobster works as before)

---

## Implementation Phases

### Phase 1: Enhanced Data Model
**Goal:** Extend the SQLite schema to support temporal snapshots, richer observations, and drift tracking.
Sub-plan: [phase-1-data-model.md](phase-1-data-model.md)

Changes:
- Schema migration v1 → v2 (new tables, new columns on existing tables)
- `TemporalSnapshot` table for weekly state captures
- `DriftRecord` table for detected changes between snapshots
- `ActivityRhythm` table for hourly/daily activity patterns
- `InferenceCache` table for cached predictions with TTL
- New columns on `um_observations`: `latency_ms`, `reply_length`, `is_followup`, `source_specificity`
- New columns on `um_preference_nodes`: `seed_source`, `decay_rate_override`, `temporal_weight`

### Phase 2: Strengthened Observation Pipeline
**Goal:** Capture richer behavioral signals from every interaction.
Sub-plan: [phase-2-observation-pipeline.md](phase-2-observation-pipeline.md)

Changes:
- Response latency extraction (requires message timestamp threading)
- Message length analysis (very short = likely disengagement, very long = high engagement)
- Follow-up detection (message within N minutes on same topic = strong interest signal)
- Topic shift detection (topics diverge between consecutive messages = cognitive load or frustration)
- Explicit vs. implicit signal labeling in observations
- Activity rhythm updates (track hourly message distribution over rolling 30 days)
- Seeding: first-run bootstrap from `owner.toml` values + optional setup interview

### Phase 3: Inference Engine
**Goal:** Build `model_infer` — context-aware prediction of the user's current state and likely needs.
Sub-plan: [phase-3-inference-engine.md](phase-3-inference-engine.md)

New function `model_infer`:
- Given current context (time, active project, recent messages), predict:
  - Current mood estimate (VAD + confidence)
  - Preferred response style (brief/detailed, direct/explanatory)
  - Likely next request type (follow-up, new topic, action request)
  - Value alignment score for a given task/decision
- Returns structured prediction with confidence scores
- Results cached in `InferenceCache` with 30-minute TTL

### Phase 4: Enhanced Reflection & Drift Detection
**Goal:** Make `model_reflect` produce structured weekly user state summaries and detect behavioral drift.
Sub-plan: [phase-4-prediction-api.md](phase-4-prediction-api.md)

Changes to consolidation pipeline:
- Weekly snapshot: capture full preference graph state as a `TemporalSnapshot`
- Drift detection: compare current snapshot to 4-week-ago snapshot, record significant changes
- Structured user state: produce a `user_state.md` in the file layer
- Week-over-week summary: "This week you were more focused on X, less on Y"
- Semantic contradiction detection via keyword expansion (beyond literal name matching)
- Observation-to-preference feedback loop: unprocessed high-confidence observations update the preference graph

---

## Data Model

### New Tables (Schema v2)

```sql
-- Temporal snapshots: weekly state captures for drift tracking
CREATE TABLE um_temporal_snapshots (
    id          TEXT PRIMARY KEY,
    snapshot_at TEXT NOT NULL,       -- ISO datetime of capture
    week_number INTEGER NOT NULL,    -- ISO week number
    year        INTEGER NOT NULL,
    data        TEXT NOT NULL        -- JSON: {preferences: [...], emotional_baseline: {...}}
);

-- Drift records: detected week-over-week changes
CREATE TABLE um_drift_records (
    id              TEXT PRIMARY KEY,
    detected_at     TEXT NOT NULL,
    snapshot_a_id   TEXT NOT NULL,   -- earlier snapshot
    snapshot_b_id   TEXT NOT NULL,   -- later snapshot
    drift_type      TEXT NOT NULL,   -- 'preference_shift' | 'value_drift' | 'pattern_change'
    description     TEXT NOT NULL,
    magnitude       REAL NOT NULL,   -- 0.0-1.0 how large the drift
    node_id         TEXT             -- if applicable, which preference node
);

-- Activity rhythm: hourly/daily activity distribution
CREATE TABLE um_activity_rhythm (
    id          TEXT PRIMARY KEY,
    hour_of_day INTEGER NOT NULL,    -- 0-23
    day_of_week INTEGER NOT NULL,    -- 0=Monday, 6=Sunday
    message_count INTEGER NOT NULL DEFAULT 0,
    avg_length  REAL NOT NULL DEFAULT 0.0,
    avg_latency REAL,                -- avg response latency in ms (null if first message)
    updated_at  TEXT NOT NULL
);

-- Inference cache: TTL-based prediction cache
CREATE TABLE um_inference_cache (
    id          TEXT PRIMARY KEY,
    cache_key   TEXT NOT NULL UNIQUE,
    result      TEXT NOT NULL,       -- JSON prediction result
    confidence  REAL NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);
```

### Modified Tables

```sql
-- um_observations: add behavioral metadata columns
ALTER TABLE um_observations ADD COLUMN latency_ms INTEGER;  -- response latency
ALTER TABLE um_observations ADD COLUMN reply_length INTEGER; -- user's message character count
ALTER TABLE um_observations ADD COLUMN is_followup INTEGER NOT NULL DEFAULT 0; -- boolean
ALTER TABLE um_observations ADD COLUMN source_specificity TEXT; -- 'explicit'|'behavioral'|'heuristic'

-- um_preference_nodes: add temporal and seed tracking
ALTER TABLE um_preference_nodes ADD COLUMN seed_source TEXT;  -- 'owner_toml'|'setup_interview'|'observed'
ALTER TABLE um_preference_nodes ADD COLUMN decay_rate_override REAL;  -- overrides default if set
ALTER TABLE um_preference_nodes ADD COLUMN temporal_weight REAL;      -- recency-weighted importance
```

---

## API / MCP Tools (New or Updated)

### New Tool: `model_infer`

```json
{
  "name": "model_infer",
  "description": "Given current context, predict the user's likely state and needs. Returns mood estimate, response style hint, likely next request type, and value alignment scores. Results are cached for 30 minutes.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "context": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Active contexts (e.g. ['work', 'coding', 'morning'])"
      },
      "recent_message": {
        "type": "string",
        "description": "Optional: the most recent user message, for on-the-fly context"
      },
      "task_description": {
        "type": "string",
        "description": "Optional: description of a task to score for value alignment"
      }
    }
  }
}
```

**Returns:**
```json
{
  "mood_estimate": {"valence": 0.3, "arousal": 0.6, "dominance": 0.7, "confidence": 0.65},
  "response_style": {
    "preferred_length": "brief",
    "tone": "direct",
    "include_rationale": false,
    "confidence": 0.7
  },
  "likely_next": {
    "request_type": "action",
    "topic_continuity": 0.8,
    "confidence": 0.55
  },
  "value_alignment": {
    "score": 0.82,
    "aligned_values": ["technical-depth", "autonomy"],
    "misaligned_values": [],
    "confidence": 0.75
  },
  "cached": false,
  "expires_at": "2026-03-08T10:30:00"
}
```

### Updated Tool: `model_reflect`

Adds:
- `produce_weekly_snapshot` parameter (default: true on Sunday runs)
- Returns `drift_detected` field with description of week-over-week changes
- Returns `user_state_summary` — structured 1-paragraph summary of current user state

### Updated Tool: `model_observe`

Adds:
- `latency_ms` parameter — response latency in milliseconds
- `reply_length` parameter — character count of this message
- `is_followup` parameter — whether this message follows up on the previous topic

### Updated Tool: `model_attention`

Adds:
- Time-of-day weighting (items more relevant at current hour score higher)
- Active project momentum weighting (items related to current narrative arcs score higher)
- Emotional state gating (when arousal is very high, urgent items dominate more strongly)

---

## Seed Bootstrap Strategy

The model starts empty. v2 must solve this. Two approaches:

### Approach A: owner.toml Seed (immediate)
Add a `[values]` and `[preferences]` section to `owner.toml` that gets ingested on first run:
```toml
[values]
autonomy = "I strongly value independence and making my own decisions"
technical_depth = "I prefer understanding systems deeply rather than using them as black boxes"
craftsmanship = "I care about the quality and elegance of what I build"

[preferences]
response_style = "concise"
code_style = "functional where possible"
```

### Approach B: First-Run Setup Interview (deferred to Phase 5)
On first enable, the inquiry module asks 3–5 targeted questions to seed the values graph.

v2 implements Approach A immediately, Approach B as a follow-on.

---

## Files Modified / Created

### New files:
- `src/mcp/user_model/temporal.py` — snapshot and drift detection
- `src/mcp/user_model/rhythm.py` — activity rhythm tracking and analysis
- `src/mcp/user_model/infer.py` — context-aware prediction engine
- `src/mcp/user_model/seed.py` — bootstrap from owner.toml values
- `docs/user-model-v2/PLAN.md` (this file)
- `docs/user-model-v2/phase-1-data-model.md`
- `docs/user-model-v2/phase-2-observation-pipeline.md`
- `docs/user-model-v2/phase-3-inference-engine.md`
- `docs/user-model-v2/phase-4-prediction-api.md`

### Modified files:
- `src/mcp/user_model/db.py` — schema v2 migration, new CRUD functions
- `src/mcp/user_model/schema.py` — new dataclasses
- `src/mcp/user_model/observation.py` — enhanced signal extraction
- `src/mcp/user_model/inference.py` — weekly snapshot + drift detection + obs→preference feedback
- `src/mcp/user_model/tools.py` — new `model_infer` tool, updated tool signatures
- `src/mcp/user_model/__init__.py` — export new tools
- `src/mcp/user_model/markdown_sync.py` — add `user_state.md` sync

---

## Success Metrics

1. **Observation richness**: After 1 week of use, the model should have >50 observations with behavioral signals (latency, length, follow-up) attached
2. **Preference graph populated**: At least 10 preference nodes (mix of seeded + inferred) with evidence_count > 1
3. **Inference quality**: `model_infer` returns results with >0.6 confidence for all fields given a week of observation data
4. **Drift detection**: After 2 weeks, at least one `DriftRecord` should exist showing a meaningful shift
5. **Response style accuracy**: Lobster's response length should visibly adjust based on `model_infer` response_style hints

---

## Open Questions

1. **Should `model_infer` results be injected automatically into the main loop context, or only when explicitly called?** Recommendation: auto-inject a brief summary alongside preference context in `get_context()`.

2. **How should the observation-to-preference feedback loop work?** Currently observations are marked processed but don't update nodes. Recommendation: high-confidence preference observations (>0.8) create/reinforce preference nodes during nightly consolidation.

3. **When is the first temporal snapshot taken?** Recommendation: on first `model_reflect` call after schema v2 migration, capture a baseline snapshot even if sparse.

4. **Should seed values from owner.toml have a different decay rate?** Recommendation: yes — seed values get `source=stated` + `decay_rate_override=0.001` (very slow decay until observations reinforce or contradict them).
