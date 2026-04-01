## Gmail — Dual-Mode Behavior

This skill operates in two modes depending on whether the user has connected their Gmail account.

### How to detect which mode to use

Run this check (takes < 1 second, no network call):

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.gmail.token_store import load_token
from mcp.user_model.owner import read_owner

owner = read_owner()
OWNER_USER_ID = owner.get("owner", {}).get("telegram_chat_id", "")
token = load_token(OWNER_USER_ID)
is_authenticated = token is not None
```

---

### Mode A: Unauthenticated (no token on disk)

Call `generate_consent_link("gmail")` to send the user a one-time consent URL.
If `generate_consent_link` raises, degrade gracefully with a user-friendly message.

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_auth.consent import generate_consent_link
import logging

log = logging.getLogger(__name__)

try:
    url = generate_consent_link("gmail")
    reply = (
        "To connect your Gmail, tap this link (expires in 30 minutes):\n"
        f"[Connect Gmail]({url})\n\n"
        "After connecting, I'll be able to read and search your emails."
    )
except Exception as exc:
    log.warning("generate_consent_link('gmail') failed: %s", exc)
    reply = (
        "I couldn't generate a Gmail connection link right now. "
        "Please try again in a few minutes, or check that LOBSTER_INSTANCE_URL "
        "and LOBSTER_INTERNAL_SECRET are set in config.env."
    )
    # Do NOT surface exc, env var names, or token values to the user.
```

---

### Mode B: Authenticated (token exists)

Read Gmail via the API. Always delegate to a background subagent — network calls
take longer than 7 seconds total.

```
send_reply(chat_id, "Checking your inbox...")
Task(prompt="...", subagent_type="general-purpose", run_in_background=true)
```

#### Reading recent emails ("check my email", "what emails do I have", "any new messages")

Subagent code pattern:

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.gmail.client import get_recent_emails
from mcp.user_model.owner import read_owner

owner = read_owner()
OWNER_USER_ID = owner.get("owner", {}).get("telegram_chat_id", "")

emails = get_recent_emails(user_id=OWNER_USER_ID, max_results=5)
if not emails:
    reply = "No recent emails in your inbox, or Gmail is not connected."
else:
    lines = []
    for e in emails:
        date_str = e.date.strftime("%a %b %-d, %-I:%M %p UTC")
        subject = e.subject or "(no subject)"
        lines.append(f"- {date_str} | {e.sender}: {subject}")
    reply = f"Your {len(emails)} most recent emails:\n" + "\n".join(lines)
```

#### Searching emails ("find emails from X", "search for Y in my email")

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.gmail.client import search_emails
from mcp.user_model.owner import read_owner

owner = read_owner()
OWNER_USER_ID = owner.get("owner", {}).get("telegram_chat_id", "")

# query is the Gmail search string derived from user's message
emails = search_emails(user_id=OWNER_USER_ID, query=query, max_results=5)
if not emails:
    reply = f"No emails found matching \"{query}\"."
else:
    lines = []
    for e in emails:
        date_str = e.date.strftime("%a %b %-d")
        subject = e.subject or "(no subject)"
        lines.append(f"- {date_str} | {e.sender}: {subject}")
    reply = f"Found {len(emails)} email(s) for \"{query}\":\n" + "\n".join(lines)
```

---

### Auth trigger ("connect my Gmail", "authenticate Gmail", "link Gmail account")

Respond immediately on the main thread — no subagent needed:

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_auth.consent import generate_consent_link
import logging

log = logging.getLogger(__name__)

try:
    url = generate_consent_link("gmail")
    reply = (
        "To connect your Gmail, tap this link (expires in 30 minutes):\n"
        f"[Connect Gmail]({url})\n\n"
        "After connecting, I'll be able to read and search your emails."
    )
except Exception as exc:
    log.warning("generate_consent_link('gmail') failed — degrading gracefully: %s", exc)
    reply = (
        "I couldn't generate a Gmail connection link right now. "
        "Please try again in a few minutes."
    )
```

---

### Natural language patterns to recognize

| Pattern | Intent |
|---------|--------|
| "check my email" / "any new emails" / "what's in my inbox" | Read recent emails |
| "find emails from [person]" / "search for [subject] in my email" | Search emails |
| "connect my Gmail" / "link Gmail" / "authenticate Gmail" | Auth flow |

---

### Graceful degradation

If `get_recent_emails` or `search_emails` returns an empty list (auth failure,
network error, empty inbox), tell the user nothing was found or Gmail is not
connected.  Never surface token values, error codes, or credentials in messages.

---

### Scope isolation

Gmail and Calendar OAuth flows are completely independent:
- Gmail tokens live in `~/messages/config/gmail-tokens/`
- Calendar tokens live in `~/messages/config/gcal-tokens/`
- Connecting Gmail never touches the Calendar token, and vice versa.
