# Google Calendar Setup

This document covers how to configure the Google Calendar integration for Lobster.
There are three audiences: **myownlobster.ai operators**, **single-user (self-hosted) operators**
(like a personal Lobster instance), and **end users** connecting their calendars through
the myownlobster.ai platform.

---

## Overview of the OAuth Flow

Lobster uses the Google OAuth 2.0 Authorization Code flow.  At a high level:

1. The user clicks "Connect Google Calendar" (or triggers the Lobster command).
2. Lobster generates an auth URL and sends it to the user.
3. The user clicks the link and clicks "Allow" on Google's consent screen.
4. Google redirects the user's browser to a **callback URL** with a short-lived code.
5. Lobster exchanges that code for access and refresh tokens.
6. Tokens are saved to disk (`~/messages/config/gcal-tokens/<user_id>.json`, mode 600).
7. Lobster can now read and write calendar events on the user's behalf.

---

## For myownlobster.ai Operators

This section covers the **one-time platform-level setup** that you do once, before any
users connect their calendars.

### 1. Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (e.g. `myownlobster-platform`).
3. Enable the **Google Calendar API** for the project:
   - Navigate to **APIs & Services > Library**.
   - Search for "Google Calendar API" and click **Enable**.

### 2. Configure the OAuth Consent Screen

1. Navigate to **APIs & Services > OAuth consent screen**.
2. Select **External** user type (allows any Google account to authorize).
3. Fill in the required fields:
   - **App name**: `Lobster` (or your branding)
   - **User support email**: your support address
   - **Developer contact email**: your email
4. Add scopes:
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/calendar.events`
5. Add **test users** while the app is in "Testing" mode — only these addresses can
   authorize until you publish the app.  You must add each user's Gmail address.
6. To allow any Google account (production): submit the app for **Google verification**.
   This requires a privacy policy URL and a few days of review.

### 3. Create OAuth 2.0 Credentials

1. Navigate to **APIs & Services > Credentials**.
2. Click **Create credentials > OAuth client ID**.
3. Application type: **Web application**.
4. Add the authorized redirect URI:
   ```
   https://myownlobster.ai/auth/google/callback
   ```
5. Download the JSON or copy the **Client ID** and **Client Secret**.

### 4. Set Environment Variables on Your Servers

Add these two variables to your Lobster deployment's `config.env` (or your secrets manager):

```
GOOGLE_CLIENT_ID=<your-client-id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<your-client-secret>
```

These are loaded at runtime by `src/integrations/google_calendar/config.py`.
If either variable is absent, all calendar features are gracefully disabled
with a log warning — nothing crashes.

### 5. Implement the Callback Route

The myownlobster.ai web application must handle:

```
GET https://myownlobster.ai/auth/google/callback?code=<code>&state=<state>
```

The `callback_server.py` module in `src/integrations/google_calendar/` provides
all the building blocks — import and wire them into your existing web framework:

```python
from integrations.google_calendar.callback_server import (
    _parse_callback_params,
    _handle_callback,
)
from integrations.google_calendar.oauth import exchange_code_for_tokens
from integrations.google_calendar.token_store import save_token

# In your web framework's route handler:
def google_callback_handler(request):
    params = _parse_callback_params(request.query_string)
    user_id = get_user_id_from_session(request)  # your platform's auth
    expected_state = get_state_from_session(request)  # stored when generating auth URL

    result = _handle_callback(
        params=params,
        user_id=user_id,
        expected_state=expected_state,
        exchange_fn=exchange_code_for_tokens,
        save_fn=save_token,
    )

    return HTMLResponse(result.html, status=200 if result.success else 400)
```

### Redirect URI Convention

| Deployment context | Redirect URI |
|--------------------|-------------|
| myownlobster.ai platform | `https://myownlobster.ai/auth/google/callback` |
| Self-hosted (single user) | `http://localhost:8080/auth/google/callback` |
| Custom domain | `https://<your-domain>/auth/google/callback` |

The redirect URI registered in Google Cloud Console **must exactly match** the
`redirect_uri` sent in the auth request.  The default in `config.py` is the
production myownlobster.ai URL; single-user operators override this with the
`GCAL_REDIRECT_URI` approach described below.

---

## For Single-User (Self-Hosted) Operators

This section is for operators running Lobster on a personal server or laptop
(like Drew's setup).  Lobster has no persistent web server, so OAuth completion
uses the **standalone callback server** — a tiny HTTP server that starts, handles
exactly one request, then exits.

### 1. Register a Separate Google Cloud Credential

You can reuse the same Google Cloud project as the platform, or create a separate
"personal" credential.  The key difference is the **redirect URI**:

```
http://localhost:8080/auth/google/callback
```

Add this redirect URI in Google Cloud Console under your OAuth client settings.

### 2. Set Environment Variables

```bash
# In ~/lobster/config/config.env:
GOOGLE_CLIENT_ID=<your-client-id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<your-client-secret>
GCAL_CALLBACK_HOST=localhost
GCAL_CALLBACK_PORT=8080
```

If `GCAL_CALLBACK_HOST` / `GCAL_CALLBACK_PORT` are not set, they default to
`localhost` and `8080` respectively.

### 3. Run the Callback Server

Before clicking the auth URL, start the callback server in a terminal:

```bash
cd ~/lobster
source .venv/bin/activate  # or: source config/config.env first

python -m integrations.google_calendar.callback_server \
    --user-id <your-telegram-chat-id>
```

The server will print the auth URL:

```
========================================================================
  Lobster Google Calendar OAuth
========================================================================

  Callback server: http://localhost:8080/auth/google/callback

  Open the following URL in your browser to connect Google Calendar:

  https://accounts.google.com/o/oauth2/v2/auth?...

  Waiting for Google to redirect back... (Ctrl-C to cancel)
```

### 4. Complete the Flow

1. Open the printed URL in your browser.
2. Choose your Google account.
3. Click **Allow** on the permissions screen.
4. Google redirects to `http://localhost:8080/auth/google/callback`.
5. The server shows a success page and exits.
6. Your terminal confirms: `Google Calendar connected successfully!`

The token is saved to:
```
~/messages/config/gcal-tokens/<your-user-id>.json
```

File permissions are set to `0600` (owner read/write only).

### Troubleshooting

**"redirect_uri_mismatch" error from Google**

The URI in your Google Cloud credential must match exactly.  If you're using
port 8080 locally, add `http://localhost:8080/auth/google/callback` to the
credential's authorized redirect URIs.

**"This app isn't verified" warning**

This is expected while your OAuth consent screen is in "Testing" mode.  Click
"Advanced" then "Go to <app name> (unsafe)" to proceed during development.
For production use, submit the app for Google verification.

**"Token has been expired or revoked"**

The authorization code is single-use and expires quickly (~10 minutes).
Start the callback server, then open the auth URL promptly.

**Callback server port already in use**

Change the port:
```bash
python -m integrations.google_calendar.callback_server \
    --user-id <id> --port 9090
```
Remember to register `http://localhost:9090/auth/google/callback` as an
authorized redirect URI in Google Cloud Console, and update `GCAL_CALLBACK_PORT=9090`.

**Token file permissions error**

The token directory `~/messages/config/gcal-tokens/` must be writable by the
Lobster process.  Check that the directory exists and has correct permissions:
```bash
ls -la ~/messages/config/
```

---

## For End Users (myownlobster.ai)

If you are using Lobster through myownlobster.ai, connecting Google Calendar is
a simple two-step process:

1. **Click "Connect Google Calendar"** in your onboarding checklist or settings page.
2. **Click "Allow"** on the Google permissions screen that opens.

That's it.  You will be redirected back to myownlobster.ai with a confirmation.

Your calendar is now linked.  Lobster can:
- Show you upcoming events when you ask ("What's on my calendar this week?")
- Create events when you ask ("Schedule a call with Alex tomorrow at 2pm")

To **disconnect** Google Calendar, go to Settings and click "Disconnect Calendar".
You can also revoke access directly from your [Google account permissions](https://myaccount.google.com/permissions).

---

## Re-authentication

Access tokens expire after approximately one hour.  Lobster automatically refreshes
them using the stored refresh token — you should never need to re-authenticate unless:

- You revoked access in your Google account settings.
- The refresh token expired (Google invalidates these after ~6 months of inactivity,
  or when you have more than 50 tokens for the same client).
- Lobster's token file was deleted.

When re-authentication is needed, Lobster will prompt you to reconnect via the same
"Connect Google Calendar" flow.

---

## LOBSTER_INTERNAL_SECRET (Token Bridge Authentication)

When Lobster runs as a **myownlobster.ai hosted instance**, Google Calendar token
refresh is handled by the myownlobster.ai platform API (the "token bridge"). To
authenticate these refresh requests, both sides share a secret:

- **Lobster side**: `LOBSTER_INTERNAL_SECRET` in `config/config.env`
- **myownlobster.ai side**: `LOBSTER_INTERNAL_SECRET` in Vercel's `.env.production.local`

**Both values must match exactly.** If they don't, token refresh requests are
rejected and calendar features silently stop working after the initial access
token expires (approximately one hour).

### How it gets set

The Lobster installer (`install.sh`) generates this secret automatically using:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

This produces a 43-character base64url-safe string (256 bits of entropy).

### If calendar stops working

If Google Calendar commands start failing silently (especially after working
initially), check the internal secret:

1. Verify `LOBSTER_INTERNAL_SECRET` is set in `~/lobster/config/config.env`
   (or your config directory's `config.env`).
2. Verify the same value is set in myownlobster.ai's Vercel environment
   (`LOBSTER_INTERNAL_SECRET` in `.env.production.local`).
3. If either is missing or they don't match, set them to the same value and
   redeploy / restart as needed.

To regenerate:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then update both Lobster's `config.env` and myownlobster.ai's Vercel env var.

### Self-hosted instances

If you are running the standalone callback server (self-hosted mode), this
secret is **not used** -- token refresh happens locally via the stored refresh
token. You can leave `LOBSTER_INTERNAL_SECRET` unset or ignore it.

---

## Security Notes

- **Tokens are stored per-user** in `~/messages/config/gcal-tokens/<user_id>.json`
  with permissions `0600` (only readable by the process owner).
- **No tokens are written to logs** at any log level.
- **CSRF protection**: the callback server validates the `state` parameter to
  prevent cross-site request forgery attacks.
- **The authorization code is single-use**: it is exchanged for tokens immediately
  and never stored.
- **Refresh tokens are long-lived**: treat the token files like passwords.  They are
  stored in a directory outside the Lobster source tree (`~/messages/`) to reduce
  the chance of accidental exposure via source control.
