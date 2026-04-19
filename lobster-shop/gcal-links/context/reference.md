## Google Calendar Skill — Quick Reference

### Check authentication status (pure, no network)

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_calendar.token_store import load_token

# MULTI-USER: prefer chat_id from the incoming message context.
# Fall back to read_owner() only for single-user / legacy installs.
def _get_user_id(message_chat_id=None) -> str:
    if message_chat_id:
        return str(message_chat_id)
    from mcp.user_model.owner import read_owner
    owner = read_owner()
    return owner.get("owner", {}).get("telegram_chat_id", "")

USER_ID = _get_user_id(message_chat_id)  # pass chat_id from dispatcher context
token = load_token(USER_ID)
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

events = get_upcoming_events(user_id=USER_ID, days=7)
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
    user_id=USER_ID,
    title="Meeting with Sarah",
    start=datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc),
    end=datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc),   # optional
    description="",   # optional
    location="",      # optional
)
# Returns CalendarEvent with .url set (Google link), or None on failure
```

---

### Generate auth URL (consent flow)

```python
from integrations.google_auth.consent import generate_consent_link

try:
    url = generate_consent_link("calendar")
    # Send to user as: [Connect Google Calendar](url)
except Exception as exc:
    # Log warning and send a user-friendly fallback message.
    # Never surface exc details to the user.
    pass
```

---

### User ID convention

For multi-user deployments, `user_id` is the **caller's Telegram `chat_id`**
passed in from the message context — not the owner's ID.
Fall back to `read_owner()` only for single-user / legacy installs where no
`chat_id` is available in context.

All token files live in `~/messages/config/gcal-tokens/{user_id}.json`.

---

### Scope isolation

Calendar tokens (`gcal-tokens/`) and Gmail tokens (`gmail-tokens/`) are
separate directories. Authenticating one never affects the other.
