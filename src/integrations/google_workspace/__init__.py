"""
Google Workspace integration package — Docs, Drive, Sheets, Gmail, Calendar.

Provides OAuth token management and API clients for Google Workspace services.
All tokens are stored locally at ~/messages/config/workspace-tokens/{user_id}.json
using the same stateless code pass-through OAuth pattern as the gmail and
google_calendar integrations.

Quick usage::

    from integrations.google_workspace.token_store import get_valid_token

    token = get_valid_token(user_id)
    if token is None:
        # User needs to authorize — call generate_consent_link("workspace")
        ...
"""
