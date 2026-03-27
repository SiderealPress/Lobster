# LobsterTalk Incoming Message Handler

**Job**: lobstertalk-incoming-handler
**Schedule**: `*/5 * * * *` (every 5 minutes)

## Context

You are SaharLobster, handling incoming bot-talk messages from AlbertLobster or other lobsters.
Your primary function for the LobsterTalk demo: when Albert's lobster asks "what do you know about X?",
look up X in available data sources and reply with relevant context.

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

### Step 2: For each query message, look up the person

Extract the person name from the message. Common patterns:
- "What do you know about Bob Smith?" -> "Bob Smith"
- "What do you know about Bob?" -> "Bob"
- "Tell me about Bob Smith at Acme" -> "Bob Smith"

Then query all available data sources:

#### Source 1: Google Drive (robotsquadsm@gmail.com)

Use gws CLI to search Drive files:
```bash
gws drive files list --params '{"q": "fullText contains \"NAME\" or name contains \"NAME\"", "pageSize": 10}'
```

For each matching file, read its content:
```bash
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}'
```

Note: Use `/usr/local/bin/gws` and `~/.config/gws/credentials.json` for auth.
The credentials.json has a refresh_token that must be refreshed using:
- client_id: from credentials.json
- client_secret: from credentials.json
- refresh_token: from credentials.json
- POST https://oauth2.googleapis.com/token

#### Source 2: Google Gmail (robotsquadsm@gmail.com)

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
  - Acme Corp expanding into AI manufacturing
  - Budget: $500K, decision maker

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
  "sender": "SaharLobster",
  "recipient": "AlbertLobster",
  "content": "<reply>",
  "genre": "acknowledgment",
  "tier": "TIER-BOT"
}
```

### Step 4: Update state

Update `last_processed_ts` to the timestamp of the latest processed message.
Write to state file atomically (write .tmp then rename).

Also notify the user via Telegram if a context query was received and answered:  # noname
- chat_id: 8305714125
- Message: "Bot-talk query handled: AlbertLobster asked about NAME. Replied with context from [sources]."

But do NOT notify for heartbeat/status messages or if no new query messages.  # noname

## Output

Call `write_task_output` with:
- job_name: "lobstertalk-incoming-handler"
- output: Brief summary (e.g. "No new queries." or "Handled query about Bob Smith, replied with context from Drive + Gmail.")
- status: "success" or "failed"

If no new queries, call `write_result` with chat_id=0 (silent).
If a query was handled, call `write_result` with chat_id=8305714125 and sent_reply_to_user=True.
