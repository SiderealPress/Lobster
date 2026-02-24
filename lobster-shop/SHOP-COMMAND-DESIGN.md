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

## Implementation

The `/shop` command is handled by the main Lobster agent using the skill management MCP tools. When Lobster sees `/shop`, it:

1. Calls `list_skills` to get all available skills with install/active status
2. Formats a response for Telegram (concise, mobile-friendly)
3. For installation: runs the skill's `install.sh` in a subagent, then calls `activate_skill`

### Skill Management Tools

| Tool | Purpose |
|------|---------|
| `list_skills` | Browse skills with status filter (all/installed/active/available) |
| `activate_skill` | Activate a skill (mode: always/triggered/contextual) |
| `deactivate_skill` | Deactivate a skill |
| `get_skill_context` | Get assembled context from all active skills |
| `get_skill_preferences` | Get merged preferences for a skill |
| `set_skill_preference` | Set a preference value |

### Status Detection

A skill is considered "installed" if:
- Its entry exists in `~/messages/config/skills-state.json` with `installed: true`
- (Legacy check: its MCP server is registered with Claude)

A skill is "active" if:
- It is installed AND has `active: true` in the skills state

A skill is "needs setup" if:
- Installed but missing required API keys/credentials

### Composable Context

Active skills inject their behavior, context, and preferences into Lobster's runtime via `get_skill_context`. Skills compose based on priority (0-100), with higher priority skills applied later. Conditional `with-<other>.md` behavior files enable cross-skill interactions.
