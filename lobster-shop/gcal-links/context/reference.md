## Google Calendar Skill — Quick Reference

### Check authentication status (pure, no network)

```python
import sys
sys.path.insert(0, "/home/admin/lobster/src")
from integrations.google_calendar.token_store import load_token

token = load_token("1234567890")   # owner's user_id = str(chat_id)
is_authenticated = token is not None
```

---

### Deep link (no auth required)

**Module:** `src/utils/calendar.py`

```python
from utils.calendar import gcal_add_link, gcal_add_link_md
from datetime import datetime, timezone

start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
end   = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)

# Telegram markdown link
link = gcal_add_link_md(title="Doctor appointment", start=start, end=end)
# → [Add to Google Calendar](https://calendar.google.com/calendar/r/eventedit?...)
```

`end` defaults to `start + 1 hour` if omitted.

---

### Read events (authenticated)

**Module:** `src/integrations/google_calendar/client.py`

```python
from integrations.google_calendar.client import get_upcoming_events

events = get_upcoming_events(user_id="1234567890", days=7)
# Returns List[CalendarEvent] — empty list on auth failure or API error

# CalendarEvent fields:
#   id: str, title: str, start: datetime, end: datetime,
#   description: str, location: str, url: Optional[str]
```

---

### Create event (authenticated)

```python
from integrations.google_calendar.client import create_event
from datetime import datetime, timezone

event = create_event(
    user_id="1234567890",
    title="Meeting with Sarah",
    start=datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc),
    end=datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc),   # optional
    description="",   # optional
    location="",      # optional
)
# Returns CalendarEvent with .url set (Google link), or None on failure
```

---

### Generate auth URL

```python
import secrets
from integrations.google_calendar.config import is_enabled
from integrations.google_calendar.oauth import generate_auth_url

if is_enabled():
    state = secrets.token_urlsafe(32)
    url = generate_auth_url(state=state)
    # Send to user as: [Authorize Google Calendar](url)
```

---

### User ID convention

The owner's `user_id` is their Telegram chat_id as a string (e.g. `"1234567890"`).
All token files live in `~/messages/config/gcal-tokens/{user_id}.json`.
