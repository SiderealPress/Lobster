## LobsterTalk Context Handler — Reference

### Scheduled job

- **Job name**: `lobstertalk-incoming-handler`
- **Schedule**: Every 5 minutes (`*/5 * * * *`)
- **Account**: robotsquadsm@gmail.com
- **What it does**: Polls bot-talk for context queries from AlbertLobster, looks up Drive + Gmail + CRM, and replies via bot-talk

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
2. **Topic keywords** — additional terms from the message beyond the person name. Strip common stop words and structural phrases ("what do you know about", "tell me about", "re:", etc.). Example: "Pokemon deal" → `["Pokemon", "deal"]`

These topic keywords are used to run a second Drive search (see below).

### Step 3: Query data sources

#### Source 1: Google Drive — name search

```bash
gws drive files list --params '{"q": "fullText contains \"NAME\" or name contains \"NAME\"", "pageSize": 10}'
```

#### Source 1b: Google Drive — topic search (NEW)

If topic keywords were extracted, run a second Drive search for those keywords:

```bash
# For each keyword KW in topic_keywords:
gws drive files list --params '{"q": "fullText contains \"KW\" or name contains \"KW\"", "pageSize": 5}'
```

Deduplicate results by fileId across both name and topic searches.

#### Reading Drive file content — mimeType routing (CRITICAL FIX)

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
{"sender": "SaharLobster", "recipient": "AlbertLobster", "content": "<reply>", "genre": "acknowledgment", "tier": "TIER-BOT"}
```

### Step 5: Update state

Update `last_processed_ts` to the latest processed message timestamp. Write atomically (write .tmp then rename).

Notify Sahar via Telegram (chat_id: 8305714125) if a query was handled:
"Bot-talk query handled: AlbertLobster asked about NAME. Replied with context from [sources]."

Do NOT notify for heartbeat/status messages or if no new queries.

### Output

Call `write_task_output` with job_name `"lobstertalk-incoming-handler"` and a brief summary.

### Tools used by the job

- `gws` CLI for Drive and Gmail
- `write_task_output` — log results
- `send_reply` — notify Sahar on Telegram when a query is handled
