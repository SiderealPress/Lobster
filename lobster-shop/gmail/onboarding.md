# Gmail Skill — Onboarding

## What this skill does

Once connected, Lobster can read and search your Gmail inbox on demand. Say
"check my email", "any new messages?", or "find emails from [person]" and
Lobster will fetch results directly from the Gmail API using your OAuth token.

## Prerequisites

- A Lobster instance running with `LOBSTER_INSTANCE_URL` and
  `LOBSTER_INTERNAL_SECRET` set in `~/lobster-config/config.env`
- A myownlobster.ai account (handles OAuth consent — no GCP setup required
  on your end)

## One-time setup

### Step 1: Connect your Gmail account

Send your Lobster assistant:

```
/gmail connect
```

or just say "connect my Gmail" or "authenticate Gmail".

Lobster will reply with a one-time consent link:

```
To connect your Gmail, tap this link (expires in 30 minutes):
[Connect Gmail](https://myownlobster.ai/connect/gmail?token=...)
```

Tap the link. You will be taken to Google's OAuth consent screen (hosted at
myownlobster.ai, which holds the GCP credentials centrally). Grant the
`gmail.readonly` permission.

### Step 2: Confirmation

After granting access, myownlobster.ai exchanges the auth code for a token and
pushes it to your Lobster instance via `POST /api/push-gmail-token`. Your token
is stored locally at `~/messages/config/gmail-tokens/{your_chat_id}.json`
(mode 0o600 — owner read/write only). No credentials leave your VPS.

Lobster will confirm when your Gmail is connected.

### Step 3: Use it

```
check my email
any new emails?
find emails from Sarah
search for "invoice" in my email
```

Tokens are refreshed automatically via the myownlobster.ai refresh proxy when
they expire. You should not need to re-authenticate unless you revoke access
from your Google account settings.

## Environment variables required

| Variable | Where | Purpose |
|----------|-------|---------|
| `LOBSTER_INSTANCE_URL` | `~/lobster-config/config.env` | Your VPS URL — myownlobster.ai pushes the token here after OAuth |
| `LOBSTER_INTERNAL_SECRET` | `~/lobster-config/config.env` | Shared secret authenticating the token push and refresh calls |

## Scope

This skill requests `gmail.readonly` only. Lobster cannot send, delete, or
modify emails on your behalf.

Gmail and Google Calendar OAuth are independent — connecting one does not
affect the other.

## Revoking access

Revoke in Google account settings:
`https://myaccount.google.com/permissions`

Alternatively, delete `~/messages/config/gmail-tokens/{your_chat_id}.json`
on your VPS to immediately disconnect.
