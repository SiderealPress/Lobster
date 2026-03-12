## Google Calendar — Dual-Mode Behavior

This skill operates in two modes depending on whether the user has connected their Google Calendar.

### How to detect which mode to use

Run this check (takes < 1 second, no network call):

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_calendar.token_store import load_token
from mcp.user_model.owner import read_owner

owner = read_owner()
OWNER_USER_ID = owner.get("owner", {}).get("telegram_chat_id", "")
token = load_token(OWNER_USER_ID)
is_authenticated = token is not None
```

---

### Mode A: Unauthenticated (no token on disk)

Generate a deep link as before. Always append to any message that mentions a concrete event with date/time:

```python
from utils.calendar import gcal_add_link_md
from datetime import datetime, timezone

link = gcal_add_link_md(
    title="Meeting with Sarah",
    start=datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc),
    # end defaults to start + 1 hour
)
# → [Add to Google Calendar](https://calendar.google.com/...)
```

---

### Mode B: Authenticated (token exists)

Use the API for read and create operations, then always include a deep link too.

#### Reading events ("what's on my calendar", "what do I have this week/today/tomorrow")

Delegate to a background subagent — API calls take > 7 seconds total:

```
send_reply(chat_id, "Checking your calendar...")
Task(prompt="...", subagent_type="general-purpose", run_in_background=true)
```

Subagent code pattern:

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_calendar.client import get_upcoming_events
from utils.calendar import gcal_add_link_md

events = get_upcoming_events(user_id=OWNER_USER_ID, days=7)
if not events:
    reply = "No upcoming events in the next 7 days."
else:
    lines = []
    for e in events:
        time_str = e.start.strftime("%a %b %-d, %-I:%M %p UTC")
        event_link = f"[{e.title}]({e.url})" if e.url else e.title
        lines.append(f"- {time_str}: {event_link}")
    reply = "Your upcoming events:\n" + "\n".join(lines)
```

#### Creating events ("add X to my calendar", "schedule X for [time]")

Delegate to a background subagent. After creating via API, always include a deep link:

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_calendar.client import create_event
from utils.calendar import gcal_add_link_md
from datetime import datetime, timezone

event = create_event(
    user_id=OWNER_USER_ID,
    title="Meeting with Sarah",
    start=datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc),
    end=datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc),
    description="",
    location="",
)

if event is not None:
    link = f"[View in Google Calendar]({event.url})" if event.url else gcal_add_link_md(
        title="Meeting with Sarah",
        start=datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc),
    )
    reply = f"Done — added \"Meeting with Sarah\" to your calendar.\n{link}"
else:
    # API failed — fall back to deep link
    link = gcal_add_link_md("Meeting with Sarah", datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc))
    reply = f"Couldn't add via API — use this link instead:\n{link}"
```

---

### Auth trigger ("connect my Google Calendar", "authenticate Google Calendar", "link Google Calendar")

Respond immediately on the main thread — no subagent needed:

```python
import sys, secrets
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_calendar.config import is_enabled
from integrations.google_calendar.oauth import generate_auth_url

if not is_enabled():
    reply = "Google Calendar isn't configured on this Lobster instance. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in config.env."
else:
    state = secrets.token_urlsafe(32)
    url = generate_auth_url(state=state)
    reply = f"Click to connect your Google Calendar:\n[Authorize Google Calendar]({url})\n\nThis link expires after a few minutes."
```

---

### Natural language patterns to recognize

| Pattern | Intent |
|---------|--------|
| "what's on my calendar" / "what do I have today/this week" | Read events |
| "add [event] to my calendar" / "schedule [event] for [time]" | Create event |
| "do I have anything on [day]" / "am I free on [day]" | Read events |
| "connect my Google Calendar" / "link Google Calendar" / "authenticate Google Calendar" | Auth flow |

---

### Graceful degradation

If the API call returns empty or None (auth failure, network error), always fall back to a deep link. Never surface token values, error codes, or credentials in Telegram messages.

---

### Deep link (always append)

Even when creating via API, append a deep link or a view link so the user can open the event in Google Calendar:

- If event was created: `[View in Google Calendar](event.url)`
- If only creating a link: `[Add to Google Calendar](gcal_add_link_md(...))`
