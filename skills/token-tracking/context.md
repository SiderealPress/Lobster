## Token Usage Tracking

Token usage is logged automatically for every subagent task and dispatcher session.

**Log files:**
- `~/lobster-workspace/logs/token-usage.jsonl` -- per-subagent records (task_id, agent_id, model, tokens, turns)
- `~/lobster-workspace/logs/token-daily.jsonl` -- daily rollups (dispatcher + subagent totals)
- `~/lobster-workspace/logs/token-weekly.jsonl` -- rolling 7-day window summaries

**To answer "how many tokens did we use?":**
- Today: read token-daily.jsonl, find today's entry (field: `date`)
- This week: read the latest entry in token-weekly.jsonl (`week_ending` field)
- Per task: query token-usage.jsonl by `task_id`
- High-context sessions: check `context_fills` array in today's daily entry

**Limits and thresholds:**
- Per-session context window: 200,000 tokens
- High-context threshold: 160,000 tokens (80%) -- sessions above this appear in `context_fills`
- Daily budget reference: ~100M tokens (based on observed usage; no hard subscription cap)
- The existing `context-monitor.log` tracks per-tool-call context fill % in real time

**Token record fields:**
- `input_tokens`: fresh input tokens (not from cache)
- `cache_creation_tokens`: tokens written to prompt cache
- `cache_read_tokens`: tokens served from prompt cache (cheap)
- `output_tokens`: generated output tokens
- `total` in daily: sum of all of the above for both dispatcher and subagents

**Nightly rollup:** Runs at 3:05 AM UTC (5 minutes after nightly-consolidation).
To run manually: `uv run /home/lobster/lobster/scripts/nightly-token-rollup.py`
