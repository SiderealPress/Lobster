# How to Migrate a Scheduled Job to the New Architecture

This guide covers converting a Claude-mediated (Kind 2) scheduled job to a
code-first (Kind 1) polling script, and registering new jobs the right way
using systemd timers.

**Written against the live implementations in `#1081` (lobstertalk-unified) and
`#1082` (lobstertalk-incoming-handler).** If the examples here contradict what
you see in those files, the code is authoritative.

---

## The Three Job Kinds

| Kind | Trigger | Runner | When to use |
|------|---------|--------|-------------|
| **Kind 1** | systemd timer → code | Python/shell script directly (no Claude) | Deterministic algorithm, fixed API, no open-ended reasoning. Examples: email poller, API watcher, git diff check. |
| **Kind 2** | systemd timer → Claude | `dispatch-job.sh` → inbox → LLM subagent | Requires LLM judgment: formatting, routing decisions, multi-step reasoning. Examples: kanban watcher, SSH message router. |
| **Kind 3** | External webhook / event | Direct inbox write | Triggered by an external system pushing a message in, not by a timer. |

**The new default for new jobs is Kind 1 or Kind 2 backed by a systemd timer.**
The old cron + `jobs.json` + `sync-crontab.sh` path is deprecated (see `dispatch-job.sh`
header and issue #1083). Do not create new `# LOBSTER-SCHEDULED` crontab entries.

---

## When to Migrate a Claude Task to a Code-First Script

Migrate from Kind 2 to Kind 1 when **all** of the following are true:

- The job follows a deterministic algorithm: same input → same output, every time.
- The job calls a fixed API with a known response schema (no need to parse free text).
- The job's decisions can be written as explicit `if/else` logic, not prose instructions.
- The job does not require multi-step reasoning, tone adjustment, or contextual judgment.
- The job produces structured output (inbox message, log entry) rather than a narrative reply.

**Do not migrate** if the job needs to decide *how* to present information, which of
several actions to take, or when to escalate to a human. Keep it as Kind 2.

Examples that are good Kind 1 candidates:
- "Poll Linear, write inbox message if any new issues assigned to me since last check"
- "Check if a GitHub PR has new review comments since last run"
- "Read a file's mtime; if changed, write a notification"

Examples that should stay Kind 2:
- "Read the kanban board and write a concise summary with action items"
- "Check SSH messages and route them with appropriate context to the right Lobster inbox"

---

## Step-by-Step: Converting a Kind 2 Job to Kind 1

### 1. Write the Python poller script

Create `~/lobster/scheduled-tasks/<job-name>.py`.

**Anatomy of a Kind 1 poller:**

```
constants and configuration
    ↓
_load_state()         # read last watermark from state file
    ↓
_poll_api()           # call external service, return new items (pure function)
    ↓
_delta()              # compute what changed since last watermark (pure function)
    ↓
if delta is empty:
    exit 0            # no LLM subagent spawned, zero cost
else:
    _write_inbox()    # write a structured message to ~/messages/inbox/
    _write_state()    # advance watermark atomically
    exit 0
```

Key invariants:
- **Never advance the watermark before writing to the inbox.** If the process
  crashes after advancing but before writing, you'll miss events permanently.
- **Never invoke `claude` or the MCP server directly.** All LLM work is done
  by the dispatcher when it reads the inbox message you wrote.
- **Exit 0 always** (even on error — let the job log it and retry next cycle).
  Non-zero exits cause systemd to mark the job as failed, which can suppress
  future runs.
- **Use atomic writes.** Write to a `.tmp` file, then `os.replace()`. This
  prevents partial reads if the process is interrupted.

See `scheduled-tasks/lobstertalk_unified.py` for a complete production example.

### 2. State file location and schema

Store state in `~/lobster-workspace/data/<job-name>-state.json`.

Minimum schema:

```json
{
  "last_check_ts": "2026-01-01T12:00:00+00:00"
}
```

For cursor-based APIs (message IDs, page tokens):

```json
{
  "last_seen_id": "msg_12345",
  "last_check_ts": "2026-01-01T12:00:00+00:00"
}
```

Read it with a helper that returns defaults on first run:

```python
def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_check_ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_check_ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
```

Write atomically:

```python
def _write_state(state: dict) -> None:
    tmp = Path(str(STATE_FILE) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)
```

### 3. Writing inbox messages

Write to `~/messages/inbox/<epoch_ms>_<job-name>.json`:

```python
def _write_inbox_message(items: list[dict], state: dict) -> None:
    epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    msg_id = f"{epoch_ms}_{JOB_NAME}"
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "scheduled_reminder",
        "chat_id": ADMIN_CHAT_ID,   # from env: LOBSTER_ADMIN_CHAT_ID
        "user_id": ADMIN_CHAT_ID,
        "username": "lobster-cron",
        "user_name": "Cron",
        "text": f"[{JOB_NAME}] {len(items)} new item(s) since {state.get('last_check_ts')}",
        "job_name": JOB_NAME,
        "items": items,             # structured payload for the dispatcher
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    tmp = INBOX_DIR / f"{msg_id}.tmp"
    out = INBOX_DIR / f"{msg_id}.json"
    tmp.write_text(json.dumps(msg, ensure_ascii=False, indent=2))
    tmp.replace(out)
```

The dispatcher reads `job_name` and routes the message to a subagent configured
for that job. The `items` payload is available in the task context.

### 4. Creating the systemd timer

Register the job via the `create_scheduled_job` MCP tool from within a Claude session:

```
create_scheduled_job(
    name="my-job",
    schedule="*/15 * * * *",    # standard cron syntax; the tool converts to systemd OnCalendar=
    context="<path to task file or inline instructions>"
)
```

For Kind 1 jobs where the script runs directly (no task file / no LLM), you
can still use `create_scheduled_job` — just set the `context` to a note explaining
the job exists. The underlying systemd service will call your script's full path.

To create the systemd unit manually (for Kind 1 scripts that call no LLM):

```bash
# /etc/systemd/system/lobster-my-job.service
[Unit]
Description=My job description
# LOBSTER-MANAGED

[Service]
Type=oneshot
User=lobster
ExecStart=/home/lobster/lobster/scheduled-tasks/my-job.py

# /etc/systemd/system/lobster-my-job.timer
[Unit]
Description=My job description
# LOBSTER-MANAGED

[Timer]
OnCalendar=*:0/15    # every 15 minutes
Persistent=true

[Install]
WantedBy=timers.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lobster-my-job.timer
```

### 5. Tombstoning the jobs.json entry

If the job was previously registered in `~/lobster-workspace/scheduled-jobs/jobs.json`,
disable it there to prevent any legacy path from re-firing:

```bash
cd ~/lobster-workspace/scheduled-jobs
jq '.jobs["my-job"].enabled = false | .jobs["my-job"].tombstoned = "migrated to systemd timer — see #1083"' \
  jobs.json > jobs.json.tmp && mv jobs.json.tmp jobs.json
```

Also remove any `# LOBSTER-SCHEDULED` crontab entry for the job. With the new
architecture, upgrade.sh Migration 70 removes all such entries automatically.

### 6. Testing checklist

Before shipping:

- [ ] Script runs standalone: `uv run ~/lobster/scheduled-tasks/my-job.py` exits 0
- [ ] On first run (no state file), script exits 0 without crashing
- [ ] On second run with no new data, script exits 0 and writes nothing to inbox
- [ ] When new data exists, inbox message appears at `~/messages/inbox/` with correct schema
- [ ] State file advances after inbox write, not before
- [ ] State file uses atomic write (no partial reads during crash)
- [ ] No direct `claude` invocations anywhere in the script
- [ ] `systemctl status lobster-my-job.timer` shows `active (waiting)`

---

## Template: `scheduled-tasks/templates/poller.py.template`

A copy-paste starting point for new Kind 1 Python pollers. See that file for the
full annotated template.

Quick summary of what the template provides:
- Constants block (JOB_NAME, ADMIN_CHAT_ID, STATE_FILE, INBOX_DIR)
- `_load_state()` and `_write_state()` with safe defaults
- `_poll()` stub to fill in with your API call
- `_write_inbox_message()` with atomic write
- `main()` wiring it all together with try/except guard
- Exit codes: always 0 (log errors, don't fail the systemd unit)

---

## Common Mistakes

### 1. Advancing the watermark before writing to the inbox

```python
# WRONG — if _write_inbox_message crashes, events are lost forever
_write_state({"last_check_ts": now})
_write_inbox_message(new_items)

# CORRECT — advance only after successful write
_write_inbox_message(new_items)
_write_state({"last_check_ts": now})
```

### 2. Forgetting `last_check_ts` on first run

If `STATE_FILE` doesn't exist and your code does `state["last_check_ts"]` without
a default, you'll get a `KeyError` on first run. Always use `_load_state()` with
defaults.

### 3. Advancing the watermark in the script instead of the subagent

For Kind 2 jobs (LLM-mediated), the subagent must update the state file after
successful processing. The pre-check script must not advance the watermark —
it only reads it. If the script advances the cursor and the subagent never runs
(inbox full, dispatcher busy), you permanently skip those events.

For Kind 1 jobs (code-only), the script itself advances the watermark after
writing to the inbox. This is correct because there is no subagent to do it.

### 4. Non-zero exit on handled errors

If your API is unreachable or returns 500, log the error and `sys.exit(0)`.
A non-zero exit causes systemd to mark the job as failed, which may suppress
future runs or trigger admin alerts for a transient issue.

### 5. Creating a new `# LOBSTER-SCHEDULED` crontab entry

The `# LOBSTER-SCHEDULED` cron layer is deprecated (issue #1083). All new
jobs must use systemd timers via `create_scheduled_job` MCP tool. Do not
write new entries to the crontab manually.

### 6. Calling the MCP server or Claude directly from a scheduled script

Scheduled scripts run outside the Claude session. They cannot call MCP tools.
All LLM work must flow through the inbox: write a `scheduled_reminder` message,
and let the dispatcher spawn a subagent to handle it.
