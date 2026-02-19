#!/usr/bin/env python3
"""
Google Calendar Integration for Lobster MCP Server

Provides OAuth2 authentication and calendar operations:
- connect_calendar: Start OAuth flow, return authorization URL
- list_calendar_events: List upcoming events
- check_availability: Check if a time slot is free
- create_calendar_event: Create a new event
- get_week_schedule: Summary of the week's events

Per-user token storage at ~/lobster/config/calendar_tokens/<chat_id>.json
Credentials expected at ~/lobster/config/google_credentials.json
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# Configuration paths
LOBSTER_CONFIG_DIR = Path.home() / "lobster" / "config"
CREDENTIALS_FILE = LOBSTER_CONFIG_DIR / "google_credentials.json"
TOKENS_DIR = LOBSTER_CONFIG_DIR / "calendar_tokens"
PENDING_FLOWS_DIR = LOBSTER_CONFIG_DIR / "calendar_pending_flows"

# Google Calendar API scopes
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

# Redirect URI for desktop/installed app flow
# Google deprecated urn:ietf:wg:oauth:2.0:oob in 2022.
# We use http://localhost - user copies the code from the redirect URL.
REDIRECT_URI = "http://localhost"


def _ensure_dirs():
    """Ensure token and pending flow directories exist."""
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_FLOWS_DIR.mkdir(parents=True, exist_ok=True)


def _token_path(chat_id: str) -> Path:
    """Get token file path for a given chat_id."""
    return TOKENS_DIR / f"{chat_id}.json"


def _pending_flow_path(chat_id: str) -> Path:
    """Get pending flow state file for a given chat_id."""
    return PENDING_FLOWS_DIR / f"{chat_id}_flow.json"


def _check_credentials_file() -> tuple[bool, str]:
    """Check if Google credentials file exists and is valid."""
    if not CREDENTIALS_FILE.exists():
        return False, (
            f"Google credentials file not found at {CREDENTIALS_FILE}.\n\n"
            "To set up Google Calendar integration:\n"
            "1. Go to https://console.cloud.google.com/\n"
            "2. Create a project (or select existing)\n"
            "3. Enable the Google Calendar API\n"
            "4. Go to Credentials > Create Credentials > OAuth 2.0 Client ID\n"
            "5. Choose 'Desktop app' as application type\n"
            "6. Download the JSON and save it as:\n"
            f"   {CREDENTIALS_FILE}\n\n"
            "See docs/google-calendar-setup.md in the Lobster repo for detailed instructions."
        )
    try:
        with open(CREDENTIALS_FILE) as f:
            creds_data = json.load(f)
        # Validate structure - could be "installed" (desktop) or "web" type
        if "installed" not in creds_data and "web" not in creds_data:
            return False, (
                f"Credentials file at {CREDENTIALS_FILE} has unexpected format. "
                "Expected 'installed' or 'web' key. Please download OAuth 2.0 "
                "Client ID credentials (Desktop app type) from Google Cloud Console."
            )
        return True, "Credentials file found and valid."
    except json.JSONDecodeError:
        return False, f"Credentials file at {CREDENTIALS_FILE} is not valid JSON."


def _get_credentials(chat_id: str) -> Optional[Credentials]:
    """Load and refresh credentials for a user. Returns None if not authenticated."""
    token_path = _token_path(chat_id)
    if not token_path.exists():
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception:
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception:
            # Refresh failed - user needs to re-authenticate
            return None

    if not creds.valid:
        return None

    return creds


def _get_calendar_service(chat_id: str):
    """Get an authenticated Google Calendar service for a user."""
    creds = _get_credentials(chat_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def _format_event(event: dict) -> str:
    """Format a single calendar event for display."""
    summary = event.get("summary", "(No title)")
    location = event.get("location", "")
    description = event.get("description", "")

    start = event.get("start", {})
    end = event.get("end", {})

    # Handle all-day events (date) vs timed events (dateTime)
    if "date" in start:
        start_str = start["date"]
        end_str = end.get("date", "")
        time_str = f"{start_str} (all day)"
    else:
        start_dt = datetime.fromisoformat(start.get("dateTime", ""))
        end_dt = datetime.fromisoformat(end.get("dateTime", ""))
        date_str = start_dt.strftime("%Y-%m-%d")
        start_time = start_dt.strftime("%H:%M")
        end_time = end_dt.strftime("%H:%M")
        time_str = f"{date_str} {start_time}-{end_time}"

    parts = [f"  {summary} | {time_str}"]
    if location:
        parts.append(f"    Location: {location}")
    if description:
        # Truncate long descriptions
        desc_short = description[:100] + "..." if len(description) > 100 else description
        desc_short = desc_short.replace("\n", " ")
        parts.append(f"    Note: {desc_short}")

    return "\n".join(parts)


# ─── MCP Tool Handlers ───────────────────────────────────────────────────────


def handle_connect_calendar(args: dict) -> str:
    """
    Start or complete the OAuth flow for Google Calendar.

    If 'auth_code' is provided, exchange it for tokens.
    Otherwise, generate and return the authorization URL.
    """
    _ensure_dirs()

    chat_id = str(args.get("chat_id", ""))
    auth_code = args.get("auth_code", "").strip()

    if not chat_id:
        return "Error: chat_id is required to connect a calendar."

    # Check if credentials file exists
    valid, msg = _check_credentials_file()
    if not valid:
        return msg

    # Check if already connected
    existing_creds = _get_credentials(chat_id)
    if existing_creds and not auth_code:
        return (
            f"Calendar already connected for user {chat_id}. "
            "To reconnect, provide auth_code to re-authorize, or "
            "delete the token file and try again."
        )

    # Load credentials data
    with open(CREDENTIALS_FILE) as f:
        creds_data = json.load(f)

    # Determine credential type
    cred_type = "installed" if "installed" in creds_data else "web"
    client_config = creds_data[cred_type]

    if auth_code:
        # Step 2: Exchange authorization code for tokens
        try:
            flow = Flow.from_client_secrets_file(
                str(CREDENTIALS_FILE),
                scopes=SCOPES,
                redirect_uri=REDIRECT_URI,
            )
            flow.fetch_token(code=auth_code)
            creds = flow.credentials

            # Save tokens
            token_path = _token_path(chat_id)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

            # Clean up pending flow state
            pending_path = _pending_flow_path(chat_id)
            if pending_path.exists():
                pending_path.unlink()

            return (
                f"Google Calendar connected successfully for user {chat_id}!\n\n"
                "You can now use these commands:\n"
                "- list_calendar_events: See upcoming events\n"
                "- get_week_schedule: See your week at a glance\n"
                "- check_availability: Check if a time slot is free\n"
                "- create_calendar_event: Add a new event"
            )
        except Exception as e:
            return f"Error exchanging authorization code: {e}\n\nPlease try the connect flow again."

    else:
        # Step 1: Generate authorization URL
        try:
            flow = Flow.from_client_secrets_file(
                str(CREDENTIALS_FILE),
                scopes=SCOPES,
                redirect_uri=REDIRECT_URI,
            )
            auth_url, state = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent",
            )

            # Save flow state for later
            flow_state = {
                "state": state,
                "chat_id": chat_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(_pending_flow_path(chat_id), "w") as f:
                json.dump(flow_state, f)

            return (
                f"To connect your Google Calendar, click this link:\n\n"
                f"{auth_url}\n\n"
                "After authorizing, your browser will redirect to a localhost URL that won't load. "
                "That's expected! Copy the 'code' parameter from the URL in your address bar "
                "(everything after 'code=' up to the next '&') and send it back to me.\n\n"
                "Use: connect_calendar with chat_id and auth_code parameters."
            )
        except Exception as e:
            return f"Error generating authorization URL: {e}"


def handle_list_calendar_events(args: dict) -> str:
    """List upcoming calendar events."""
    chat_id = str(args.get("chat_id", ""))
    days_ahead = args.get("days_ahead", 7)
    max_results = args.get("max_results", 20)

    if not chat_id:
        return "Error: chat_id is required."

    service = _get_calendar_service(chat_id)
    if not service:
        return (
            f"Calendar not connected for user {chat_id}. "
            "Use connect_calendar to set up Google Calendar access."
        )

    try:
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(days=days_ahead)

        events_result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = events_result.get("items", [])

        if not events:
            return f"No events found in the next {days_ahead} days."

        lines = [f"Upcoming events (next {days_ahead} days):\n"]
        current_date = None
        for event in events:
            start = event.get("start", {})
            if "date" in start:
                event_date = start["date"]
            else:
                event_date = datetime.fromisoformat(start.get("dateTime", "")).strftime("%Y-%m-%d")

            if event_date != current_date:
                current_date = event_date
                # Format date nicely
                try:
                    dt = datetime.strptime(event_date, "%Y-%m-%d")
                    day_name = dt.strftime("%A, %B %d")
                    lines.append(f"\n--- {day_name} ---")
                except ValueError:
                    lines.append(f"\n--- {event_date} ---")

            lines.append(_format_event(event))

        return "\n".join(lines)

    except Exception as e:
        return f"Error listing events: {e}"


def handle_check_availability(args: dict) -> str:
    """Check if a time slot is free."""
    chat_id = str(args.get("chat_id", ""))
    date_str = args.get("date", "")
    start_time = args.get("start_time", "")
    end_time = args.get("end_time", "")

    if not chat_id:
        return "Error: chat_id is required."
    if not date_str or not start_time or not end_time:
        return "Error: date, start_time, and end_time are all required. Format: date=YYYY-MM-DD, times=HH:MM"

    service = _get_calendar_service(chat_id)
    if not service:
        return (
            f"Calendar not connected for user {chat_id}. "
            "Use connect_calendar to set up Google Calendar access."
        )

    try:
        # Parse the date and times - assume user's local timezone or UTC
        # We'll use the calendar's timezone setting
        calendar = service.calendars().get(calendarId="primary").execute()
        cal_tz = calendar.get("timeZone", "UTC")

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cal_tz)

        start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)

        # Query freebusy
        body = {
            "timeMin": start_dt.isoformat(),
            "timeMax": end_dt.isoformat(),
            "items": [{"id": "primary"}],
        }
        freebusy = service.freebusy().query(body=body).execute()
        busy_periods = freebusy.get("calendars", {}).get("primary", {}).get("busy", [])

        if not busy_periods:
            return (
                f"You're FREE on {date_str} from {start_time} to {end_time}. "
                "No conflicting events found."
            )
        else:
            conflicts = []
            for period in busy_periods:
                busy_start = datetime.fromisoformat(period["start"]).astimezone(tz)
                busy_end = datetime.fromisoformat(period["end"]).astimezone(tz)
                conflicts.append(f"  Busy: {busy_start.strftime('%H:%M')} - {busy_end.strftime('%H:%M')}")

            return (
                f"BUSY on {date_str} from {start_time} to {end_time}.\n"
                f"Conflicting periods:\n" + "\n".join(conflicts)
            )

    except Exception as e:
        return f"Error checking availability: {e}"


def handle_create_calendar_event(args: dict) -> str:
    """Create a new calendar event."""
    chat_id = str(args.get("chat_id", ""))
    title = args.get("title", "")
    date_str = args.get("date", "")
    start_time = args.get("start_time", "")
    end_time = args.get("end_time", "")
    description = args.get("description", "")
    location = args.get("location", "")
    attendees = args.get("attendees", [])

    if not chat_id:
        return "Error: chat_id is required."
    if not title:
        return "Error: title is required."
    if not date_str or not start_time or not end_time:
        return "Error: date, start_time, and end_time are all required. Format: date=YYYY-MM-DD, times=HH:MM"

    service = _get_calendar_service(chat_id)
    if not service:
        return (
            f"Calendar not connected for user {chat_id}. "
            "Use connect_calendar to set up Google Calendar access."
        )

    try:
        # Get calendar timezone
        calendar = service.calendars().get(calendarId="primary").execute()
        cal_tz = calendar.get("timeZone", "UTC")

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cal_tz)

        start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)

        event_body = {
            "summary": title,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": cal_tz,
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": cal_tz,
            },
        }

        if description:
            event_body["description"] = description
        if location:
            event_body["location"] = location
        if attendees:
            event_body["attendees"] = [{"email": email} for email in attendees]

        created_event = service.events().insert(calendarId="primary", body=event_body).execute()

        event_link = created_event.get("htmlLink", "")
        event_id = created_event.get("id", "")

        return (
            f"Event created successfully!\n\n"
            f"  Title: {title}\n"
            f"  Date: {date_str}\n"
            f"  Time: {start_time} - {end_time}\n"
            f"  Timezone: {cal_tz}\n"
            + (f"  Location: {location}\n" if location else "")
            + (f"  Description: {description}\n" if description else "")
            + f"\n  Event ID: {event_id}\n"
            + (f"  Link: {event_link}" if event_link else "")
        )

    except Exception as e:
        return f"Error creating event: {e}"


def handle_get_week_schedule(args: dict) -> str:
    """Get a summary of the current week's events."""
    chat_id = str(args.get("chat_id", ""))

    if not chat_id:
        return "Error: chat_id is required."

    service = _get_calendar_service(chat_id)
    if not service:
        return (
            f"Calendar not connected for user {chat_id}. "
            "Use connect_calendar to set up Google Calendar access."
        )

    try:
        # Get calendar timezone
        calendar = service.calendars().get(calendarId="primary").execute()
        cal_tz = calendar.get("timeZone", "UTC")

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cal_tz)

        now = datetime.now(tz)

        # Start of this week (Monday) and end of week (Sunday)
        days_since_monday = now.weekday()
        week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
        week_end = week_start + timedelta(days=7)

        events_result = service.events().list(
            calendarId="primary",
            timeMin=week_start.isoformat(),
            timeMax=week_end.isoformat(),
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = events_result.get("items", [])

        if not events:
            return f"No events this week ({week_start.strftime('%b %d')} - {week_end.strftime('%b %d')})."

        # Group events by day
        days = {}
        for event in events:
            start = event.get("start", {})
            if "date" in start:
                event_date = start["date"]
            else:
                event_date = datetime.fromisoformat(start.get("dateTime", "")).astimezone(tz).strftime("%Y-%m-%d")

            if event_date not in days:
                days[event_date] = []
            days[event_date].append(event)

        lines = [
            f"Week Schedule ({week_start.strftime('%b %d')} - {week_end.strftime('%b %d')}):",
            f"Timezone: {cal_tz}",
            f"Total events: {len(events)}",
            "",
        ]

        # Show each day of the week
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for i in range(7):
            day_dt = week_start + timedelta(days=i)
            date_key = day_dt.strftime("%Y-%m-%d")
            day_name = day_names[i]
            day_label = f"{day_name}, {day_dt.strftime('%b %d')}"

            is_today = day_dt.date() == now.date()
            if is_today:
                day_label += " (TODAY)"

            day_events = days.get(date_key, [])

            if day_events:
                lines.append(f"--- {day_label} ({len(day_events)} events) ---")
                for event in day_events:
                    lines.append(_format_event(event))
            else:
                lines.append(f"--- {day_label} ---")
                lines.append("  (no events)")

            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"Error getting week schedule: {e}"


# ─── Tool Definitions (for MCP registration) ─────────────────────────────────

CALENDAR_TOOLS = [
    {
        "name": "connect_calendar",
        "description": "Start or complete the Google Calendar OAuth flow. First call without auth_code to get the authorization URL. Second call with the auth_code from the user to complete connection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "oneOf": [{"type": "integer"}, {"type": "string"}],
                    "description": "The user's chat ID (used to store per-user tokens).",
                },
                "auth_code": {
                    "type": "string",
                    "description": "The authorization code from Google after the user approves. Leave empty to get the auth URL.",
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "list_calendar_events",
        "description": "List upcoming Google Calendar events for a connected user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "oneOf": [{"type": "integer"}, {"type": "string"}],
                    "description": "The user's chat ID.",
                },
                "days_ahead": {
                    "type": "integer",
                    "description": "Number of days ahead to look. Default 7.",
                    "default": 7,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of events to return. Default 20.",
                    "default": 20,
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "check_availability",
        "description": "Check if a specific time slot is free on a user's Google Calendar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "oneOf": [{"type": "integer"}, {"type": "string"}],
                    "description": "The user's chat ID.",
                },
                "date": {
                    "type": "string",
                    "description": "Date to check (YYYY-MM-DD format).",
                },
                "start_time": {
                    "type": "string",
                    "description": "Start time (HH:MM format, 24h).",
                },
                "end_time": {
                    "type": "string",
                    "description": "End time (HH:MM format, 24h).",
                },
            },
            "required": ["chat_id", "date", "start_time", "end_time"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create a new event on a user's Google Calendar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "oneOf": [{"type": "integer"}, {"type": "string"}],
                    "description": "The user's chat ID.",
                },
                "title": {
                    "type": "string",
                    "description": "Event title/summary.",
                },
                "date": {
                    "type": "string",
                    "description": "Event date (YYYY-MM-DD format).",
                },
                "start_time": {
                    "type": "string",
                    "description": "Start time (HH:MM format, 24h).",
                },
                "end_time": {
                    "type": "string",
                    "description": "End time (HH:MM format, 24h).",
                },
                "description": {
                    "type": "string",
                    "description": "Optional event description/notes.",
                },
                "location": {
                    "type": "string",
                    "description": "Optional event location.",
                },
                "attendees": {
                    "type": "array",
                    "description": "Optional list of attendee email addresses.",
                    "items": {"type": "string"},
                },
            },
            "required": ["chat_id", "title", "date", "start_time", "end_time"],
        },
    },
    {
        "name": "get_week_schedule",
        "description": "Get a summary of the current week's events from a user's Google Calendar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "oneOf": [{"type": "integer"}, {"type": "string"}],
                    "description": "The user's chat ID.",
                },
            },
            "required": ["chat_id"],
        },
    },
]

# Handler dispatch map
CALENDAR_HANDLERS = {
    "connect_calendar": handle_connect_calendar,
    "list_calendar_events": handle_list_calendar_events,
    "check_availability": handle_check_availability,
    "create_calendar_event": handle_create_calendar_event,
    "get_week_schedule": handle_get_week_schedule,
}
