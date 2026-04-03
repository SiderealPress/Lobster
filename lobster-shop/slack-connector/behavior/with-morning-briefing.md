## Slack Connector × Morning Briefing

When both slack-connector and morning-briefing skills are active, the morning briefing includes a Slack activity section.

### Additional briefing section: Slack highlights

Insert after "Yesterday's activity" and before "Background agents":

**Slack highlights**
- Summarize notable threads from monitored channels in the last 24 hours
- Highlight any unresolved @mentions of the owner
- Note channels with unusually high activity (>2× their 7-day average message count)
- Include any trigger rule matches that fired overnight

### Data source

Pull from the local log index — do not make live Slack API calls during briefing generation. Use `slack_channel_summary` for each monitored channel, filtered to the last 24 hours.

### Privacy

Never include DM content in the briefing unless the briefing recipient is a DM participant. Channel messages from public channels are fine to summarize.
