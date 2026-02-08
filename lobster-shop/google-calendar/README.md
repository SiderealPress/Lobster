# Google Calendar

**Manage your calendar by chatting with Lobster.**

No more switching apps to check your schedule or create events. Just tell Lobster what you need.

## What You Can Do

- **"What's on my calendar today?"** — Get a summary of today's events
- **"Schedule a meeting with Alex tomorrow at 2pm"** — Create events naturally
- **"When am I free this week?"** — Find open time slots
- **"Move my 3pm to 4pm"** — Reschedule events
- **"Cancel my dentist appointment"** — Remove events
- **"Remind me about my meetings every morning at 8am"** — Daily agenda digest

## Setup

Run the installer:

```bash
bash ~/lobster/lobster-shop/google-calendar/install.sh
```

The installer will walk you through:

1. Installing required Python packages
2. Setting up Google Cloud credentials (you'll need a Google account)
3. Authorizing Lobster to access your calendar
4. Registering the calendar tools with Lobster

### Google Cloud Setup (5 minutes)

The installer will guide you, but here's the overview:

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable the **Google Calendar API**
4. Create **OAuth 2.0 credentials** (choose "Desktop application")
5. Download the `credentials.json` file
6. Place it where the installer tells you

After that, the installer handles the rest.

## Daily Agenda

Once installed, you can set up a daily morning digest:

> "Lobster, send me my calendar every morning at 8am"

Lobster will create a scheduled job that sends your day's events each morning.

## Status

**Beta** — Core features work. Being actively developed.
