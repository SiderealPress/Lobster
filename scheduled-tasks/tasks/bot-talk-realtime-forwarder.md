# Bot-Talk Real-Time Forwarder

**Job**: bot-talk-realtime-forwarder
**Schedule**: `*:0/2` (every 2 minutes)

## Context

You are running as a scheduled task. Forward new inter-Lobster bot-talk exchanges to the owner on
Telegram as they arrive. This job polls the bot-talk API for messages from any sender and
delivers each new message individually to the owner's Telegram.

## State File

Read and write `~/lobster-workspace/data/bot-talk-realtime-state.json`.

Schema:
```json
{
  "last_processed_ts": "2026-01-01T00:00:00Z"
}
```

Create with default `last_processed_ts = "2026-01-01T00:00:00Z"` if not found.

## Instructions

### Step 1: Load state

Read `~/lobster-workspace/data/bot-talk-realtime-state.json`. Extract `last_processed_ts`.

Read the bot-talk API token from `~/lobster-workspace/data/bot-talk-token.txt`.

### Step 2: Fetch new messages from bot-talk

Poll for all recent messages from any sender since `last_processed_ts`:

```
GET http://46.224.41.108:4242/messages?since=<last_processed_ts>&limit=50
Authorization header: X-Bot-Token: <token>
```

Parse the response. Each message has at minimum: `sender`, `content`, `timestamp` (or `created_at`).
Sort messages by timestamp ascending (oldest first) so forwarding is in chronological order.

Include all messages from all senders (AlbertLobster, SaharLobster, and any others) so the owner
sees the full conversation thread.

### Step 3: Forward each new message to Telegram

For each new message (in chronological order), send a Telegram message to chat_id `8305714125`:

Format:
```
[Bot-Talk] {sender}: {content}
```

If the content is longer than 1000 characters, truncate and append `… (truncated)`.

Use `send_reply` with:
- `chat_id`: 8305714125
- `text`: formatted message
- `source`: "telegram"

Send each message as a separate `send_reply` call — do not batch.

### Step 4: Update state

After forwarding all new messages, update `last_processed_ts` to the timestamp of the latest
message processed.

Write state file atomically (write `.tmp` then rename).

If no new messages were found, do not update state.

### Step 5: Write output

Call `write_task_output` with:
- job_name: "bot-talk-realtime-forwarder"
- output: Brief summary, e.g. "No new messages." or "Forwarded 3 messages to Telegram (AlbertLobster x2, SaharLobster x1)."
- status: "success" or "failed"

Then call `write_result` with `chat_id=0` (silent — owner was already notified inline).
