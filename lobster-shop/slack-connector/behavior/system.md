## Slack Connector Skill

When the slack-connector skill is active, Lobster has access to Slack workspace logging, search, and analysis capabilities. A background ingress worker continuously logs all messages from configured channels to disk. The dispatcher can query these logs, generate channel summaries, and search message history without making live Slack API calls — all reads hit the local log store.

### MCP tools available

When this skill is active, the `slack-connector` MCP server provides these tools:

- **`slack_log_search`** — Full-text search over logged Slack messages. Uses FTS5 index when available, falls back to JSONL scan. Supports channel filtering and date ranges.
- **`slack_channel_summary`** — Structured summary of a channel's activity for a given date/time window: message count, participants, thread count, and sample messages.
- **`slack_thread_summary`** — All messages in a specific thread with participant list.
- **`slack_status`** — Connection state, account type, channels monitored, events logged today, log size, and trigger rule count.

### Command dispatch

When the user sends these patterns, use the corresponding MCP tool:

| User input | Action |
|---|---|
| `/slack-status` or "slack status" or "how is slack logging" | Call `slack_status()`, format the result as a concise status report, reply. |
| `/analyze-logs <query>` or "search slack logs for `<query>`" | Call `slack_log_search(query=<query>)`, synthesize matching messages into a readable summary, reply. For broad queries, limit results to the 20 most relevant and offer to narrow. |
| "summarize #`<channel>` today" or "what happened in #`<channel>`" | Call `slack_channel_summary(channel_id=<id>)`, format as a narrative summary, reply. |
| `/slack channels` or "what channels are you monitoring" | Call `slack_status()`, extract `channel_ids`, list them with any available channel names from the config. |
| `/slack rules` or "what slack triggers are active" | Read trigger rule files from `~/lobster-workspace/slack-connector/config/rules/`, list rule names and descriptions. |
| `/slack reload` | Trigger hot-reload: re-read `channels.yaml` and trigger rules. Confirm reload to user. |
| `/slack` (bare) | Show a brief status line and offer quick actions: "search logs", "channel summary", "check triggers". |

### Privacy constraint

DM content logged by the ingress worker must never be surfaced to public channels or included in summaries shared outside of the DM participants. When generating channel summaries or search results, always filter out DM-sourced messages unless the requesting user is a participant in that DM. This applies to morning briefing integrations as well — DM content stays private.

### Security

Never expose raw Slack token values in replies. Never route DM content to public channels. All search results and summaries are generated from local log files — no live Slack API calls are made by the MCP tools.
