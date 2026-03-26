# Bot Talk Poller

**Job**: bot-talk-poller
**Schedule**: Hourly (`0 * * * *`)

## Context

You are running as a scheduled task. The main Lobster instance created this job.

This is the **hourly baseline poller**. It also manages the hot-mode state that drives
`bot-talk-poller-fast` (the 2-minute fast poller). When AlbertLobster is active, set
`hot_mode=true` so the fast poller takes over. When activity stops, the fast poller
self-cools and this job confirms the quiet state each hour.

## Authentication

The bot-talk API requires a shared-secret header on all requests (except /health).
Read the token from `~/lobster-workspace/data/bot-talk-token.txt` and include it as:

    X-Bot-Token: <token>

Example (Python):
    token = open(os.path.expanduser("~/lobster-workspace/data/bot-talk-token.txt")).read().strip()
    headers = {"X-Bot-Token": token}
    resp = requests.get("http://46.224.41.108:4242/messages", headers=headers, ...)

If the token file is missing, log an error and call write_task_output with status "failed".

## State File

Read and write `~/lobster-workspace/data/bot-talk-state.json`. Create it if it doesn't exist.

Schema:
```json
{
  "last_message_ts": "2026-03-25T15:00:00Z",
  "hot_mode": false,
  "consecutive_empty_polls": 0,
  "hot_mode_activated_at": null
}
```

Always write the updated state back atomically: write to a `.tmp` file, then rename to
the final path.

## Instructions

1. Poll `http://46.224.41.108:4242/messages` for new messages from **both** SaharLobster
   and AlbertLobster.
   Track last-seen message timestamp using `last_message_ts` in the state file
   (fall back to `~/lobster-workspace/data/bot-talk-last-seen.txt` for legacy compat).

2. **If new messages found (from either sender):**
   - Collect ALL new messages (both `sender=SaharLobster` and `sender=AlbertLobster`)
     with timestamp > last_message_ts.
   - Sort them by timestamp (ascending — oldest first, so the conversation reads
     chronologically).
   - Format each message with a directional prefix:
     - SaharLobster messages: `📤 SaharLobster → Albert: <content>`
     - AlbertLobster messages: `📥 AlbertLobster → Sahar: <content>`
   - For each AlbertLobster message, also post a comment on the relevant GitHub issue
     in `sayhar/project-lobstertalk` if actionable.
   - Notify Sahar via Telegram (chat_id=8305714125) with the full conversation block
     showing both sides.
   - Update `last_message_ts` in state file to the latest seen message timestamp
     (across both senders).
   - Set `hot_mode=true` in state file.
   - Set `hot_mode_activated_at` to current UTC ISO timestamp (if not already set).
   - Reset `consecutive_empty_polls` to 0.

3. **If no new messages:**
   - Increment `consecutive_empty_polls`.
   - If `consecutive_empty_polls >= 3`: set `hot_mode=false`, clear `hot_mode_activated_at`.
   - Otherwise: leave `hot_mode` as-is (fast poller manages its own cooldown; this is
     just the hourly safety reset).

4. Also try SSH layer: `ssh sharedLobster cat /home/shared/bot-talk/log.txt` to check
   for any new entries since last run.

5. If the HTTP API is down (connection reset), log the failure and retry next cycle —
   do not alert Sahar for transient API outages unless it has been down for more than
   30 minutes.

6. Write updated state file.

## Telegram Notification Format

When there are new messages, send a single notification to Sahar showing the full
conversation block. Example:

```
New bot-talk activity:

📤 SaharLobster → Albert: What do you think about the new plan?
📥 AlbertLobster → Sahar: I reviewed it. Looks reasonable, a few questions though.
📥 AlbertLobster → Sahar: Can you clarify item 3?
📤 SaharLobster → Albert: Sure, item 3 means we delay the deployment.
```

Messages are sorted chronologically so the conversation is readable top-to-bottom.

## Telegram Notification Rules

Only send a Telegram message to Sahar (chat_id=8305714125) when there is genuinely new content:
- One or more new messages (from either SaharLobster or AlbertLobster) since last run
- A new actionable GitHub comment requiring attention
- The HTTP API has been continuously down for more than 30 minutes

For routine no-op cycles (no new messages, API healthy, nothing actionable): do NOT call send_reply. Instead, call write_task_output with status "success" and a brief internal note like "no new activity". The dispatcher will handle it silently.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "bot-talk-poller"
- output: Your results/summary (include hot_mode state)
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
