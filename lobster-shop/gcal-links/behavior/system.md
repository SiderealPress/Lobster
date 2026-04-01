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

When the user explicitly wants to connect their Google Calendar, use `generate_consent_link()` to
send them a one-time myownlobster.ai OAuth URL. This replaces the old direct OAuth URL approach.

Respond immediately on the main thread — no subagent needed:

```python
import sys
import os
import logging
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from utils.calendar import gcal_add_link_md
from datetime import datetime, timezone

log = logging.getLogger(__name__)

try:
    from integrations.google_auth.consent import generate_consent_link
    url = generate_consent_link("calendar")
    reply = (
        "To connect your Google Calendar, tap this link (expires in 30 minutes):\n"
        f"[Connect Google Calendar]({url})\n\n"
        "After connecting, I'll be able to read and create calendar events for you."
    )
except Exception as exc:
    # Graceful fallback: generate_consent_link raises if env vars are missing
    # or if the myownlobster.ai endpoint is unreachable. Fall back to a deep link
    # so the user still gets a useful response.
    log.warning(
        "generate_consent_link('calendar') failed — falling back to deep link: %s",
        exc,
    )
    from utils.calendar import gcal_add_link_md
    from datetime import datetime, timezone
    link = gcal_add_link_md(
        title="My Event",
        start=datetime.now(tz=timezone.utc),
    )
    reply = (
        "I couldn't generate a connection link right now. "
        "You can still add individual events to your calendar using this link:\n"
        f"{link}"
    )
```

> **Note:** Deep link behavior (Mode A) for individual event creation remains available and is
> not affected by this flow. If the user just wants to add a single event without connecting their
> calendar, generate the deep link as usual. Only use `generate_consent_link()` when the user
> explicitly asks to **connect** their calendar.

---

### Natural language patterns to recognize

| Pattern | Intent |
|---------|--------|
| "what's on my calendar" / "what do I have today/this week" | Read events |
| "add [event] to my calendar" / "schedule [event] for [time]" | Create event |
| "do I have anything on [day]" / "am I free on [day]" | Read events |
| "connect my Google Calendar" / "link Google Calendar" / "authenticate Google Calendar" | Auth flow — use `generate_consent_link("calendar")` |

---

### Graceful degradation

If the API call returns empty or None (auth failure, network error), always fall back to a deep link. Never surface token values, error codes, or credentials in Telegram messages.

If `generate_consent_link()` raises (missing env vars, network error), fall back to a deep link
and log a warning. Do not surface the exception message to the user.

---

### Deep link (always append)

Even when creating via API, append a deep link or a view link so the user can open the event in Google Calendar:

- If event was created: `[View in Google Calendar](event.url)`
- If only creating a link: `[Add to Google Calendar](gcal_add_link_md(...))`
