## Google Workspace — Dual-Mode Behavior

This skill operates in two modes depending on whether the user has connected their Google Workspace account.

### How to detect which mode to use

Run this check (takes < 1 second, no network call):

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_workspace.token_store import load_token
from mcp.user_model.owner import read_owner

owner = read_owner()
OWNER_USER_ID = owner.get("owner", {}).get("telegram_chat_id", "")
token = load_token(OWNER_USER_ID)
is_authenticated = token is not None
```

---

### Mode A: Unauthenticated (no token on disk)

Call `generate_consent_link("workspace")` to send the user a one-time consent URL.
If `generate_consent_link` raises, degrade gracefully with a user-friendly message.

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_auth.consent import generate_consent_link
import logging

log = logging.getLogger(__name__)

try:
    url = generate_consent_link("workspace")
    reply = (
        "To connect Google Workspace (Docs, Drive, Sheets), tap this link "
        "(expires in 30 minutes):\n"
        f"[Connect Google Workspace]({url})\n\n"
        "After connecting, I'll be able to read, create, and edit your Google Docs, "
        "list Drive files, and read/write Sheets."
    )
except Exception as exc:
    log.warning("generate_consent_link('workspace') failed: %s", exc)
    reply = (
        "I couldn't generate a Google Workspace connection link right now. "
        "Please try again in a few minutes, or check that LOBSTER_INSTANCE_URL "
        "and LOBSTER_INTERNAL_SECRET are set in config.env."
    )
    # Do NOT surface exc, env var names, or token values to the user.
```

---

### Mode B: Authenticated — Read a Google Doc

Always delegate to a background subagent — network calls take longer than 7 seconds total.

```
send_reply(chat_id, "Reading your doc...")
Task(prompt="...", subagent_type="general-purpose", run_in_background=true)
```

Subagent code pattern:

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_workspace.docs_client import gdocs_read
from mcp.user_model.owner import read_owner

owner = read_owner()
OWNER_USER_ID = owner.get("owner", {}).get("telegram_chat_id", "")

# doc_id_or_url comes from the user's message (doc ID or full docs.google.com URL)
content = gdocs_read(user_id=OWNER_USER_ID, doc_id_or_url=doc_id_or_url)
if content is None:
    reply = "I couldn't read that document. Make sure Google Workspace is connected and you have access to the doc."
else:
    # Truncate very long docs for Telegram display
    if len(content) > 3000:
        reply = content[:3000] + "\n\n[...doc continues — ask me to search for specific sections]"
    else:
        reply = content
```

---

### Auth trigger ("/workspace connect", "connect my Google", "link Google Workspace")

Respond immediately on the main thread — no subagent needed:

```python
import sys
import os
sys.path.insert(0, os.path.expanduser("~/lobster/src"))
from integrations.google_auth.consent import generate_consent_link
import logging

log = logging.getLogger(__name__)

try:
    url = generate_consent_link("workspace")
    reply = (
        "Tap this link to connect Google Workspace (expires in 30 minutes):\n"
        f"[Connect Google Workspace]({url})\n\n"
        "This grants access to Google Docs, Drive, and Sheets. "
        "After connecting, try /gdocs, /gdrive, or /gsheets."
    )
except Exception as exc:
    log.warning("generate_consent_link('workspace') failed — degrading gracefully: %s", exc)
    reply = (
        "I couldn't generate a Google Workspace connection link right now. "
        "Please try again in a few minutes."
    )
```

---

### Natural language patterns to recognize

| Pattern | Intent |
|---------|--------|
| "read my doc [URL or title]" / "show me [doc]" / "what's in [doc]" | Read Doc → `gdocs_read` |
| "create a doc called X" / "make a new document" | Create Doc → `gdocs_create` (Slice 3) |
| "write [content] to my doc" / "add this to my document" | Edit Doc → `gdocs_edit` (Slice 3) |
| "list my Drive files" / "what's in my Drive" | List Drive → `gdrive_list` (Slice 4) |
| "find docs about X" / "search Drive for X" | Search Drive → `gdrive_search` (Slice 4) |
| "read cells A1:C10 of [sheet]" / "show me my spreadsheet" | Read Sheet → `gsheets_read` (Slice 5) |
| "update [sheet] with [data]" / "write to my spreadsheet" | Write Sheet → `gsheets_write` (Slice 6) |
| "connect Google" / "/workspace connect" / "link my Google account" | Auth flow |

---

### Graceful degradation

If any client function returns None (auth failure, network error, doc not found), tell the
user the operation failed and suggest reconnecting. Never surface token values, error codes,
or credentials in messages.

---

---

### Post-auth confirmation (after token push)

When `push_workspace_token_endpoint` receives a valid token, the MCP server
automatically queues an outbox reply:

> "Google Workspace connected. You can now use /gdocs, /gdrive, and /gsheets."

This message is delivered by the bot process when it drains the outbox.
No action is required from the dispatcher or a subagent.

---

### Token location

Workspace tokens live at `~/messages/config/workspace-tokens/{user_id}.json`.
A single token covers Docs, Drive, Sheets, Gmail, and Calendar.
