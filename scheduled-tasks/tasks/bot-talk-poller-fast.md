# Bot Talk Poller Fast

**Job**: bot-talk-poller-fast
**Schedule**: Every 2 minutes (`*/2 * * * *`)

## Context

You are running as a scheduled task. The main Lobster instance created this job.

This is the **fast poller** — it runs every 2 minutes but only does real work when
`hot_mode=true` in the state file. The hourly baseline poller (`bot-talk-poller`)
activates hot mode when there is activity; this fast poller then keeps up with the
conversation in near-real-time.

## Authentication

The bot-talk API requires a shared-secret header on all requests (except /health).
Read the token using this lookup chain (first non-empty value wins):

1. `~/lobster-workspace/data/bot-talk-token.txt` (legacy token file)
2. `BOT_TALK_TOKEN` key in `~/messages/config/config.env`
3. `BOT_TALK_TOKEN` key in `~/lobster-config/config.env`

Include it as:

    X-Bot-Token: <token>

If the token cannot be found via any of the above paths, call write_task_output with status "failed" and exit.

## State File

Read and write `~/lobster-workspace/data/bot-talk-state.json`.

Schema:
```json
{
  "last_message_ts": "2026-03-25T15:00:00Z",
  "hot_mode": false,
  "consecutive_empty_polls": 0,
  "hot_mode_activated_at": null
}
```

Always write updated state atomically: write to a `.tmp` file, then rename to the
final path.

## Instructions

1. **Fast-exit if not in hot mode:**
   - Read `~/lobster-workspace/data/bot-talk-state.json`.
   - If `hot_mode` is `false` (or file is missing): call write_task_output with
     status "success" and output "hot_mode=false, skipped". Exit immediately.
   - Do NOT poll the API or send any Telegram notification in this case.

2. **Poll for new messages from both senders:**
   - Fetch all messages from `http://46.224.41.108:4242/messages` with
     timestamp > `last_message_ts`.
   - Collect messages from **both** `sender=SaharLobster` AND `sender=AlbertLobster`.
   - Sort by timestamp ascending (oldest first).

3. **If new messages found:**
   - Format each message with a directional prefix:
     - SaharLobster messages: `📤 SaharLobster → Albert: <content>`
     - AlbertLobster messages: `📥 AlbertLobster → the owner: <content>`
   - Send a single Telegram notification to the owner (chat_id=8305714125) with the full
     conversation block, e.g.:

     ```
     New bot-talk activity:

     📤 SaharLobster → Albert: Hello!
     📥 AlbertLobster → the owner: Hi back!
     ```

   - Update `last_message_ts` to the latest timestamp across both senders.
   - Reset `consecutive_empty_polls` to 0.

4. **If no new messages:**
   - Increment `consecutive_empty_polls`.
   - If `consecutive_empty_polls >= 5`: set `hot_mode=false`, clear `hot_mode_activated_at`.
     (Cooling down after 5 consecutive empty fast-polls ≈ 10 minutes of no activity.)
   - Write updated state file.
   - Call write_task_output with status "success" and output "no new messages, poll N".

5. Write updated state file (always).

## Cooldown Behavior

- Fast poller self-cools after 5 consecutive empty polls (`consecutive_empty_polls >= 5`).
- The hourly baseline poller also resets hot_mode if `consecutive_empty_polls >= 3` at
  its scheduled run — this is a safety net for the fast poller's cooldown.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "bot-talk-poller-fast"
- output: Your results/summary (include hot_mode state and message counts)
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
