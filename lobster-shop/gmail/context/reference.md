## Gmail Skill — Quick Reference

### Check authentication status (pure, no network)

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

### Generate consent URL (unauthenticated)

**Module:** `src/integrations/google_auth/consent.py`

```python
from integrations.google_auth.consent import generate_consent_link

try:
    url = generate_consent_link("gmail")
    # Send to user as: [Connect Gmail](url)
except Exception as exc:
    # Log warning and send a user-friendly fallback message.
    # Never surface exc details to the user.
    pass
```

---

### Read recent emails (authenticated)

**Module:** `src/integrations/gmail/client.py`

```python
from integrations.gmail.client import get_recent_emails

emails = get_recent_emails(user_id=OWNER_USER_ID, max_results=10)
# Returns List[EmailMessage] — empty list on auth failure or API error

# EmailMessage fields:
#   id: str, thread_id: str, subject: str, sender: str,
#   date: datetime (UTC), snippet: str, labels: tuple[str, ...]
```

---

### Search emails (authenticated)

```python
from integrations.gmail.client import search_emails

emails = search_emails(user_id=OWNER_USER_ID, query="from:boss@example.com is:unread")
# Returns List[EmailMessage] — empty list on auth failure or no results
```

Gmail search operators work as-is (from:, subject:, is:unread, after:, label:, etc.).

---

### User ID convention

The owner's `user_id` is their Telegram chat_id as a string, read from
`~/lobster-config/owner.toml`.
All Gmail token files live in `~/messages/config/gmail-tokens/{user_id}.json`.

---

### Scope isolation

Gmail tokens (`gmail-tokens/`) and Calendar tokens (`gcal-tokens/`) are
separate directories.  Authenticating one never affects the other.
