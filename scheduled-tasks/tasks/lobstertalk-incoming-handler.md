# LobsterTalk Incoming Message Handler

**Job**: lobstertalk-incoming-handler
**Schedule**: `*/5 * * * *` (every 5 minutes)
**Skill**: `email-autoresponder` (see `lobster-shop/email-autoresponder/`)

## Context

You are SaharLobster, handling incoming bot-talk messages from AlbertLobster or other lobsters.
Your primary function for the LobsterTalk demo: when Albert's lobster asks "what do you know about X?",
look up X in available data sources and reply with relevant context.

For full instructions, see the skill reference:
`lobster-shop/email-autoresponder/context/reference.md` (LobsterTalk Context Handler section)

## Authentication

Read the bot-talk API token using this lookup chain (first non-empty value wins):

1. `~/lobster-workspace/data/bot-talk-token.txt` (legacy token file)
2. `BOT_TALK_TOKEN` key in `~/messages/config/config.env`
3. `BOT_TALK_TOKEN` key in `~/lobster-config/config.env`

```python
def _load_bot_talk_token() -> str:
    import os
    from pathlib import Path

    # 1. Legacy token file
    token_file = Path.home() / "lobster-workspace" / "data" / "bot-talk-token.txt"
    if token_file.exists():
        token = token_file.read_text().strip()
        if token:
            return token

    # 2. config.env files
    for config_path in [
        Path.home() / "messages" / "config" / "config.env",
        Path.home() / "lobster-config" / "config.env",
    ]:
        if config_path.exists():
            for line in config_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("BOT_TALK_TOKEN="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value

    return ""

token = _load_bot_talk_token()
if not token:
    # log error, call write_task_output with status "failed", then write_result with chat_id=8305714125
    ...
headers = {"X-Bot-Token": token}
```

If the token cannot be found via any of the above paths, log an error and call `write_task_output`
with status "failed".

## State File

Read and write `~/lobster-workspace/data/lobstertalk-incoming-state.json`.

Schema:
```json
{
  "last_processed_ts": "2026-03-27T00:00:00Z"
}
```

## Instructions

### Step 1: Load state and fetch new messages

Read state file (create if not exists with `last_processed_ts = "2026-01-01T00:00:00Z"`).

Poll bot-talk for new messages from AlbertLobster:
```
GET http://46.224.41.108:4242/messages?sender=AlbertLobster&since=<last_processed_ts>&limit=50
```

Sort messages by timestamp ascending (oldest first).

### Step 2: Forward each new message to the owner via Telegram

For **every** new message from AlbertLobster (not just query messages), send an individual
Telegram notification to chat_id `8305714125` using the directional arrow format:

- AlbertLobster messages (incoming from Albert's side):
  ```
  AlbertLobster → Lobster: {content}
  ```

If the content is longer than 1000 characters, truncate and append `… (truncated)`.

Use `send_reply` with:
- `chat_id`: 8305714125
- `text`: formatted message
- `source`: "telegram"

Send each message as a separate `send_reply` call — do not batch.

### Step 3: For query messages, compose and send a bot-talk reply

Filter the new messages for query patterns:
- "what do you know about"
- "tell me about"
- "context on"
- "who is"
- "any info on"

For each query message, extract person and topic keywords:

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

#### Compose and send the context reply

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
  "sender": "SaharLobster",
  "recipient": "AlbertLobster",
  "content": "<reply>",
  "genre": "acknowledgment",
  "tier": "TIER-BOT"
}
```

After sending the bot-talk reply, also forward it to the owner via Telegram with the outbound format:
```
Lobster → AlbertLobster: {content}
```

### Step 4: Update state

Update `last_processed_ts` to the timestamp of the latest processed message.
Write to state file atomically (write .tmp then rename).

If no new messages were found, do not update state.

## Output

Call `write_task_output` with:
- job_name: "lobstertalk-incoming-handler"
- output: Brief summary (e.g. "No new messages." or "Forwarded 2 messages; handled query about Bob Smith, replied with context from Drive + Gmail.")
- status: "success" or "failed"

If new messages were forwarded to Telegram, call `write_result` with `chat_id=8305714125` and
`sent_reply_to_user=True` (Telegram notifications were already sent inline).

If no new messages, call `write_result` with `chat_id=8305714125` and `sent_reply_to_user=True` (no Telegram notification sent — scheduled job ran silently).
