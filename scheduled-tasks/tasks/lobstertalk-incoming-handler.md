# LobsterTalk Incoming Message Handler

> **DEPRECATED** — This job is disabled. Incoming message handling (routing to inbox,
> context lookups, and replies) has been consolidated into `lobstertalk-unified`. The
> context-lookup logic (Drive/Gmail/CRM) should be triggered by the dispatcher when it
> processes the inbox message from bot-talk. Do not re-enable without also disabling
> `lobstertalk-unified`.

**Job**: lobstertalk-incoming-handler
**Schedule**: `*/5 * * * *` (every 5 minutes)
**Skill**: `email-autoresponder` (see `lobster-shop/email-autoresponder/`)

## Context

You are $LOBSTER_NAME (read from environment), handling incoming bot-talk messages from AlbertLobster or other lobsters.
Your primary function for the LobsterTalk demo: when Albert's lobster asks "what do you know about X?",
look up X in available data sources and reply with relevant context.

For full instructions, see the skill reference:
`lobster-shop/email-autoresponder/context/reference.md` (LobsterTalk Context Handler section)

## Authentication

Read the bot-talk API token:
```python
token = open(os.path.expanduser("~/lobster-workspace/data/bot-talk-token.txt")).read().strip()
headers = {"X-Bot-Token": token}
```

## State File

Read and write `~/lobster-workspace/data/lobstertalk-incoming-state.json`.

Schema:
```json
{
  "last_processed_ts": "2026-03-27T00:00:00Z"
}
```

## Instructions

### Step 1: Load state and check for new messages

Read state file (create if not exists with `last_processed_ts = "2026-01-01T00:00:00Z"`).

Poll bot-talk for new messages from AlbertLobster:
```
GET http://46.224.41.108:4242/messages?sender=AlbertLobster&since=<last_processed_ts>&limit=50
```

Filter for messages containing query patterns:
- "what do you know about"
- "tell me about"
- "context on"
- "who is"
- "any info on"

### Step 2: For each query message, extract person and topic keywords

Extract the person name from the message. Common patterns:
- "What do you know about Bob Smith?" -> "Bob Smith"
- "What do you know about Bob?" -> "Bob"
- "Tell me about Bob Smith at Acme" -> "Bob Smith"

Also extract **topic keywords**: additional terms in the query beyond the person name. Strip structural
phrases ("what do you know about", "tell me about", "re:", etc.) and common stop words. These are used
to run a second Drive search.

Example: "What do you know about Bob Smith re: Pokemon deal?" → person: "Bob Smith", topics: ["Pokemon", "deal"]

Then query all available data sources:

#### Source 1: Google Drive — name search

Use gws CLI to search Drive files by person name:
```bash
gws drive files list --params '{"q": "fullText contains \"NAME\" or name contains \"NAME\"", "pageSize": 10}'
```

#### Source 1b: Google Drive — topic search

If topic keywords were extracted, run a second Drive search for each keyword:
```bash
gws drive files list --params '{"q": "fullText contains \"KEYWORD\" or name contains \"KEYWORD\"", "pageSize": 5}'
```

Deduplicate results by fileId across name and topic searches before reading content.

#### Reading Drive file content — mimeType routing (CRITICAL)

Check the `mimeType` field from the files list result and route accordingly:

**For native Google Docs** (`mimeType = "application/vnd.google-apps.document"`):
```bash
gws drive files export --params '{"fileId": "FILE_ID", "mimeType": "text/plain"}'
```

**For all other file types** (PDFs, plain text uploads, binary files):
```bash
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}'
```

Do NOT use `alt=media` on Google Docs — it returns garbled or empty content.

Note: Use `/usr/local/bin/gws` and `~/.config/gws/credentials.json` for auth.
The credentials.json has a refresh_token that must be refreshed using:
- client_id: from credentials.json
- client_secret: from credentials.json
- refresh_token: from credentials.json
- POST https://oauth2.googleapis.com/token

#### Source 2: Google Gmail (GMAIL_ACCOUNT2_REDACTED)

Search Gmail for emails mentioning the person:
```bash
gws gmail users.messages list --params '{"userId": "me", "q": "NAME", "maxResults": 5}'
```

For each message, get subject and snippet.

#### Source 3: Conversation/memory history

Search lobster memory for the person:
- Check `~/lobster-workspace/data/bot-talk-state.json` for any prior context
- The main lobster memory is in `~/lobster-workspace/data/memory.db` but skip if complex

#### Source 4: Twenty CRM (if API token available)

Check `~/lobster-workspace/data/twenty-api-token.txt`. If it exists, query:
```
POST https://honest-navy-moose.twenty.com/graphql
Authorization: Bearer <token>
{"query": "{ people(filter: {name: {firstName: {like: \"%NAME%\"}}}, first: 5) { edges { node { id name { firstName lastName } emails { primaryEmail } phones { primaryPhoneNumber } notes { edges { node { body } } } } } } }"}
```

### Step 3: Compose and send the response

Aggregate all findings into a concise context reply. Format:

```
Context on Bob Smith:

[Google Drive] Contact Notes - Bob Smith (Acme Corp).txt:
  - Met at Tech Conference 2026-01-15
  - Interested in Q2 2026 collaboration proposal
  - Budget: $500K, decision maker

[Google Drive — topic: Pokemon] Pokemon Partnership Deck.gdoc:
  - Pokemon collab proposal, Q2 2026
  - Decision maker: Bob Smith

[Gmail] 3 relevant emails found:
  - 2026-01-20: "Introduction" - initial intro email exchanged
  - 2026-02-05: "Follow-up" - proposal timeline discussion
  - 2026-02-28: "Q2 Timeline" - Bob confirmed Q2 works

[CRM] Bob Smith @ Acme Corp:
  - Email: bob@example.com
  - Notes: Met at conference, Q2 proposal follow-up needed
```

If nothing found: "No context found for NAME in available data sources (Drive, Gmail, CRM)."

Send the reply via bot-talk:
```
POST http://46.224.41.108:4242/message
{
  "sender": "$LOBSTER_NAME",
  "recipient": "AlbertLobster",
  "content": "<reply>",
  "genre": "acknowledgment",
  "tier": "TIER-BOT"
}
```

### Step 4: Update state

Update `last_processed_ts` to the timestamp of the latest processed message.
Write to state file atomically (write .tmp then rename).

Also notify Sahar via Telegram if a context query was received and answered:
- chat_id: ADMIN_CHAT_ID_REDACTED
- Message: "Bot-talk query handled: AlbertLobster asked about NAME. Replied with context from [sources]."

But do NOT notify Sahar for heartbeat/status messages or if no new query messages.

## Output

Call `write_task_output` with:
- job_name: "lobstertalk-incoming-handler"
- output: Brief summary (e.g. "No new queries." or "Handled query about Bob Smith, replied with context from Drive + Gmail.")
- status: "success" or "failed"

If no new queries, call `write_result` with chat_id=0 (silent).
If a query was handled, call `write_result` with chat_id=ADMIN_CHAT_ID_REDACTED and sent_reply_to_user=True.
