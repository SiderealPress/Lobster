## Email Autoresponder — Reference

### Scheduled job

- **Job name**: `gmail-auto-draft`
- **Schedule**: Every 5 minutes (`*/5 * * * *`)
- **Account**: GMAIL_ACCOUNT_REDACTED
- **What it does**: Finds inbox emails without existing drafts, drafts context-aware HTML replies with named read-only Drive links

### Draft deduplication (critical)

**Before creating any draft**, call `list_drafts` to get all existing drafts. Build a map of `threadId -> draftId`. If a thread already has a draft, **skip it entirely** — do not create another, do not modify the existing one.

### Drive file search

The Google Workspace MCP only exposes Gmail — Drive must be queried via REST API directly:

```python
import json, urllib.request, urllib.parse
with open('/home/claude-user/.config/google-workspace-mcp/tokens.json') as f:
    tokens = json.load(f)
token = tokens['access_token']
params = urllib.parse.urlencode({'q': 'trashed=false', 'fields': 'files(id,name,mimeType)', 'pageSize': 50})
req = urllib.request.Request(
    f'https://www.googleapis.com/drive/v3/files?{params}',
    headers={'Authorization': f'Bearer {token}'}
)
with urllib.request.urlopen(req) as r:
    files = json.load(r)['files']
```

Always do a fresh search — do not rely solely on hardcoded IDs.

**Known files (verify against fresh search):**
- `General Investment Partners - Opportunity Fund I Pitch Deck` — ID: `1IIQEmt5-tzoCTjaN19Zhsq9uQWK79GQW0ojX2sWkJo0`
- `Buy My House` (Document) — ID: `1k95Kx3PBjWB7wl4UWNPDRDQADXmtVfMe51rJgkqBEB0`

### Draft logic summary

| Email type | Action |
|------------|--------|
| Business / investment inquiry | HTML draft with named pitch deck link, sign as "General Investment Partners" |
| Real estate / house inquiry | HTML draft with named Buy My House doc link, sign as "Al" |
| Spam / automated / newsletters | Skip — do NOT create a draft |
| Unclear intent | Friendly open-ended reply, reference relevant Drive files if any |

### Link rules (strictly enforced)

- **Named hyperlinks only**: `<a href="URL">Descriptive Name</a>` — NEVER paste raw URLs
- **Read-only links only** — NEVER use `/edit` links:
  - Presentation: `https://docs.google.com/presentation/d/{ID}/view?usp=sharing`
  - Document: `https://docs.google.com/document/d/{ID}/preview`

### Draft format

- Use `html` parameter (not `body`) in `draft_email`
- `to`: sender's email
- `subject`: "Re: [original subject]"
- `threadId`: original email's threadId (required for correct threading)
- Concise: 2–4 short paragraphs

### MCP tools used by the job

- `mcp__google-workspace__list_drafts` — check for existing drafts before creating
- `mcp__google-workspace__search_emails` — find inbox emails
- `mcp__google-workspace__read_email` — read full email content
- `mcp__google-workspace__draft_email` — create the reply draft
- `mcp__lobster-inbox__write_task_output` — log results

### Toggle tools (for Lobster main thread)

- `mcp__lobster-inbox__get_scheduled_job("gmail-auto-draft")` — check status
- `mcp__lobster-inbox__update_scheduled_job(name="gmail-auto-draft", enabled=True/False)` — toggle
- `mcp__lobster-inbox__check_task_outputs(job_name="gmail-auto-draft", limit=5)` — see recent results

---

## LobsterTalk Context Handler — Reference

### Scheduled job

- **Job name**: `lobstertalk-incoming-handler`
- **Schedule**: Every 5 minutes (`*/5 * * * *`)
- **Account**: GMAIL_ACCOUNT2_REDACTED
- **What it does**: Polls bot-talk for context queries from AlbertLobster, looks up Drive + Gmail + CRM, and replies via bot-talk

### Task file

`scheduled-tasks/tasks/lobstertalk-incoming-handler.md` — the full job instructions live there. This section is the reference summary.

### Step 1: Load state and check for new messages

Read state file `~/lobster-workspace/data/lobstertalk-incoming-state.json` (create with `last_processed_ts = "2026-01-01T00:00:00Z"` if not exists).

Poll bot-talk for new messages:
```
GET http://46.224.41.108:4242/messages?sender=AlbertLobster&since=<last_processed_ts>&limit=50
Authorization: X-Bot-Token <token from ~/lobster-workspace/data/bot-talk-token.txt>
```

Filter for messages containing: "what do you know about", "tell me about", "context on", "who is", "any info on"

### Step 2: Extract subject and person

From each query message, extract:
1. **Person name** — e.g., "What do you know about Bob Smith re: Pokemon deal?" → "Bob Smith"
2. **Topic keywords** — additional terms beyond the person name, after stripping stop words and structural phrases ("what do you know about", "tell me about", "re:", etc.). Example: "Pokemon deal" → `["Pokemon", "deal"]`

### Step 3: Query data sources

#### Source 1: Google Drive — name search

```bash
gws drive files list --params '{"q": "fullText contains \"NAME\" or name contains \"NAME\"", "pageSize": 10}'
```

#### Source 1b: Google Drive — topic search (topic-aware fix)

If topic keywords were extracted, run a second Drive search for each keyword:

```bash
# For each keyword KW in topic_keywords:
gws drive files list --params '{"q": "fullText contains \"KW\" or name contains \"KW\"", "pageSize": 5}'
```

Deduplicate results by fileId across both name and topic searches before reading content.

#### Reading Drive file content — mimeType routing (Google Docs export fix)

Check the `mimeType` field from the files list result. Route accordingly:

**For native Google Docs** (`mimeType = "application/vnd.google-apps.document"`):
```bash
gws drive files export --params '{"fileId": "FILE_ID", "mimeType": "text/plain"}'
```

**For all other file types** (PDFs, plain text uploads, etc.):
```bash
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}'
```

Do NOT use `alt=media` on Google Docs — it returns garbled or empty content.

#### Source 2: Gmail

```bash
gws gmail users.messages list --params '{"userId": "me", "q": "NAME", "maxResults": 5}'
```

For each message, get subject and snippet.

#### Source 3: Twenty CRM

Check `~/lobster-workspace/data/twenty-api-token.txt`. If present:
```
POST https://honest-navy-moose.twenty.com/graphql
Authorization: Bearer <token>
{"query": "{ people(filter: {name: {firstName: {like: \"%NAME%\"}}}, first: 5) { edges { node { id name { firstName lastName } emails { primaryEmail } phones { primaryPhoneNumber } notes { edges { node { body } } } } } } }"}
```

### Step 4: Compose and send reply

Aggregate findings. Format:

```
Context on Bob Smith:

[Google Drive] Contact Notes - Bob Smith.txt:
  - Met at Tech Conference 2026-01-15
  - Budget: $500K

[Google Drive — topic: Pokemon] Pokemon Partnership Deck.gdoc:
  - Pokemon collab proposal, Q2 2026
  - Decision maker: Bob Smith

[Gmail] 3 relevant emails found:
  - 2026-01-20: "Introduction"
  - 2026-02-05: "Follow-up"

[CRM] Bob Smith @ Acme Corp:
  - Email: bob@example.com
```

If nothing found: "No context found for NAME in available data sources (Drive, Gmail, CRM)."

Send reply via bot-talk:
```
POST http://46.224.41.108:4242/message
Authorization: X-Bot-Token <token>
{"sender": "OwnerLobster", "recipient": "AlbertLobster", "content": "<reply>", "genre": "acknowledgment", "tier": "TIER-BOT"}
```

### Step 5: Update state

Update `last_processed_ts` to the latest processed message timestamp. Write atomically (write .tmp then rename).

Notify the instance owner via Telegram (chat_id: ADMIN_CHAT_ID_REDACTED) if a query was handled:
"Bot-talk query handled: AlbertLobster asked about NAME. Replied with context from [sources]."

Do NOT notify for heartbeat/status messages or if no new queries.

### Output

Call `write_task_output` with job_name `"lobstertalk-incoming-handler"` and a brief summary.

### Toggle tools (for Lobster main thread)

- `mcp__lobster-inbox__get_scheduled_job("lobstertalk-incoming-handler")` — check status
- `mcp__lobster-inbox__update_scheduled_job(name="lobstertalk-incoming-handler", enabled=True/False)` — toggle
- `mcp__lobster-inbox__check_task_outputs(job_name="lobstertalk-incoming-handler", limit=5)` — see recent results
