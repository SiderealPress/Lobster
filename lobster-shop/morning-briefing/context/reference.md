## Morning Briefing — Reference

### What it is

A structured daily digest modeled on the "AI CEO morning briefing" pattern from Polsia.
Synthesizes all active data sources into one mobile-friendly Telegram message sent at 8am PT.

---

### Briefing sections

| Section | Data source | Purpose |
|---------|-------------|---------|
| Good morning header | System date | Anchors the day |
| Today's calendar | `list_calendar_events(telegram_chat_id=owner_chat_id)` | What's happening today |
| Open work | `list_tasks(status="all")` | Pending / in-progress tasks |
| Yesterday's activity | `memory_recent(hours=24)` + `get_conversation_history(limit=30)` | What happened, what Lobster did |
| Background agents | `~/messages/config/pending-agents.json` | Any subagents still running |
| Priority focus | `get_priorities()` | Top 2 items from the ranked priority stack |
| One insight | Synthesized from all sources | Useful pattern, risk, or opportunity |

---

### Data sources — technical details

**Calendar:**
```python
# Via MCP tool
list_calendar_events(telegram_chat_id=owner_chat_id)
# Returns list of {id, title, start, end, location, description, url}
# start/end are ISO 8601 strings; convert to PT for display
```

**Tasks:**
```python
list_tasks(status="all")
# Returns all tasks with status: pending | in_progress | completed
# Only show pending and in_progress in the briefing; completed is historical
```

**Memory (yesterday's activity):**
```python
memory_recent(hours=24)
# Returns recent events from Lobster's memory store
# Types: message, task, decision, note, link
```

**Conversation history:**
```python
get_conversation_history(limit=30, direction="received")
# Returns recent incoming messages — use to identify what the owner worked on
# Filter out self-check system messages (source: "Self-Check")
```

**Pending agents:**
```python
import json
from pathlib import Path
data = json.loads(Path("~/messages/config/pending-agents.json").expanduser().read_text())
agents = data.get("agents", [])
# Each: {id, description, chat_id, started_at}
# Compute elapsed time from started_at vs now
```

**Priorities:**
```python
get_priorities()
# Returns canonical priorities.md — parse the ## High Priority section
# Top 1-2 items = "Priority focus" section
```

**Scheduled job outputs:**
```python
check_task_outputs(limit=5)
# Returns recent outputs from scheduled jobs (e.g. nightly-github-backup)
# Summarize any notable results from last 24h
```

---

### Scheduled job

| Property | Value |
|----------|-------|
| Job name | `morning-briefing` |
| Schedule | `0 15 * * *` (8:00 AM PT = 15:00 UTC) |
| Trigger | Injects `/briefing` into inbox |
| Created with | `create_scheduled_job` MCP tool |

To change the time, update the job schedule via `update_scheduled_job` and note
that the `send_time` preference is informational — the actual cron controls delivery.

---

### Customization (preferences)

Edit preferences via `set_skill_preference`:

```
set_skill_preference("morning-briefing", "tone", "detailed")
set_skill_preference("morning-briefing", "include_calendar", false)
set_skill_preference("morning-briefing", "send_time", "07:00")  # informational only
```

To disable the scheduled briefing entirely:
```
update_scheduled_job("morning-briefing", enabled=false)
```

---

### Output format example

```
Good morning, {owner_name} — Sunday, March 8

Today's calendar
• 10:00 AM — Weekly standup (30 min)
• 11:00 AM — Coffee meetup in SF

Open work
• PR #42: Add validation utility — awaiting review [pending]
• Dashboard redesign — open PR against main [pending]

Yesterday's activity
• Reviewed and merged two PRs
• Background agent completed API migration task
• New scheduled job configured for nightly backups
• Research task on caching strategy completed

Background agents
• No agents running in background.

Priority focus
• Finish API migration PR — ready for final review
• Clear stale background agent — confirm with owner

One insight
• High activity across two projects today — consider a weekly standup template
  to sync across projects before context fragments.
```

---

### Related skills

- **gcal-links** — provides the calendar API integration this briefing reads from
- **lobster-status** — system health; complement to briefing for operational awareness
