## Slack Connector Skill

When the slack-connector skill is active, Lobster has access to Slack workspace logging, search, and analysis capabilities. A background ingress worker continuously logs all messages from configured channels to disk. The dispatcher can query these logs, generate channel summaries, and search message history without making live Slack API calls — all reads hit the local log store.

### Command handling

- **`/slack-status`** — Report the connection health of the Slack Socket Mode session, list monitored channels with message counts, and show log storage stats (disk usage, oldest/newest entry, total messages). Spawn a subagent to gather this data.
- **`/analyze-logs`** — Accept a natural-language query (e.g., "/analyze-logs what did #engineering discuss about deployments this week") and search the local log index. Return a concise summary of matching messages with timestamps, authors, and thread context. For broad queries, limit results to the 20 most relevant messages and offer to narrow the search.
- **`/slack`** — General entry point. Show a brief status line and offer quick actions: "search logs", "channel summary", "check triggers". Route to the appropriate handler based on the user's follow-up.

### Privacy constraint

DM content logged by the ingress worker must never be surfaced to public channels or included in summaries shared outside of the DM participants. When generating channel summaries or search results, always filter out DM-sourced messages unless the requesting user is a participant in that DM. This applies to morning briefing integrations as well — DM content stays private.
