## Lobster Status Reporting

When the user sends `/status`, `/health`, `/stats`, or asks how you're doing / if you're alive / about system health, generate a system status report.

### How to generate the report

Run the status report script in a **background subagent** (follows the 7-second rule):

```
1. send_reply(chat_id, "Checking systems...")
2. Spawn a background subagent with this prompt:
```

**Subagent prompt template:**

```
Run the Lobster status report and send the result to the user.

1. Execute this command:
   uv run ~/lobster/lobster-shop/lobster-status/src/status_report.py

2. The script outputs a formatted Telegram status message.
   Send the EXACT output as a reply to chat_id={chat_id} via send_reply.

3. If the script fails, send a brief error message instead.
```

### Command variants

| Command | Behavior |
|---------|----------|
| `/status` | Full status report (default) |
| `/health` | Same as `/status` |
| `/stats` | Same as `/status` |
| "how are you doing?" / "are you alive?" | Same as `/status` |

### Important notes

- The status report script reads local files and system stats — no network calls, no API keys needed
- The script imports from `~/lobster/src/dashboard/collectors.py` (same data as the WebSocket dashboard)
- Output is pre-formatted for Telegram (markdown-compatible, concise, mobile-friendly)
- Do NOT modify or reformat the script output — send it exactly as printed
