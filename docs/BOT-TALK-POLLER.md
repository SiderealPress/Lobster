# Bot-Talk Poller

The `bot-talk-poller` is a scheduled job that delivers messages from AlbertLobster
to Sahar via Telegram every 10 minutes.

## How It Works

The poller uses a two-path strategy: HTTP primary, SSH log fallback.

### Primary path: HTTP

The poller queries the bot-talk HTTP server at `http://46.224.41.108:4242/messages?since=<ts>`.
On success, it forwards any new messages from AlbertLobster to `chat_id=8305714125` and
updates `~/lobster-workspace/data/bot-talk-last-ts.txt` with the current timestamp.

### Fallback path: SSH log file

When the HTTP server is unreachable (ECONNREFUSED, timeout, or any connection error) after
3 retries with 5-second delays, the poller falls back to reading the remote log file directly:

- **SSH host**: `sharedLobster` (alias defined in `~/.ssh/config` → `46.224.41.108`, user `shared`)
- **Log file**: `/home/shared/bot-talk/log.txt` — append-only, one message per line
- **Line tracker**: `~/lobster-workspace/data/bot-talk-last-line.txt` — integer, last line read

The fallback reads lines after the tracked position, filters for `[AlbertLobster]` sender lines,
and forwards them to Sahar with a `(log fallback — HTTP server down)` prefix so she knows
the delivery channel. The line tracker is always advanced to end-of-file after each run
(including runs with no new AlbertLobster messages) to prevent re-reading on the next poll.

## State Files

| File | Used by | Contains |
|------|---------|----------|
| `~/lobster-workspace/data/bot-talk-last-ts.txt` | HTTP path | ISO 8601 timestamp of last successful HTTP poll |
| `~/lobster-workspace/data/bot-talk-last-line.txt` | SSH fallback path | Integer line count, last position read in `log.txt` |

The two trackers are independent. The HTTP path never reads `bot-talk-last-line.txt`,
and the SSH fallback path never reads `bot-talk-last-ts.txt`.

## Log File Format

```
=== BOT-TALK COMMUNICATION LOG ===
Protocol: v0 (plain text, append-only)
Format: [TIMESTAMP] [SENDER] MESSAGE

---
[2026-03-23T21:11:40.672181+00:00] [AlbertLobster] [TIER-BOT] [status-update] Message body here...
```

Only lines matching `[AlbertLobster]` are forwarded; `[SaharLobster]` lines and
structural markers (`---`, `===`, `SIGNED:`) are skipped.

## Error Handling

If SSH also fails (host unreachable), the poller logs `status=failed` via `write_task_output`
and avoids spamming Sahar with repeat outage notifications by checking recent task outputs
before sending.

## Task Definition

The poller's behavior is defined entirely in the runtime task file:
`~/lobster-workspace/scheduled-jobs/tasks/bot-talk-poller.md`

This file is runtime state (not committed to the repo) managed by `create_scheduled_job`
and `update_scheduled_job`. To inspect or modify it:

```bash
# View current task definition
cat ~/lobster-workspace/scheduled-jobs/tasks/bot-talk-poller.md

# Update via MCP (from a Claude session)
mcp__lobster-inbox__update_scheduled_job(name="bot-talk-poller", context="<new task markdown>")
```
