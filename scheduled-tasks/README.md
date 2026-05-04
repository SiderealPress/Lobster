# scheduled-tasks/

Scripts in this directory are called **only by cron**. They are never invoked by
agents directly.

Agents interact with scheduling via MCP tools (`create_scheduled_job`,
`update_scheduled_job`). Those tools manage `jobs.json` and the task files under
`scheduled-jobs/tasks/`. This directory holds the shell scripts that cron calls
to do lightweight pre-checks before (optionally) writing to the inbox.

---

## The Two-Layer Model

```
Cron
 │
 └─ Layer 1: shell script (this directory)
     │  Lightweight check — HTTP call, file stat, API poll
     │  If nothing new: log, exit 0 (no LLM cost)
     │  If new data: call dispatch-job.sh → write to inbox
     │
     └─ Layer 2: LLM subagent (spawned by dispatcher)
         Receives pre-filtered payload
         Does reasoning, formatting, notifications, GitHub actions
         Updates state file on success
```

**Key invariant:** No LLM cost when nothing changed. A polling script that fires
every 2 minutes and finds nothing new exits 0 and costs nothing.

For full architecture documentation, see issue #1059.

---

## Scripts in This Directory

| Script | Pattern | Description |
|---|---|---|
| `dispatch-job.sh` | Non-polling dispatcher | Writes a `scheduled_reminder` to the inbox; called by all Layer 1 scripts and directly by cron for reminder jobs |
| `bot-talk-check-dispatch.sh` | Poll with state | Polls bot-talk API; skips dispatch if no new messages since last cursor |
| `lobstertalk-incoming-check.sh` | API poll | Polls bot-talk API for messages addressed to this Lobster instance; skips dispatch if none |
| `sync-crontab.sh` | Local code | Syncs the crontab from a config file; no LLM |
| `export-logs.py` | Local code | Exports log data; no LLM |

### `dispatch-job.sh`

The core dispatcher. Reads the task file for a job, writes a `scheduled_reminder`
message to `~/messages/inbox/`, and updates `jobs.json` with the last-run
timestamp. All Layer 1 polling scripts `exec` into this when they find new data.

For non-polling jobs (reminders, digests, mode-switches), cron calls this
directly:
```
0 8 * * 1-5  ~/lobster/scheduled-tasks/dispatch-job.sh morning-briefing
```

### `post-reminder.sh` (in `scripts/`)

Purpose-built for **self-reminders** created by the dispatcher itself (e.g.,
"remind me in 30 minutes"). Has built-in dedup: checks `inbox/` and
`processing/` for an existing unprocessed message of the same type before
writing. Safe to call from cron or from `at`.

---

## Adding a New Job

1. Pick a pattern from `templates/README.md`.
2. Copy the appropriate template, fill in placeholders, save to this directory.
3. Create `~/lobster-workspace/scheduled-jobs/tasks/<job-name>.md` — this is
   the task brief passed to the LLM subagent.
4. Register the job via the `create_scheduled_job` MCP tool (this updates
   `jobs.json` and the crontab) **or** add an entry to `sync-crontab.sh`.

---

## What Belongs Here vs. Elsewhere

| If you need to... | Do this |
|---|---|
| Poll an external source on a schedule | Create a Layer 1 script here |
| Dispatch a non-polling job on a schedule | Call `dispatch-job.sh` from cron |
| Schedule a self-reminder from within the dispatcher | Use `post-reminder.sh` |
| Run something with no LLM involvement | Write a `local-code` script here |
| Register or configure a scheduled job | Use the `create_scheduled_job` MCP tool |
| See patterns and conventions | Read `templates/README.md` |
