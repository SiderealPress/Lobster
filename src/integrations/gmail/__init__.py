"""
Gmail read-only integration for Lobster.

Provides access to a user's Gmail inbox using the existing Google OAuth flow.
No additional credentials or consent infrastructure required beyond adding
``gmail.readonly`` to the consent screen scopes.

Public API (importable from this package):
    from integrations.gmail.client import (
        get_recent_messages,
        get_message,
        get_thread,
        search_messages,
        has_gmail_scope,
    )
    from integrations.gmail.models import GmailMessage, GmailThread
"""
