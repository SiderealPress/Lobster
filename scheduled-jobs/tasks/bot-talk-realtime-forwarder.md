# Bot-Talk Real-Time Forwarder

**Job**: bot-talk-realtime-forwarder
**Schedule**: `*/2 * * * *` (every 2 minutes)

## Context

You are running as a scheduled task. Forward new inter-Lobster bot-talk exchanges to the owner on
Telegram as they arrive.

**Architecture**: Rather than polling the bot-talk API to detect what was sent and received, this
job reads from a local activity log (`bot-talk-activity.jsonl`) that is written at the moment each
message is sent or received. Entries with `"forwarded": false` are new and need to be forwarded.
This eliminates all filtering/identity-detection complexity — if it's in the log, it's relevant.

## Activity Log Format

The log lives at `~/lobster-workspace/data/bot-talk-activity.jsonl`.

Each line is a JSON entry:
```json
{
  "ts": "2026-03-31T14:05:00.123456+00:00",
  "direction": "sent" | "received",
  "sender": "SaharLobster",
  "recipient": "AlbertLobster",
  "text": "message content",
  "forwarded": false
}
```

Entries are appended by:
- `bot_talk_mirror.py` — for outbound messages sent via `mirror_outbound` (SaharLobster → bot-talk)
- `lobstertalk-incoming-handler` — for inbound queries from AlbertLobster and the replies sent back

## Instructions

### Step 1: Read the activity log

Open `~/lobster-workspace/data/bot-talk-activity.jsonl`. If the file does not exist, exit silently
with `write_task_output(output="No activity log found yet.", status="success")` and
`write_result(chat_id=0, sent_reply_to_user=False)`.

Read all lines. Parse each as JSON. Collect entries where `"forwarded": false`.

Sort the unforwarded entries by `ts` ascending (oldest first).

### Step 2: Forward each unforwarded entry to Telegram

For each unforwarded entry (in chronological order), call `send_reply` with:
- `chat_id`: 8305714125
- `source`: "telegram"
- `text`: formatted as:
  ```
  [Bot-Talk] {direction} — {sender}: {text}
  ```
  where `direction` is `"sent"` or `"received"`.

If `text` is longer than 1000 characters, truncate and append `... (truncated)`.

Send each entry as a separate `send_reply` call — do not batch into one message.

### Step 3: Mark forwarded entries

After forwarding all entries, rewrite `bot-talk-activity.jsonl` with every forwarded entry's
`"forwarded"` field set to `true`.

Do this atomically:
1. Read all lines again (re-read to avoid race conditions)
2. Build a set of `ts` values from entries you just forwarded
3. For each line, if its `ts` is in the forwarded set, set `"forwarded": true`
4. Write all lines to a `.tmp` file, then rename to the final path

If there are no unforwarded entries, skip the rewrite entirely.

### Step 4: Write output

Call `write_task_output` with:
- `job_name`: "bot-talk-realtime-forwarder"
- `output`: Brief summary, e.g. "No new entries." or "Forwarded 3 entries."
- `status`: "success" or "failed"

Then call `write_result`:
- If entries were forwarded: `write_result(task_id=<task_id>, chat_id=8305714125, sent_reply_to_user=True)`
- If nothing forwarded: `write_result(task_id=<task_id>, chat_id=0, sent_reply_to_user=False)`

## Timezone

Always display timestamps in **Eastern Time (ET)** — currently EDT (UTC-4). Convert all UTC
timestamps before including them in any message to the owner.

## Error handling

- If the activity log cannot be parsed (corrupt line), skip that line and continue.
- If a `send_reply` fails, stop forwarding and report the error in `write_task_output`.
  Do not mark any entries as forwarded if the send failed.
- Never call the bot-talk API directly — this job does not poll the API.
