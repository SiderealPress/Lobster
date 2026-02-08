# /shop Command — Design Document

The `/shop` command lets users browse and manage skills directly from Telegram.

## Usage

### Browse skills

```
User: /shop

Lobster:
  Lobster Shop

  Installed:
    (none yet)

  Available:
    Google Calendar (beta) — See your schedule, create events, get reminders
    Install: /shop install google-calendar

  Coming Soon:
    Notion — Search notes, create pages from chat
    Spotify — Control your music by asking
    Home Automation — Control lights and devices
    Email Triage — Summarize inbox, draft replies
    Expense Tracking — Log expenses, get summaries
```

### Install a skill

```
User: /shop install google-calendar

Lobster:
  Installing Google Calendar...

  Dependencies installed.
  Config directory created at ~/lobster/config/google-calendar/

  Manual setup needed:
  1. Create a Google Cloud project at https://console.cloud.google.com
  2. Enable the Google Calendar API
  3. Create OAuth credentials (Desktop app)
  4. Download credentials.json
  5. Send it to me here, or place it at:
     ~/lobster/config/google-calendar/credentials.json

  Then tell me "calendar setup done" and I'll finish the auth flow.
```

### Check skill status

```
User: /shop status google-calendar

Lobster:
  Google Calendar (beta)
  Status: Installed, needs credentials
  Missing: credentials.json not found
  Next step: Download OAuth credentials from Google Cloud Console
```

### Uninstall a skill

```
User: /shop uninstall google-calendar

Lobster:
  Uninstalled Google Calendar.
  - Removed MCP server registration
  - Config files kept at ~/lobster/config/google-calendar/ (delete manually if wanted)
```

## Implementation Notes

The `/shop` command would be handled by the main Lobster agent (not a separate bot command handler). When Lobster sees `/shop`, it:

1. Reads `lobster-shop/INDEX.md` and skill manifests
2. Checks which skills are installed (look for MCP registrations, config dirs)
3. Formats a response for Telegram (concise, mobile-friendly)

For installation, Lobster would:
1. Run the skill's `install.sh` in a subagent
2. Report progress back to the user
3. Guide through any manual steps interactively

### Status Detection

A skill is considered "installed" if:
- Its MCP server is registered with Claude (`claude mcp list`)
- Its config directory exists

A skill is "needs setup" if:
- Installed but missing required API keys/credentials

### Not Yet Implemented

This is a design document. The actual `/shop` command handler will be built once the first skill (Google Calendar) is fully functional and the pattern is validated.
