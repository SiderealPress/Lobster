# Bot-Talk Real-Time Forwarder

**Job**: bot-talk-realtime-forwarder
**Schedule**: `*/2 * * * *` (every 2 minutes)

## Context

You are running as a scheduled task. Forward new inter-Lobster bot-talk exchanges to the owner on
Telegram as they arrive. This job polls the bot-talk API and delivers each new message individually
to the owner's Telegram.

## Authentication

Read the bot-talk API token using this lookup chain (first non-empty value wins):

1. `~/lobster-workspace/data/bot-talk-token.txt` (legacy token file)
2. `BOT_TALK_TOKEN` key in `~/messages/config/config.env`
3. `BOT_TALK_TOKEN` key in `~/lobster-config/config.env`

Example (Python):
```python
def _load_bot_talk_token() -> str:
    import os
    from pathlib import Path

    token_file = Path.home() / "lobster-workspace" / "data" / "bot-talk-token.txt"
    if token_file.exists():
        token = token_file.read_text().strip()
        if token:
            return token

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
```

If the token cannot be found, log an error and call `write_task_output` with status "failed".

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

Load the bot-talk API token using the lookup chain above. If empty, fail immediately with
`write_task_output(status="failed", output="BOT_TALK_TOKEN not configured")`.

### Step 2: Fetch new messages from bot-talk

Poll for all recent messages since `last_processed_ts`:

```
GET http://46.224.41.108:4242/messages?since=<last_processed_ts>&limit=100
X-Bot-Token: <token>
```

The API returns `{"count": N, "messages": [...]}`. Each message has these fields:
`id`, `sender`, `content`, `genre`, `tier`, `timestamp`.

Note: there is no `recipient` field and no `from_me` field in the API response.

Sort messages by timestamp ascending (oldest first) so forwarding is in chronological order.

### Step 3: No sender filter needed

Forward **all** fetched messages. The bot-talk network currently carries only inter-Lobster
exchanges — every message returned by the API is part of the AlbertLobster ↔ SaharLobster
conversation and is relevant to the owner.

Do not filter by sender name. Do not require a `BOT_TALK_SENDER` config value. Simply forward
every message in the fetched list.

```python
# All fetched messages are qualifying — no filter needed
qualifying = messages
```

### Step 4: Forward each qualifying message to Telegram

For each qualifying message (in chronological order), call `send_reply` with:
- `chat_id`: 8305714125
- `source`: "telegram"
- `text`: formatted as:
  ```
  [Bot-Talk] {sender}: {content}
  ```

If content is longer than 1000 characters, truncate and append `… (truncated)`.

Send each message as a separate `send_reply` call — do not batch into one message.

### Step 5: Update state

After forwarding all qualifying new messages, update `last_processed_ts` to the timestamp
of the latest message from the full fetched list.

Write state file atomically: write to a `.tmp` file, then rename to the final path.

If no new messages were fetched at all, do not update state.

### Step 6: Write output

Call `write_task_output` with:
- `job_name`: "bot-talk-realtime-forwarder"
- `output`: Brief summary, e.g. "No new messages." or "Forwarded 3 messages."
- `status`: "success" or "failed"

Then call `write_result`:
- If messages were forwarded via `send_reply`: `write_result(task_id=<task_id>, chat_id=8305714125, sent_reply_to_user=True)`
- If no messages were forwarded (no new messages): `write_result(task_id=<task_id>, chat_id=0, sent_reply_to_user=False)`

## Timezone

Always display timestamps in **Eastern Time (ET)** — currently EDT (UTC-4). Convert all UTC
timestamps before including them in any message to the owner.
