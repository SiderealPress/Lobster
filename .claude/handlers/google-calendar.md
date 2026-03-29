# Google Calendar Handler

Calendar commands work in two modes. Check auth status first (no network call needed):

```python
import sys; sys.path.insert(0, "/home/admin/lobster/src")
from integrations.google_calendar.token_store import load_token
is_authenticated = load_token("<REDACTED_PHONE>") is not None
```

## Unauthenticated mode (default)

Generate a deep link whenever an event with a concrete date/time is mentioned:

```python
from utils.calendar import gcal_add_link_md
from datetime import datetime, timezone
link = gcal_add_link_md(title="Doctor appointment",
                        start=datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc))
# → [Add to Google Calendar](https://calendar.google.com/...)
```

- Append link on its own line at the end of the message
- Omit `end` to default to start + 1 hour
- Do NOT generate a link when date/time is vague

## Authenticated mode (token exists for user)

Delegate to a background subagent — API calls exceed the 7-second rule.

**Reading events** ("what's on my calendar", "what do I have this week/today"):
```python
from integrations.google_calendar.client import get_upcoming_events
events = get_upcoming_events(user_id="<REDACTED_PHONE>", days=7)
# Returns List[CalendarEvent] or [] on failure — always falls back gracefully
```

**Creating events** ("add X to my calendar", "schedule X for [time]"):
```python
from integrations.google_calendar.client import create_event
event = create_event(user_id="<REDACTED_PHONE>", title="...", start=start, end=end)
# Returns CalendarEvent with .url, or None on failure
# On failure, fall back to gcal_add_link_md()
```

Always append a deep link or view link even when creating via API.

## Auth command ("connect my Google Calendar", "authenticate Google Calendar", "link Google Calendar")

Handle on the main thread — no subagent, no API call:

```python
import secrets
from integrations.google_calendar.config import is_enabled
from integrations.google_calendar.oauth import generate_auth_url
if is_enabled():
    url = generate_auth_url(state=secrets.token_urlsafe(32))
    reply = f"Click to connect your Google Calendar:\n[Authorize Google Calendar]({url})"
else:
    reply = "Google Calendar isn't configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in config.env."
```

## Rules

- Never expose tokens, credentials, or raw error messages in replies
- If API fails, always fall back to a deep link — never return an empty reply
- user_id = owner's Telegram chat_id as string (set via config, do NOT hardcode)
- When a subagent handles events, pass event title/start/end to `gcal_add_link_md()` for the link
