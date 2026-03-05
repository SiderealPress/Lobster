# Google Calendar Integration

This package implements Google Calendar OAuth access for Lobster. It provides
credential loading and feature-flag helpers as the foundation for a full
OAuth 2.0 flow in subsequent phases.

## How credentials are loaded

Credentials are read from environment variables injected by Lobster's secrets
layer (`config.env`). They must never appear in source code.

| Variable              | Description                              |
|-----------------------|------------------------------------------|
| `GOOGLE_CLIENT_ID`    | OAuth 2.0 client identifier              |
| `GOOGLE_CLIENT_SECRET`| OAuth 2.0 client secret                  |

Both variables are populated from the Google Cloud Console OAuth app
registered under the `myownlobster-platform` project.

A companion JSON file at `~/messages/config/google-oauth.json` (mode `600`)
holds the same values in structured form for use by OAuth helper scripts.
That file is never committed to version control.

## Graceful degradation

If either variable is absent, `is_enabled()` returns `False` and emits a
warning. No exception is raised at startup, so Lobster continues operating
without calendar features. Callers should always gate calendar work behind
`is_enabled()`:

```python
from integrations.google_calendar.config import is_enabled, load_credentials

if is_enabled():
    creds = load_credentials()
    # proceed with OAuth flow
```

## OAuth scopes

| Scope                                                    | Purpose                         |
|----------------------------------------------------------|---------------------------------|
| `https://www.googleapis.com/auth/calendar.readonly`      | List and read calendar events   |
| `https://www.googleapis.com/auth/calendar.events`        | Create and update calendar events|

Both scopes are requested by default via `DEFAULT_SCOPES` in `config.py`.

## Redirect URI

```
https://myownlobster.ai/auth/google/callback
```

This URI must be registered in the Google Cloud Console OAuth app's
"Authorized redirect URIs" list.

## Package structure

```
src/integrations/google_calendar/
├── __init__.py   — package docstring and public surface
├── config.py     — credential loading, is_enabled(), GoogleOAuthCredentials
└── README.md     — this file
```

## Future phases

- **Phase 2**: OAuth 2.0 flow implementation (authorization URL generation,
  token exchange, token storage)
- **Phase 3**: Calendar API client (list events, create events)
- **Phase 4**: Lobster MCP tools (`list_calendar_events`, `create_calendar_event`)
