## Google Calendar — Dual-Mode Behavior

This skill operates in two modes depending on whether the user has connected their Google Calendar.

### How to detect which mode to use

Run this check (takes < 1 second, no network call):

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_calendar.token_store import load_token

# MULTI-USER: always prefer chat_id from the incoming message context.
# Fall back to read_owner() only for single-user / legacy installs where
# the message doesn't carry a chat_id (e.g. scheduled jobs).
#
# In the Lobster dispatcher the message looks like:
#   message["chat_id"]  — Telegram chat_id of the user who sent the message
#
# Usage pattern (the subagent receives chat_id as a parameter):
#   USER_ID = message_chat_id or _fallback_to_owner()
def _fallback_to_owner() -> str:
    from mcp.user_model.owner import read_owner
    owner = read_owner()
    return owner.get("owner", {}).get("telegram_chat_id", "")

# Prefer caller's chat_id; fall back to owner for single-user installs.
# `message_chat_id` must be passed in from the dispatcher context.
USER_ID = str(message_chat_id) if message_chat_id else _fallback_to_owner()
token = load_token(USER_ID)
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

# MULTI-USER: use the chat_id from the message that triggered this subagent.
# The dispatcher must pass it as a parameter when spawning the subagent.
# Fall back to read_owner() only for single-user / legacy installs.
def _get_user_id(message_chat_id=None) -> str:
    if message_chat_id:
        return str(message_chat_id)
    from mcp.user_model.owner import read_owner
    owner = read_owner()
    return owner.get("owner", {}).get("telegram_chat_id", "")

USER_ID = _get_user_id(message_chat_id)  # pass chat_id from dispatcher context

events = get_upcoming_events(user_id=USER_ID, days=7)
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

# MULTI-USER: use the chat_id from the message that triggered this subagent.
def _get_user_id(message_chat_id=None) -> str:
    if message_chat_id:
        return str(message_chat_id)
    from mcp.user_model.owner import read_owner
    owner = read_owner()
    return owner.get("owner", {}).get("telegram_chat_id", "")

USER_ID = _get_user_id(message_chat_id)  # pass chat_id from dispatcher context

# title, start, end are derived from the user's message (resolved by the dispatcher)
event = create_event(
    user_id=USER_ID,
    title=title,   # e.g. "Meeting with Sarah"
    start=start,   # datetime resolved from user's message
    end=end,       # datetime resolved from user's message (or start + 1h)
    description="",
    location="",
)

if event is not None:
    link = f"[View in Google Calendar]({event.url})" if event.url else gcal_add_link_md(
        title=title,
        start=start,
    )
    reply = f"Done — added \"{title}\" to your calendar.\n{link}"
else:
    # API failed — fall back to deep link
    link = gcal_add_link_md(title, start)
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
