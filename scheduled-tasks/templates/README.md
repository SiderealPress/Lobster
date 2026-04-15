# Scheduling Pattern Templates

Templates for the five standard Lobster scheduling patterns. Copy the appropriate
template, fill in the `YOUR_*` placeholders, and wire it into cron.

---

## Choosing a Pattern

```
Is this a polling job?
│
├─ YES: Does it check an external source for new data?
│   │
│   ├─ YES: Is the source a REST API with a ?since= timestamp parameter?
│   │   ├─ YES → api-poll.sh.template     (REST API, bearer token, JSON items)
│   │   └─ NO  → poll-with-state.sh.template  (generic source, custom check logic)
│   │
│   └─ NO, or cadence is slow and LLM decides what's new:
│       └─ poll-basic.sh.template         (reachability check only, no cursor)
│
└─ NO: Does it invoke Claude?
    ├─ YES → reminder.sh.template         (always dispatch, LLM does the work)
    └─ NO  → local-code.sh.template       (no dispatch, no LLM, self-contained)
```

---

## Template Summary

| Template | Polling | State | LLM | When to use |
|---|---|---|---|---|
| `poll-with-state.sh.template` | Yes | Yes | Yes (Layer 2) | Frequent polling; you own the cursor |
| `api-poll.sh.template` | Yes | Yes | Yes (Layer 2) | REST API with `?since=` timestamp support |
| `poll-basic.sh.template` | Yes | No | Yes (Layer 2) | Slow cadence; LLM handles no-op detection |
| `reminder.sh.template` | No | No | Yes (Layer 2) | Scheduled jobs that always do work |
| `local-code.sh.template` | No | No | No | Self-contained maintenance tasks |

---

## How to Instantiate a Template

1. Copy the template to `scheduled-tasks/`:
   ```bash
   cp scheduled-tasks/templates/api-poll.sh.template \
      scheduled-tasks/YOUR_JOB_NAME-check.sh
   chmod +x scheduled-tasks/YOUR_JOB_NAME-check.sh
   ```

2. Replace every `YOUR_*` placeholder with your actual values.

3. For polling jobs, create the task file the subagent will receive:
   ```
   scheduled-jobs/tasks/YOUR_JOB_NAME.md
   ```
   This file is passed verbatim to the LLM subagent by `dispatch-job.sh`.

4. Register the job with the Lobster scheduler (via MCP `create_scheduled_job`)
   or add a cron entry to `scripts/sync-crontab.sh`.

5. Test the script manually before wiring to cron:
   ```bash
   bash -x scheduled-tasks/YOUR_JOB_NAME-check.sh
   ```

---

## File Conventions

### State files
Track the last-seen cursor (timestamp, ID, hash) so the script can drop no-ops.

```
~/lobster-workspace/data/<job-name>-state.json
```

Format (adapt fields as needed):
```json
{
  "last_seen_ts": "2026-01-15T10:30:00Z",
  "last_seen_id": "msg-12345"
}
```

**Who writes the state file:** the Layer 2 subagent, after successful processing.
Never advance the cursor in the Layer 1 script — only the subagent knows whether
processing succeeded.

### Token files
Store auth tokens as single-line plain text.

```
~/lobster-workspace/data/<job-name>-token.txt
```

These files live outside the repo and are never committed.

### Log files
Each script run writes a timestamped log:

```
~/lobster-workspace/scheduled-jobs/logs/<job-name>-<YYYYMMDD-HHMMSS>.log
```

Polling pre-check scripts append `-precheck` to the log name to distinguish them
from the subagent's own log:

```
~/lobster-workspace/scheduled-jobs/logs/<job-name>-precheck-<YYYYMMDD-HHMMSS>.log
```

---

## The Two-Layer Model

Templates enforce the architecture described in `scheduled-tasks/README.md`:

**Layer 1 (scripts, this directory):** Lightweight checks. No LLM. Cost: ~0.
**Layer 2 (LLM subagent):** Reasoning, formatting, actions. Only spawned when Layer 1 finds new data.

The key invariant: a polling script that fires every 2 minutes and finds nothing
new costs nothing. LLM invocations are reserved for cases where there is something
to reason about.

---

## Real Examples

| Script | Template used | Notes |
|---|---|---|
| `bot-talk-check-dispatch.sh` | `poll-with-state.sh` | Polls bot-talk API; state in `bot-talk-state.json` |
| `lobstertalk-incoming-check.sh` | `api-poll.sh` | REST API, `?recipient=` + `?since=` params |
| `dispatch-job.sh` | (not a template; used by all templates) | Writes to inbox; called by Layer 1 scripts |
| `post-reminder.sh` | (not a template; purpose-built) | Self-reminders with built-in dedup |
