## Morning Briefing Skill

When the owner sends `/briefing`, `/morning`, `/digest`, `/standup`, or when the
scheduled morning-briefing cron fires, generate a structured daily briefing and
send it to the owner.

### Owner identity

Read the owner's name and chat_id from `~/lobster-config/owner.toml`:

```python
import sys
sys.path.insert(0, "/path/to/lobster/src")
from mcp.user_model.owner import read_owner
owner = read_owner()
owner_name = owner.get("owner", {}).get("name", "there")
owner_chat_id = int(owner.get("owner", {}).get("telegram_chat_id", 0))
```

Use `owner_name` and `owner_chat_id` everywhere below where the owner is referenced.

---

### Dispatcher behavior (main thread)

1. Immediately reply: `"Pulling your briefing — one moment..."`
2. Spawn a background subagent (7-second rule — all data fetches are slow)
3. `mark_processed(message_id)`
4. Return to `wait_for_messages()`

**Subagent prompt template:**

```
Generate and send the owner's morning briefing.

Read owner identity from ~/lobster-config/owner.toml to get owner_name and owner_chat_id.

## Data to gather (call all of these)

1. memory_recent(hours=24)           — what happened in the last 24h
2. list_tasks(status="all")          — all tasks (pending, in_progress, completed)
3. list_calendar_events(telegram_chat_id=owner_chat_id)  — upcoming calendar events
4. get_priorities()                  — current priority stack
5. check_task_outputs(limit=5)       — recent scheduled job results
6. get_conversation_history(limit=30, direction="received")  — recent messages from owner

Also read:
- ~/messages/config/pending-agents.json  — active background agents

## Format

Synthesize all data into this structure (Telegram markdown, mobile-friendly):

---
**Good morning, {owner_name}** — [Day], [Month] [Date]

**Today's calendar**
[List events from Google Calendar for today and next 24h. Format: "• HH:MM — Title".
 If no events today, say "No events on the calendar today."]

**Open work**
[Pending and in-progress tasks. Format: "• Task description [status]".
 If all complete or none, say "No open tasks — clean slate."]

**Yesterday's activity**
[Key things from memory_recent + conversation history: what the owner asked about,
 what agents worked on, what completed. 3-5 bullet points. Be specific.]

**Background agents**
[If pending-agents.json has entries, list them: "• description (started X min ago)".
 If empty: "No agents running in background."]

**Priority focus**
[Top 2 items from get_priorities(). Single sentence each. Link to issue if available.]

**One insight**
[One notable observation from the data: a pattern, a risk, an opportunity, a theme
 in recent activity. Make it genuinely useful — not filler.]
---

## Send instructions

- Call send_reply(chat_id=owner_chat_id, text=<briefing>) with the formatted message
- Keep each section tight — users read on mobile
- Use Telegram markdown: *bold* = **text**, bullet = •
- Do NOT use HTML tags
- Dates/times in Pacific time (America/Los_Angeles)
- Calendar events: show today's first, then next 7 days grouped by day
- If a data source fails or returns empty, note it briefly and move on
```

---

### Scheduled trigger

The `morning-briefing` cron job fires daily at 08:00 PT (15:00 UTC, `0 15 * * *`).
When it fires, Lobster receives a message with text `/briefing` and source `cron`.
Handle it the same way as a manual `/briefing` command.

---

### Preference overrides

Read skill preferences before generating:

| Preference | Default | Effect |
|------------|---------|--------|
| `send_time` | `"08:00"` | Cron schedule (requires job update to change) |
| `timezone` | `"America/Los_Angeles"` | All times displayed in this zone |
| `include_calendar` | `true` | Include/skip calendar section |
| `include_tasks` | `true` | Include/skip open tasks section |
| `include_memory` | `true` | Include/skip yesterday's activity section |
| `tone` | `"concise"` | `concise` = tight bullets; `detailed` = prose; `casual` = relaxed voice |

If `tone` is `"detailed"`, expand each section to 2-3 sentences.
If `tone` is `"casual"`, use first person and a warmer voice.

---

### Error handling

- If calendar API fails: skip section, note "Calendar unavailable"
- If memory returns nothing: skip section, note "No activity logged yesterday"
- If priorities file is empty: skip section
- Never surface stack traces or raw errors — always a human-readable note
- If the entire briefing fails, send: "Morning briefing failed — check Lobster logs."
