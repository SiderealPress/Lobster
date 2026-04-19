# Google Calendar Skill — Onboarding

## What this skill does

This skill has two modes:

- **Unauthenticated:** Lobster generates a Google Calendar deep link for any
  event with a date and time, so you can add it to your calendar in one tap.
  No setup required.
- **Authenticated:** Once connected, Lobster reads your upcoming events on demand
  and can create events directly via the Google Calendar API.

Say "what's on my calendar this week", "add a meeting tomorrow at 2pm", or
"connect my Google Calendar" to get started.

## Prerequisites

- A Lobster instance with `LOBSTER_INSTANCE_URL` and `LOBSTER_INTERNAL_SECRET`
  set in `~/lobster-config/config.env`
- A myownlobster.ai account (handles OAuth consent — no GCP setup required
  on your end)

## One-time setup

### Step 1: Connect your Google Calendar

Send your Lobster assistant:

```
/calendar connect
```

or just say "connect my Google Calendar" or "authenticate Google Calendar".

Lobster will reply with a one-time consent link:

```
To connect your Google Calendar, tap this link (expires in 30 minutes):
[Connect Google Calendar](https://myownlobster.ai/connect/calendar?token=...)
```

Tap the link. You will be taken to Google's OAuth consent screen (hosted at
myownlobster.ai, which holds the GCP credentials centrally). Grant the
`calendar.readonly` and `calendar.events` permissions.

### Step 2: Confirmation

After granting access, myownlobster.ai exchanges the auth code for a token and
pushes it to your Lobster instance. Your token is stored locally at
`~/messages/config/gcal-tokens/{your_chat_id}.json` (mode 0o600 — owner
read/write only). No credentials leave your VPS.

Lobster will confirm when your calendar is connected.

### Step 3: Use it

```
what's on my calendar this week?
do I have anything tomorrow?
add a dentist appointment for Friday at 10am
schedule a call with Alex next Tuesday at 3pm
```

Tokens are refreshed automatically via the myownlobster.ai refresh proxy when
they expire. You should not need to re-authenticate unless you revoke access
from your Google account settings.

## Environment variables required

| Variable | Where | Purpose |
|----------|-------|---------|
| `LOBSTER_INSTANCE_URL` | `~/lobster-config/config.env` | Your VPS URL — myownlobster.ai pushes the token here after OAuth |
| `LOBSTER_INTERNAL_SECRET` | `~/lobster-config/config.env` | Shared secret authenticating the token push and refresh calls |

## Deep link mode (no auth required)

Even without connecting your calendar, Lobster will always generate a Google
Calendar deep link when you mention a concrete event with a date and time:

> "Remind me about my dentist appointment on Friday at 10am"

Lobster replies with:

> [Add to Google Calendar](https://calendar.google.com/calendar/r/eventedit?...)

Tap the link to open a pre-filled event in Google Calendar.

## Scope

This skill requests:
- `calendar.readonly` — read upcoming events
- `calendar.events` — create events on your behalf

Lobster cannot delete or modify existing events unless you explicitly ask.

Google Calendar and Gmail OAuth are independent — connecting one does not
affect the other.

## Revoking access

Revoke in Google account settings:
`https://myaccount.google.com/permissions`

Alternatively, delete `~/messages/config/gcal-tokens/{your_chat_id}.json`
on your VPS to immediately disconnect.
