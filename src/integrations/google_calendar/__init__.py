"""
Google Calendar integration for Lobster.

This package provides OAuth-based access to Google Calendar, allowing Lobster
to read and write calendar events on behalf of authenticated users.

The integration is optional: if GOOGLE_CLIENT_ID is not set in the environment,
all calendar features are gracefully disabled with a log warning.

Package layout:
    config.py      — credential loading and feature-flag helpers
    oauth.py       — OAuth 2.0 flow: auth URL generation, token exchange, refresh
    token_store.py — per-user token persistence with auto-refresh
    README.md      — setup instructions and required OAuth scopes
"""
