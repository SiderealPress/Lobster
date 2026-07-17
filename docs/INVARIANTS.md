# Invariants Registry

This file maps each **cross-cutting invariant** — a rule that must be kept true across multiple files — to the full list of files that encode it. Unlike `docs/engineering-lessons-learned.md` (which describes bug patterns after the fact), this registry is forward-looking: it lets an agent editing one call site discover every other call site it must keep in sync, *before* introducing a new bug.

If you add a new call site for an existing invariant, or discover a new cross-cutting invariant, update this file in the same PR.

---

## Dispatcher-Exclusion Invariant

**Invariant:** A session row with `agent_type='dispatcher'` must never be treated as a subagent by any code that lists, counts, or reconciles `agent_sessions` rows for dead/stale/completed/pending status.

**Files that must stay in sync (5):**

1. `src/agents/session_store.py` — `cleanup_stale_running_sessions()` (SQL filter: `AND COALESCE(agent_type, '') != 'dispatcher'`)
2. `src/agents/session_store.py` — `get_unnotified_completed()` (SQL filter, same pattern)
3. `src/mcp/inbox_server.py` — `reconcile_agent_sessions()` periodic loop (Python skip: `if (session.get("agent_type") or "") == "dispatcher": continue`)
4. `src/mcp/inbox_server.py` — `_startup_sweep()` (Python skip, mirrors #3)
5. `scripts/periodic-self-check.sh` — the `PENDING_COUNT` raw `sqlite3` query (bypasses `session_store.py` entirely, so it needs its own SQL filter)

**Related lessons-learned entry:** [`Dispatcher-Exclusion Bug: The Dispatcher's Own Session Row Miscounted as a Dead/Pending Subagent`](engineering-lessons-learned.md#dispatcher-exclusion-bug-the-dispatchers-own-session-row-miscounted-as-a-deadpending-subagent) — documents that this exact bug was independently found and fixed four times (Issue #781, PR #2099 x2, PR #2103) because nothing recorded all affected call sites in one place.

**Note:** A consolidation refactor to replace these five hand-copied filters with a single shared helper is tracked separately (Slice D / BIS-723) and has not landed as of this writing. Until it does, any new agent-session query must apply the filter/skip pattern manually at all five sites above.

---

## jq `--arg` JSON-Escaping Invariant

**Invariant:** Any shell script that constructs a JSON inbox message from a variable that may contain newlines or special characters must build it via `jq -n --arg`/`--argjson`, never via heredoc variable expansion — otherwise the result can be invalid JSON that tight-loops `wait_for_messages`.

**Files that must stay in sync (4):**

1. `scripts/periodic-self-check.sh` — `jq -n` JSON construction (around line 138); highest risk, since `$SELF_CHECK_TEXT` concatenates agent summaries and scan output that can contain newlines
2. `scripts/alert.sh` — `jq -n` JSON construction (around line 37); `$message` is caller-supplied and can contain anything
3. `scripts/check-agent-outputs.sh` — `jq -n` JSON construction (around line 118); `$SAFE_ID` is pre-sanitized but kept consistent with the pattern
4. `scripts/daily-update-check.sh` — two `jq -n` JSON construction sites (around lines 31 and 52), for the git-behind-count and version-check messages respectively

**Related lessons-learned entry:** none exists yet. `docs/engineering-lessons-learned.md` currently documents the dispatcher-exclusion bug and several other patterns, but has no dedicated entry for the heredoc-vs-`jq --arg` JSON-escaping bug, despite it being fixed twice at the same four call sites (PR #2016, then re-fixed/re-landed in PR #2031 — both merged, both referencing issue #2004). If this bug recurs, add an entry there and link it from here.

**History:** Both PR #2016 (merged 2026-05-08) and PR #2031 (merged 2026-05-10) replaced heredoc-based JSON construction with `jq -n --arg`/`--argjson` across these same four scripts, citing the 2026-04-24/25 WFM tight-loop restart storm (originally fixed for `daily-health-check.sh` in PR #1808) as the root-cause precedent.
