# Google Calendar Integration

This package implements Google Calendar OAuth access for Lobster, providing
credential loading, a full OAuth 2.0 flow, per-user token persistence, and a
clean API layer for reading and writing calendar events.

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

The Calendar API helpers (`get_upcoming_events`, `create_event`) degrade
gracefully on auth failures: they return `[]` / `None` rather than raising.

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
├── __init__.py    — package docstring and public surface
├── config.py      — credential loading, is_enabled(), GoogleOAuthCredentials
├── oauth.py       — OAuth 2.0 flow: auth URL, token exchange, token refresh
├── token_store.py — per-user token persistence with auto-refresh
├── client.py      — Calendar REST API: list events, create events
└── README.md      — this file
```

---

## Public API Reference

### `config.py`

#### `GoogleOAuthCredentials`

Frozen dataclass holding OAuth client credentials.

```python
@dataclass(frozen=True)
class GoogleOAuthCredentials:
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    redirect_uri: str
```

#### `load_credentials(scopes?, redirect_uri?) -> GoogleOAuthCredentials`

Load credentials from environment variables. Raises `GoogleCredentialError` if
either `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` is absent.

#### `is_enabled() -> bool`

Return `True` if both credential environment variables are set and non-empty.
Use this as a cheap pre-flight check before attempting any calendar operation.

#### Constants

- `SCOPE_READONLY` — `"https://www.googleapis.com/auth/calendar.readonly"`
- `SCOPE_EVENTS` — `"https://www.googleapis.com/auth/calendar.events"`
- `DEFAULT_SCOPES` — `(SCOPE_READONLY, SCOPE_EVENTS)`

---

### `oauth.py`

#### `TokenData`

Frozen dataclass holding a user's OAuth tokens.

```python
@dataclass(frozen=True)
class TokenData:
    access_token: str
    expires_at: datetime   # timezone-aware UTC
    scope: str
    refresh_token: Optional[str] = None
```

#### `generate_auth_url(state, scopes?, credentials?) -> str`

Build the Google OAuth 2.0 authorization URL. Redirect the user to this URL
to begin the consent flow.

#### `exchange_code_for_tokens(code, credentials?) -> TokenData`

Exchange the authorization code (received via the callback) for access and
refresh tokens.

#### `refresh_access_token(refresh_token, credentials?) -> TokenData`

Obtain a new access token using a long-lived refresh token.

#### `is_token_valid(token) -> bool`

Return `True` if the access token is still valid (with a 5-minute safety
buffer applied).

#### Exceptions

- `OAuthError` — base class
- `OAuthTokenError(error, description)` — Google returned an OAuth error
- `OAuthNetworkError` — network-level failure reaching Google's endpoints

---

### `token_store.py`

Per-user token files are stored at `~/messages/config/gcal-tokens/{user_id}.json`
with mode `0600`.

#### `save_token(user_id, token, token_dir?) -> None`

Persist a `TokenData` to disk (atomic write, mode 600).

#### `load_token(user_id, token_dir?) -> TokenData | None`

Load a user's token from disk. Returns `None` if no file exists or the file
is malformed.

#### `get_valid_token(user_id, token_dir?, credentials?) -> TokenData | None`

Compose load + validity check + refresh + save into one call. Returns a
valid access token or `None` if the user must re-authenticate. This is the
primary entry point used by the Calendar API client.

---

### `client.py`

#### `CalendarEvent`

Frozen dataclass representing a single Google Calendar event.

```python
@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    start: datetime          # timezone-aware UTC
    end: datetime            # timezone-aware UTC
    description: str = ""
    location: str = ""
    url: Optional[str] = None   # htmlLink from Google Calendar
```

#### `get_upcoming_events(user_id, days?, credentials?) -> list[CalendarEvent]`

Fetch events from the user's primary calendar from now through `now + days`
(default 7). Returns an empty list if the user has no valid token or if any
API or network error occurs.

```python
from integrations.google_calendar.client import get_upcoming_events

events = get_upcoming_events(user_id="1234567890", days=7)
for event in events:
    print(event.title, event.start)
```

#### `create_event(user_id, title, start, end?, description?, location?, credentials?) -> CalendarEvent | None`

Create a new event on the user's primary calendar. `end` defaults to
`start + 1 hour` when not provided. Returns the created `CalendarEvent`
(with Google-assigned `id` and `url`) or `None` on auth failure or API error.

```python
from datetime import datetime, timezone
from integrations.google_calendar.client import create_event

event = create_event(
    user_id="1234567890",
    title="Doctor appointment",
    start=datetime(2026, 3, 10, 14, 0, 0, tzinfo=timezone.utc),
    location="123 Main St",
)
if event:
    print(event.url)   # browser link to the new event
```

#### `CalendarAPIError`

Domain exception raised by `_call_calendar_api` on non-2xx responses.

```python
class CalendarAPIError(RuntimeError):
    status_code: int   # HTTP status code from Google
```

#### `gcal_add_link` (re-export from `utils.calendar`)

Generate a "Add to Google Calendar" deep link — no OAuth required.

```python
from integrations.google_calendar.client import gcal_add_link

url = gcal_add_link("Doctor appointment", start, end, location="123 Main St")
# Returns: https://calendar.google.com/calendar/r/eventedit?text=...
```

Also available directly from `utils.calendar`:

```python
from utils.calendar import gcal_add_link, gcal_add_link_md
```

---

## Usage Example

```python
from integrations.google_calendar.config import is_enabled
from integrations.google_calendar.client import get_upcoming_events, create_event

if not is_enabled():
    print("Google Calendar not configured.")
else:
    # List upcoming events
    events = get_upcoming_events(user_id="1234567890", days=7)
    for event in events:
        print(f"{event.start:%Y-%m-%d %H:%M} — {event.title}")

    # Create an event
    from datetime import datetime, timezone
    new_event = create_event(
        user_id="1234567890",
        title="Sync with Drew",
        start=datetime(2026, 3, 10, 15, 0, 0, tzinfo=timezone.utc),
        description="Weekly catch-up",
    )
    if new_event:
        print(f"Created: {new_event.url}")
```

## Future phases

- **Phase 4**: Lobster MCP tools (`list_calendar_events`, `create_calendar_event`)
  exposed to Claude via the MCP server so it can answer "what's on my calendar?"
  and schedule events on command.
