"""
Google Workspace OAuth scope configuration.

Defines the full scope bundle requested when a user authorizes Google Workspace
access. The workspace scope covers Docs, Drive, Sheets, Gmail, and Calendar
in a single consent grant, so a single token is sufficient for all operations.

The ``is_enabled()`` function provides a cheap pre-flight check that degrades
gracefully when LOBSTER_INSTANCE_URL or LOBSTER_INTERNAL_SECRET are absent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OAuth scopes
# ---------------------------------------------------------------------------

#: All scopes requested in the workspace consent grant.
WORKSPACE_SCOPES: tuple[str, ...] = (
    # Google Docs: full read/write
    "https://www.googleapis.com/auth/documents",
    # Google Drive: full access + app-created files
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    # Google Sheets: full read/write
    "https://www.googleapis.com/auth/spreadsheets",
    # Gmail: read + send + archive (included for unified-token support)
    "https://www.googleapis.com/auth/gmail.modify",
    # Calendar: full read/write (included for unified-token support)
    "https://www.googleapis.com/auth/calendar",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HOME: Path = Path.home()
_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages")))
WORKSPACE_TOKEN_DIR: Path = _MESSAGES_DIR / "config" / "workspace-tokens"


# ---------------------------------------------------------------------------
# is_enabled — cheap pre-flight check
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Return True if Google Workspace can be activated for a user.

    A workspace token does not need to exist yet — this checks whether the
    OAuth flow is possible (i.e. the required environment variables are set
    so generate_consent_link("workspace") can succeed).

    Returns True if either:
    - A workspace token already exists for any user, OR
    - Both LOBSTER_INSTANCE_URL and LOBSTER_INTERNAL_SECRET are set in the
      environment (meaning the consent flow is possible).

    Degrades gracefully: always returns False rather than raising.
    """
    instance_url = os.environ.get("LOBSTER_INSTANCE_URL", "").strip()
    internal_secret = os.environ.get("LOBSTER_INTERNAL_SECRET", "").strip()

    if instance_url and internal_secret:
        return True

    # Also enabled if a token directory already exists with at least one token
    if WORKSPACE_TOKEN_DIR.exists():
        try:
            if any(WORKSPACE_TOKEN_DIR.glob("*.json")):
                return True
        except OSError:
            pass

    missing = []
    if not instance_url:
        missing.append("LOBSTER_INSTANCE_URL")
    if not internal_secret:
        missing.append("LOBSTER_INTERNAL_SECRET")

    log.warning(
        "Google Workspace integration unavailable — missing environment variables: %s. "
        "Set these in config.env to enable workspace features.",
        ", ".join(missing),
    )
    return False
