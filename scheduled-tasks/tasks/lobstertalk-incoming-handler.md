# Lobstertalk Incoming Handler

**Job**: lobstertalk-incoming-handler
**Kind**: 1 (polling — dispatched only when new messages are found)
**Triggered by**: lobstertalk-incoming-check.sh (pre-check script)
**Created**: 2026-03-28

## Context

You are running because lobstertalk-incoming-check.sh found one or more new messages
addressed to SaharLobster on the bot-talk API since the last processed timestamp.

The bot-talk API is at `$BOT_TALK_API_URL` (default: http://46.224.41.108:4242).
The auth token is in `~/lobster-workspace/data/bot-talk-token.txt`.
The watermark state file is `~/lobster-workspace/data/lobstertalk-incoming-state.json`.

## Instructions

### Step 1: Load state

Read the bot-talk auth token:
```
TOKEN=$(cat ~/lobster-workspace/data/bot-talk-token.txt)
```

Read the last-processed timestamp from `~/lobster-workspace/data/lobstertalk-incoming-state.json`.
If the file does not exist or is malformed, use `2026-01-01T00:00:00Z` as the fallback.

### Step 2: Fetch new incoming messages

Query the bot-talk API for messages addressed to SaharLobster since the last-processed timestamp:

```
GET $BOT_TALK_API_URL/messages?recipient=SaharLobster&since=<last_processed_ts>&limit=20
X-Bot-Token: <token>
```

If the API is unreachable, write a brief error to `write_task_output` and exit — do not update the watermark.

### Step 3: Process each new message

For each message returned:

1. Read the message content and identify the sender.
2. Determine the appropriate response:
   - **If the message is a question or request to SaharLobster**: respond directly via the bot-talk API (POST to `/messages` or the appropriate reply endpoint) as `SaharLobster`, signing responses with `🤖🦞 Lobster (ops):`. Keep responses concise.
   - **If the message requires Sahar's direct input or judgment**: queue a Telegram notification (see Step 4 — do not send immediately, collect first).
   - **If the message is informational only** (status update, FYI, no reply expected): acknowledge receipt if appropriate, otherwise skip.
3. Track the timestamp of the latest message processed.

### Step 4: Notify Sahar (only if actionable)

**No-op gate**: Only send a Telegram notification (`send_reply` to `chat_id=8305714125`) if at least one message genuinely requires Sahar's attention and cannot be handled autonomously. Combine all notifications into a single message — never send one per message.

If all messages were handled autonomously, do not send a Telegram notification.

### Step 5: Update the watermark

Write the timestamp of the latest processed message to the state file atomically:

```python
import json, os
state_file = os.path.expanduser('~/lobster-workspace/data/lobstertalk-incoming-state.json')
state = {'last_processed_ts': '<latest_message_timestamp>'}
tmp = state_file + '.tmp'
with open(tmp, 'w') as f:
    json.dump(state, f, indent=2)
os.replace(tmp, state_file)
```

## Output

Call `write_task_output` with:
- `job_name`: "lobstertalk-incoming-handler"
- `output`: Brief summary, e.g. "Processed 3 messages from AlbertLobster. Replied to 2 directly, notified Sahar of 1." or "Processed 1 message. Handled autonomously — no Sahar notification needed."
- `status`: "success" or "failed"

Keep output concise — the main Lobster instance reviews these logs later.
