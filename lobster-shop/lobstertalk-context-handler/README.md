# LobsterTalk Context Handler

Handles incoming context queries from AlbertLobster (or other Lobster instances) via the bot-talk protocol. When Albert asks "what do you know about Person X?", this skill's scheduled job looks up Google Drive, Gmail, and Twenty CRM, then replies with aggregated context.

## What it does

- Polls bot-talk every 5 minutes for incoming queries from AlbertLobster
- Extracts the person name AND any topic keywords from the query
- Searches Drive by person name, then runs a second search for topic keywords (deduped)
- Routes Drive file reads through the correct API: exports native Google Docs as plain text (not `alt=media`), uses `alt=media` for other file types
- Searches Gmail for threads mentioning the person
- Queries Twenty CRM for contact records and notes
- Sends a consolidated reply back to AlbertLobster via bot-talk
- Notifies Sahar on Telegram when a query is handled

## Scheduled job

**Job name**: `lobstertalk-incoming-handler`
**Schedule**: `*/5 * * * *`

## Bot commands

- `/lobstertalk` — check handler status, toggle on/off
- `/botquery` — alias
