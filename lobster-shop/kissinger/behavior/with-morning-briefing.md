# Kissinger + Morning Briefing Integration

When both kissinger and morning-briefing are active, augment the daily briefing
with CRM insights:

1. **Stale contacts** — Run `kissinger_contacts_stale(days=14)` and list anyone
   the owner hasn't touched in 2+ weeks with a nudge to follow up.

2. **Pending follow-ups** — Search for interactions logged with follow_up dates
   on or before today. Remind the owner of any due follow-ups.

3. **Graph health** — Include a one-line stat from `kissinger_graph_stats`
   (e.g., "Network: 47 people, 112 connections").

Keep this section brief — 3–5 bullet points max. Lead with whoever needs
attention most urgently.
