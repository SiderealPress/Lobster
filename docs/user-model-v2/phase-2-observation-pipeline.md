# Phase 2: Strengthened Observation Pipeline

*Part of User Model v2 — see [PLAN.md](PLAN.md)*

---

## Goal

Capture richer behavioral signals from every user interaction. The v1 pipeline extracts signals from message text alone. v2 adds behavioral metadata (latency, length, follow-up patterns) and improves the quality of heuristic extraction.

## Signal Inventory

### Existing Signals (v1)
| Signal | Type | Method | Confidence |
|--------|------|--------|------------|
| Sentiment (pos/neg) | text | keyword matching | 0.5–0.9 |
| Energy level | text | urgency keywords + `!` | 0.7 |
| Correction | text | regex patterns | 0.75 |
| Preference statement | text | regex patterns | 0.8 |
| Topic | text | keyword clusters | 0.6 |
| Timing (time-of-day) | metadata | hour bucketing | 1.0 |

### New Signals (v2)
| Signal | Type | Method | Confidence |
|--------|------|--------|------------|
| Response latency | behavioral | `latency_ms` param | 0.85 |
| Message length | behavioral | character count | 0.75 |
| Follow-up detection | behavioral | topic continuity + time delta | 0.8 |
| Topic shift | behavioral | topic divergence between messages | 0.7 |
| Engagement score | derived | length + latency composite | 0.7 |
| Activity rhythm | derived | hour + day + count update | 1.0 |

## Enhanced `observe_message()` Signature

```python
def observe_message(
    conn: sqlite3.Connection,
    message_text: str,
    message_id: str,
    context: str = "",
    message_ts: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    # NEW in v2:
    latency_ms: int | None = None,           # ms since last Lobster reply
    reply_length: int | None = None,         # len(message_text) if not provided
    previous_topic: str | None = None,       # topic of previous message for shift detection
    previous_message_ts: datetime | None = None,  # for follow-up detection
) -> list[str]:
```

## New Signal Extraction Functions

### `extract_latency_signal`
```python
def extract_latency_signal(
    message_id: str,
    latency_ms: int,
    context: str = "",
) -> Observation | None:
    """
    Classify response latency as engagement signal.

    Latency categories:
    - < 30s: immediate (very high interest/urgency)
    - 30s–2m: quick (engaged)
    - 2m–10m: normal
    - 10m–60m: slow (lower engagement or context switch)
    - > 60m: very slow (likely separate session, not a follow-up signal)

    Only returns a signal for immediate/quick (<2m) or very slow (>60m).
    Normal latency is not informative.
    """
```

**Signal content format:** `"immediate"`, `"quick"`, `"slow"`, `"very_slow"`
**Signal type:** `TIMING` (reuses existing enum)
**Why it matters:** Quick replies to a message = high interest in that topic. Slow replies = lower priority or high cognitive load.

### `extract_length_signal`
```python
def extract_length_signal(
    message_id: str,
    message_text: str,
    context: str = "",
) -> Observation | None:
    """
    Message length as engagement proxy.

    Length categories (in characters):
    - < 20: very short (disengaged, dismissive, or mobile)
    - 20–100: short (normal mobile message)
    - 100–500: medium (engaged, thinking it through)
    - > 500: long (deeply engaged, nuanced topic)

    Only records signal at extremes (< 30 or > 300).
    """
```

**Signal content format:** `"very_short"`, `"long"`, `"very_long"`
**Signal type:** New `ObservationSignalType.LENGTH`

### `extract_followup_signal`
```python
def extract_followup_signal(
    message_id: str,
    message_text: str,
    previous_topic: str | None,
    previous_message_ts: datetime | None,
    current_ts: datetime,
    context: str = "",
) -> Observation | None:
    """
    Detect follow-up messages: same topic continuation within 5 minutes.

    A follow-up is strong evidence that the topic matters to Drew.
    Three consecutive follow-ups on a topic should reinforce a preference node.
    """
```

**Signal content format:** `"followup:{topic}"` (e.g., `"followup:coding"`)
**Signal type:** `TOPIC` (extends existing enum semantics)

### `extract_topic_shift_signal`
```python
def extract_topic_shift_signal(
    message_id: str,
    current_topic: str | None,
    previous_topic: str | None,
    context: str = "",
) -> Observation | None:
    """
    Detect abrupt topic shifts — potential frustration or context overload signal.

    Only records when:
    - Both topics are non-None
    - They are different
    - The shift happens within a short time window (< 10 minutes)
    """
```

**Signal content format:** `"shift:{prev_topic}→{new_topic}"`
**Signal type:** `TOPIC`

### `update_activity_rhythm_from_message`
```python
def update_activity_rhythm_from_message(
    conn: sqlite3.Connection,
    message_ts: datetime,
    message_length: int,
    latency_ms: int | None = None,
) -> None:
    """
    Update the activity rhythm table for this message's hour and day.
    Called on every observed message — fast, O(1) per call.
    """
```

## Updated `ObservationSignalType` Enum

```python
class ObservationSignalType(str, Enum):
    TIMING = "timing"
    SENTIMENT = "sentiment"
    TOPIC = "topic"
    ENERGY = "energy"
    PREFERENCE = "preference"
    CORRECTION = "correction"
    EMOTION = "emotion"
    LENGTH = "length"       # NEW: message length signal
    LATENCY = "latency"     # NEW: response latency signal (replaces overloaded TIMING)
    ENGAGEMENT = "engagement"  # NEW: derived composite engagement score
```

## Seed Bootstrap: Owner.toml Values Ingestion

First-run bootstrap is critical — the model starts empty. `seed.py` handles ingestion of stated values from `owner.toml`.

### `owner.toml` Extended Format

```toml
[owner]
name = "Drew"
telegram_chat_id = "OWNER_CHAT_ID_PLACEHOLDER"

[values]
autonomy = "I strongly value independence and making my own decisions. Avoid asking for permission."
technical_depth = "I prefer understanding systems deeply rather than using them as black boxes."
craftsmanship = "I care about the quality and elegance of what I build — correctness over speed."
directness = "I value directness and dislike hedging or excessive caveats."

[preferences]
response_length = "concise — short answers unless I ask for detail"
code_style = "functional where possible; pure functions over classes"
notifications = "async preferred; don't interrupt unless urgent"

[constraints]
no_emojis = "Never use emojis in responses unless I explicitly ask"
no_markdown_bullet_spam = "Don't use excessive bullet points; prefer prose for explanations"
```

### `seed.py` Functions

```python
def seed_from_owner_toml(
    conn: sqlite3.Connection,
    owner_file: Path | None = None,
) -> dict[str, int]:
    """
    Ingest values, preferences, and constraints from owner.toml into the preference graph.
    Returns dict of counts: {'values': N, 'preferences': N, 'constraints': N}.

    Seeded nodes have:
    - source = NodeSource.STATED
    - seed_source = 'owner_toml'
    - confidence = 1.0
    - decay_rate_override = 0.001 (very slow decay)
    - flexibility: values=SOFT, constraints=HARD
    """
```

```python
def is_seeded(conn: sqlite3.Connection) -> bool:
    """Return True if at least one node with seed_source='owner_toml' exists."""
```

```python
def reseed_if_needed(conn: sqlite3.Connection, owner_file: Path | None = None) -> bool:
    """
    Run seed_from_owner_toml if not yet seeded. Returns True if seeding occurred.
    Called on every startup — idempotent.
    """
```

## Observation-to-Preference Feedback Loop

The key gap in v1: observations accumulate but never update the preference graph. v2 adds a feedback loop during nightly consolidation.

### Rules for Observation Promotion

In `inference.py`, during nightly consolidation after decay + contradiction detection:

```
For each unprocessed observation:
  If signal_type = PREFERENCE and confidence > 0.8 and source_specificity = 'explicit':
    → Create/reinforce a preference node (NodeSource.STATED)
    → Evidence: the observation content

  If signal_type = TOPIC and is_followup = True:
    → Reinforce any existing preference node for that topic
    → Or create a new PREFERENCE node: "High interest in {topic}"

  If signal_type = CORRECTION:
    → Flag the observation for manual review (add to blind_spots as surfaced=False)
    → Don't auto-create nodes from corrections — require explicit model_correct call

  If signal_type = SENTIMENT and confidence > 0.75:
    → Record emotional state (already done in v1 — carry forward)

  If signal_type = LENGTH and content = 'very_long':
    → Reinforce any preference node for the current topic (topic engagement signal)
```

This keeps the feedback loop conservative: only high-confidence, explicit signals create nodes automatically. Everything else waits for the user to call `model_correct`.

## Test Plan

- [ ] `extract_latency_signal` correctly categorizes: 10s→immediate, 90s→quick, 300s→None, 600s→slow
- [ ] `extract_length_signal` returns None for 50-char messages, signal for 600-char messages
- [ ] `extract_followup_signal` detects same-topic messages within 5 minutes
- [ ] `extract_topic_shift_signal` detects topic changes within 10 minutes
- [ ] `update_activity_rhythm_from_message` correctly upserts (hour, day) pairs
- [ ] Seed bootstrap creates correct node types from owner.toml sections
- [ ] Seeded nodes have decay_rate_override=0.001
- [ ] `is_seeded()` correctly returns True/False based on DB state
- [ ] Feedback loop promotes high-confidence preference observations to preference nodes
- [ ] Feedback loop does NOT create nodes from corrections (those require model_correct)
- [ ] All new observations have `source_specificity` correctly set
