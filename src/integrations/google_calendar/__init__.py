"""
Google Calendar integration for Lobster.

This package provides OAuth-based access to Google Calendar, allowing Lobster
to read and write calendar events on behalf of authenticated users.

The integration is optional: if GOOGLE_CLIENT_ID is not set in the environment,
all calendar features are gracefully disabled with a log warning.

Package layout:
    config.py         — credential loading and feature-flag helpers
    oauth.py          — OAuth 2.0 flow: auth URL generation, token exchange, refresh
    token_store.py    — per-user token persistence with auto-refresh
    client.py         — Calendar REST API: list events, create events (Phase 3)
    callback_server.py — Standalone OAuth callback HTTP server (Phase 5)
                         Run with: python -m integrations.google_calendar.callback_server
    docs/google-calendar-setup.md — Operator + end-user setup documentation
"""
